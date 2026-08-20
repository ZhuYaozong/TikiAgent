"""基于 Chat Completions 的 OpenAI-compatible 模型适配器。"""

from collections.abc import Mapping, Sequence
from typing import Any

from openai import OpenAI

from tikiagent.llm.config import ModelSettings
from tikiagent.llm.models import ModelResponse, ModelToolCall


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
