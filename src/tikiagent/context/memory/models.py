"""可检索历史、长期笔记和局部交互的数据模型。"""

from __future__ import annotations

from typing import Any

from pydantic import Field, model_validator

from tikiagent.context.schema import (
    ContextAgentName,
    ContextModel,
    HistoryRecordType,
    NotepadScope,
)


class HistoryRecord(ContextModel):
    """未来 Agent 可以再次检索的一条结构化历史。"""

    record_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    record_type: HistoryRecordType
    producer: ContextAgentName
    summary: str = Field(min_length=1, max_length=8000)
    payload: dict[str, Any] = Field(default_factory=dict)
    refs: list[str] = Field(default_factory=list)
    sequence: int = Field(default=0, ge=0)


class RetrievalPolicy(ContextModel):
    """每个 Context Profile 可以调整默认检索策略。"""

    supplement_keyword: bool = False
    supplement_recent: bool = False
    keyword_limit: int = Field(default=4, ge=0)
    recent_limit: int = Field(default=3, ge=0)
    max_records: int = Field(default=8, ge=1)


class NotepadEntry(ContextModel):
    """经过批准、可跨步骤重新检索的长期事实。"""

    note_id: str = Field(min_length=1)
    content: str = Field(min_length=1, max_length=4000)
    scope: NotepadScope
    source_refs: list[str] = Field(min_length=1)
    approved: bool = False
    task_id: str | None = None
    session_id: str | None = None
    agent_scope: set[ContextAgentName] = Field(default_factory=set)

    @model_validator(mode="after")
    def validate_scope_identity(self) -> NotepadEntry:
        if self.scope == "task" and not self.task_id:
            raise ValueError("task scope 的 Notepad 必须提供 task_id")
        if self.scope == "session" and not self.session_id:
            raise ValueError("session scope 的 Notepad 必须提供 session_id")
        if self.scope == "agent" and not self.agent_scope:
            raise ValueError("agent scope 的 Notepad 必须提供 agent_scope")
        return self


class ReActInteraction(ContextModel):
    """不可拆分的一轮 Assistant ToolCall 与全部 ToolResult。"""

    interaction_id: str = Field(min_length=1)
    assistant_message: dict[str, Any]
    tool_messages: list[dict[str, Any]] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_tool_call_pairs(self) -> ReActInteraction:
        raw_calls = self.assistant_message.get("tool_calls")
        if not isinstance(raw_calls, list) or not raw_calls:
            raise ValueError("ReActInteraction 必须包含 Assistant ToolCall")
        call_ids = [
            item.get("id")
            for item in raw_calls
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        result_ids = [
            item.get("tool_call_id")
            for item in self.tool_messages
            if isinstance(item.get("tool_call_id"), str)
        ]
        if len(call_ids) != len(raw_calls) or sorted(call_ids) != sorted(result_ids):
            raise ValueError("ToolCall 与 ToolResult 的 tool_call_id 必须完整配对")
        return self


class LocalMemory(ContextModel):
    """单次 Agent.run() 私有的短期 ReAct 上下文。"""

    summary: str | None = None
    recent_interactions: list[ReActInteraction] = Field(default_factory=list)
