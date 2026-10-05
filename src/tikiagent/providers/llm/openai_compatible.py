"""基于 Chat Completions 的 OpenAI-compatible 模型适配器。"""

from collections.abc import Mapping, Sequence
from typing import Any
import json

from openai import OpenAI

from tikiagent.providers.llm.config import ModelSettings
from tikiagent.providers.llm.request import structured_messages
from tikiagent.providers.llm.models import ModelResponse, ModelToolCall, StructuredModel
from tikiagent.providers.llm.structured_output import (
    StructuredOutputError,
    parse_structured_output,
)


class ModelOutputError(RuntimeError):
    """模型输出无法安全执行；不携带可能含敏感数据的响应正文。"""

    def __init__(self, message, diagnostics=None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


class OpenAICompatibleClient:
    """可通过配置连接 DeepSeek、vLLM 等兼容后端。"""

    def __init__(
        self,
        settings: ModelSettings,
        client: Any | None = None,
        observer=None,
    ) -> None:
        self.settings = settings
        self.observer = observer
        self.client = client or OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.timeout_seconds,
            max_retries=settings.max_retries,
        )

    def _observe(self, response):
        """每次供应商响应均记录预算和安全诊断，包括结构化解析失败之前。"""
        choice = response.choices[0] if getattr(response, "choices", None) else None
        usage = getattr(response, "usage", None)
        data = {"finish_reason": getattr(choice, "finish_reason", None),
                "max_output_tokens": self.settings.max_output_tokens,
                "reasoning_effort": self.settings.reasoning_effort,
                "available_input_budget": self.settings.context_limit - self.settings.max_output_tokens - self.settings.safety_margin,
                "usage": usage.model_dump() if hasattr(usage, "model_dump") else {},
                "tool_names": [c.function.name for c in (getattr(getattr(choice, "message", None), "tool_calls", None) or [])]}
        if self.observer:
            try:
                self.observer(data)
            except OSError:
                # 审计写入失败不能把已取得的模型响应变成重新付费请求。
                pass
        return data

    @staticmethod
    def _convert_tools(
        tool_schemas: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": schema["name"],
                    "description": schema["description"],
                    "parameters": schema["parameters"],
                },
            }
            for schema in tool_schemas
        ]

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tool_schemas: Sequence[Mapping[str, Any]],
        *, _single: bool = False,
    ) -> ModelResponse:
        self._check_budget(messages, self._convert_tools(tool_schemas))
        request_messages = list(messages)
        client = self.client.with_options(max_retries=0) if _single and hasattr(self.client, "with_options") else self.client
        attempts = 1 if _single else 2
        diagnostics = []
        # 空响应、截断或坏参数合计最多重试一次，绝不交给工具执行半截参数。
        for attempt in range(attempts):
            self._check_budget(request_messages, self._convert_tools(tool_schemas))
            response = client.chat.completions.create(
                model=self.settings.model, messages=request_messages,
                **({"tools": self._convert_tools(tool_schemas), "tool_choice": "auto"} if tool_schemas else {}),
                max_tokens=self.settings.max_output_tokens,
                **self.settings.generation_options(),
            )
            response_details = self._observe(response)
            if not getattr(response, "choices", None):
                raise ValueError(f"模型返回非 ChatCompletion 响应：type={type(response).__name__}，缺少 choices")
            message = response.choices[0].message
            invalid = getattr(response.choices[0], "finish_reason", None) == "length"
            for call in message.tool_calls or []:
                try:
                    invalid = invalid or not isinstance(json.loads(call.function.arguments), dict)
                except (ValueError, TypeError):
                    invalid = True
            diagnostics.append({"attempt": attempt + 1, "finish_reason": getattr(response.choices[0], "finish_reason", None),
                                "invalid_output": invalid, "has_text": bool(message.content), "tool_call_count": len(message.tool_calls or [])})
            if invalid:
                if attempt == attempts - 1:
                    raise ModelOutputError("模型返回截断或无效工具参数；未执行本批工具", {"attempts": diagnostics})
                request_messages = [*messages, {"role": "user", "content":
                    "上一响应截断或工具参数不完整，未执行任何工具。请简短输出，工具参数必须是完整 JSON 对象；证据使用引用，勿复制长正文。"}]
                continue
            if message.tool_calls or (message.content and message.content.strip()):
                break
        model_tool_calls = tuple(
            ModelToolCall(
                tool_call_id=tool_call.id,
                name=tool_call.function.name,
                arguments_json=tool_call.function.arguments,
            )
            for tool_call in (message.tool_calls or [])
        )
        return ModelResponse(
            assistant_message=message.model_dump(exclude_none=True),
            tool_calls=model_tool_calls,
            final_text=message.content,
            diagnostics={"response_type": type(response).__name__, "finish_reason": getattr(response.choices[0], "finish_reason", None),
                         "has_text": bool(message.content and message.content.strip()), "has_tool_calls": bool(model_tool_calls),
                         "response_retries": attempt, "empty_response_retries": attempt if request_messages == list(messages) else 0,
                         **response_details},
        )

    def complete_once(self, messages, tool_schemas):
        """收尾的单次请求；禁止 SDK 和输出重新生成重试。"""
        return self.complete(messages, tool_schemas, _single=True)

    def complete_structured_once(self, messages, response_type):
        return self.complete_structured(messages, response_type, _single=True)

    def complete_structured(
        self,
        messages: Sequence[Mapping[str, Any]],
        response_type: type[StructuredModel],
        *, _single: bool = False,
    ) -> StructuredModel:
        """使用 Schema 提示和本地校验获得结构化结果。"""

        request_messages = structured_messages(messages, response_type.model_json_schema())
        last_error: StructuredOutputError | None = None
        client = self.client.with_options(max_retries=0) if _single and hasattr(self.client, "with_options") else self.client

        # 第一次失败时把校验错误作为 Observation，让模型修正一次。
        for attempt in range(1 if _single else 2):
            self._check_budget(request_messages, [])
            response = client.chat.completions.create(
                model=self.settings.model,
                messages=request_messages,
                max_tokens=self.settings.max_output_tokens,
                **self.settings.generation_options(),
            )
            details = self._observe(response)
            if not getattr(response, "choices", None):
                raise StructuredOutputError(f"模型返回非 ChatCompletion 响应：type={type(response).__name__}，缺少 choices")
            content = response.choices[0].message.content
            if getattr(response.choices[0], "finish_reason", None) == "length":
                last_error = StructuredOutputError("模型输出被截断，请缩短内容")
                content = None
            elif content is not None:
                try:
                    return parse_structured_output(content, response_type)
                except StructuredOutputError as error:
                    last_error = error
            else:
                last_error = StructuredOutputError("模型返回内容为空")

            if attempt == 0:
                request_messages.extend(
                    [
                        {"role": "assistant", "content": content or ""},
                        {
                            "role": "user",
                            "content": (
                                "上一个输出未通过结构校验："
                                f"{last_error}。"
                                "请仅返回修正后的 JSON 对象。"
                            ),
                        },
                    ]
                )

        raise StructuredOutputError(
            f"模型在 {'1' if _single else '2'} 次请求内未返回合法结构化结果：{last_error}", details
        )

    def _check_budget(self, messages, tools) -> None:
        # 包括结构化重试追加的内容；供应商实际 tokenizer 仍可能有差异。
        from tikiagent.context.compression.monitor import CharacterTokenEstimator

        usage = CharacterTokenEstimator().estimate({"messages": list(messages), "tools": tools})
        if usage + self.settings.max_output_tokens + self.settings.safety_margin > self.settings.context_limit:
            raise ValueError("最终模型请求超过输入预算，已阻止发送")
