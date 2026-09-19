"""Context 模型共享的校验基类与作用域类型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


ContextAgentName = Literal[
    "supervisor",
    "research_agent",
    "code_agent",
    "verifier",
]


HistoryRecordType = Literal[
    "handoff",
    "result",
    "verification",
    "note",
    "error",
    "artifact_ref",
]


TodoStatus = Literal[
    "pending",
    "in_progress",
    "awaiting_verification",
    "completed",
    "failed",
]


NotepadScope = Literal["global", "session", "task", "agent"]


class ContextModel(BaseModel):
    """Context Plane 模型统一拒绝未声明字段。"""

    model_config = ConfigDict(extra="forbid")
