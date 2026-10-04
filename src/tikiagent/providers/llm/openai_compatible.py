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


class OpenAICompatibleClient:
    """可通过配置连接 DeepSeek、vLLM 等兼容后端。"""

    def __init__(
        self,
        settings: ModelSettings,
        client: Any | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.timeout_seconds,
            max_retries=settings.max_retries,
        )

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
    ) -> ModelResponse:
        self._check_budget(messages, self._convert_tools(tool_schemas))
        # 仅重试空响应一次；此处没有执行工具，不会重放已有副作用。
        for attempt in range(2):
            response = self.client.chat.completions.create(
                model=self.settings.model, messages=list(messages),
                tools=self._convert_tools(tool_schemas), tool_choice="auto",
                max_tokens=self.settings.max_output_tokens,
            )
            if not getattr(response, "choices", None):
                raise ValueError(f"模型返回非 ChatCompletion 响应：type={type(response).__name__}，缺少 choices")
            message = response.choices[0].message
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
                         "has_text": bool(message.content and message.content.strip()), "has_tool_calls": bool(model_tool_calls), "empty_response_retries": attempt},
        )

    def complete_structured(
        self,
        messages: Sequence[Mapping[str, Any]],
        response_type: type[StructuredModel],
    ) -> StructuredModel:
        """使用 Schema 提示和本地校验获得结构化结果。"""

        request_messages = structured_messages(messages, response_type.model_json_schema())
        last_error: StructuredOutputError | None = None

        # 第一次失败时把校验错误作为 Observation，让模型修正一次。
        for attempt in range(2):
            self._check_budget(request_messages, [])
            response = self.client.chat.completions.create(
                model=self.settings.model,
                messages=request_messages,
                max_tokens=self.settings.max_output_tokens,
            )
            if not getattr(response, "choices", None):
                raise StructuredOutputError(f"模型返回非 ChatCompletion 响应：type={type(response).__name__}，缺少 choices")
            content = response.choices[0].message.content
            if content is not None:
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
            f"模型连续两次未返回合法结构化结果：{last_error}"
        )

    def _check_budget(self, messages, tools) -> None:
        # 包括结构化重试追加的内容；供应商实际 tokenizer 仍可能有差异。
        from tikiagent.context.compression.monitor import CharacterTokenEstimator

        usage = CharacterTokenEstimator().estimate({"messages": list(messages), "tools": tools})
        if usage + self.settings.max_output_tokens > self.settings.context_limit:
            raise ValueError("最终模型请求超过输入预算，已阻止发送")
