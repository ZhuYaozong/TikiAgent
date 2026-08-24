"""不进入 Agent Workflow 的普通对话服务。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from tikiagent.llm.models import ModelClient


class ChatService(Protocol):
    def respond(
        self,
        user_input: str,
        *,
        recent_messages: Sequence[Mapping[str, str]],
    ) -> str: ...


class ModelChatService:
    """普通对话不暴露 Tool Schema，因此不能绕过 Workflow/Harness。"""

    def __init__(self, model: ModelClient) -> None:
        self.model = model

    def respond(
        self,
        user_input: str,
        *,
        recent_messages: Sequence[Mapping[str, str]],
    ) -> str:
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 TikiAgent 的普通对话入口。只回答问题，不调用工具，"
                    "不声称已经搜索、修改文件或执行命令。"
                ),
            },
            *[dict(item) for item in recent_messages],
            {"role": "user", "content": user_input},
        ]
        response = self.model.complete(messages, ())
        if not response.final_text:
            raise RuntimeError("CHAT 模型没有返回最终文本")
        return response.final_text
