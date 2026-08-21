"""基于 Chat Completions 的 OpenAI-compatible 模型适配器。"""

import json
from collections.abc import Mapping, Sequence
from typing import Any

from openai import OpenAI

from tikiagent.llm.config import ModelSettings
from tikiagent.llm.models import (
    ModelResponse,
    ModelToolCall,
    StructuredModel,
)
from tikiagent.llm.structured_output import (
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
        response = self.client.chat.completions.create(
            model=self.settings.model,
            messages=list(messages),
            tools=self._convert_tools(tool_schemas),
            tool_choice="auto",
        )
        message = response.choices[0].message
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
        )

    def complete_structured(
        self,
        messages: Sequence[Mapping[str, Any]],
        response_type: type[StructuredModel],
    ) -> StructuredModel:
        """使用 Schema 提示和本地校验获得结构化结果。"""

        schema = json.dumps(
            response_type.model_json_schema(),
            ensure_ascii=False,
            indent=2,
        )
        request_messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "只返回一个 JSON 对象，不要使用 Markdown。"
                    "输出必须满足以下 JSON Schema：\n"
                    f"{schema}"
                ),
            },
            *[dict(message) for message in messages],
        ]
        last_error: StructuredOutputError | None = None

        # 第一次失败时把校验错误作为 Observation，让模型修正一次。
        for attempt in range(2):
            response = self.client.chat.completions.create(
                model=self.settings.model,
                messages=request_messages,
            )
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
