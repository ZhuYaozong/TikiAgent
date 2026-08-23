"""Context Plane 使用的结构化模型。"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class TodoItem(ContextModel):
    """Task Board 中一个独立工作项；同一 Agent 可以拥有多个 Todo。"""

    todo_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    owner: ContextAgentName
    status: TodoStatus = "pending"
    attempts: int = Field(default=0, ge=0)
    handoff_id: str | None = None
    result_id: str | None = None
    verification_id: str | None = None


class TaskBoard(ContextModel):
    """当前任务的结构化进度快照，不保存在自然语言 Messages 中。"""

    items: dict[str, TodoItem] = Field(default_factory=dict)


class RetrievalPolicy(ContextModel):
    """每个 Context Profile 可以调整默认检索策略。"""

    supplement_keyword: bool = False
    supplement_recent: bool = False
    keyword_limit: int = Field(default=4, ge=0)
    recent_limit: int = Field(default=3, ge=0)
    max_records: int = Field(default=8, ge=1)


class ContextProfile(ContextModel):
    """一个 Agent 的 Context 选择和组装规则。"""

    agent: ContextAgentName
    role: str = Field(min_length=1)
    system_rules: list[str] = Field(min_length=1)
    phase_rules: dict[str, list[str]] = Field(default_factory=dict)
    tool_names_by_phase: dict[str, set[str]] = Field(default_factory=dict)
    allowed_record_types: set[HistoryRecordType]
    include_global_task_board: bool = False
    max_notepad_entries: int = Field(default=6, ge=0)
    retrieval: RetrievalPolicy = Field(default_factory=RetrievalPolicy)


class ContextRequest(ContextModel):
    """当前节点向 Context Engine 发出的上下文请求。"""

    agent: ContextAgentName
    task_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    phase: str = Field(min_length=1)
    instruction: str = Field(min_length=1)
    context_refs: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class WorkingMemory(ContextModel):
    """当前 Agent 完成当前步骤所需的任务相关信息集合。"""

    task: str = Field(min_length=1)
    phase: str = Field(min_length=1)
    instruction: str = Field(min_length=1)
    acceptance_criteria: list[str] = Field(default_factory=list)
    todos: list[TodoItem] = Field(default_factory=list)
    history_summary: str | None = None
    relevant_history: list[HistoryRecord] = Field(default_factory=list)
    relevant_notepad: list[NotepadEntry] = Field(default_factory=list)
    protected_refs: list[str] = Field(default_factory=list)


class BaseContext(ContextModel):
    """当前拿什么工作；行为规则属于 PromptBundle。"""

    agent: ContextAgentName
    working_memory: WorkingMemory

    def render(self) -> str:
        """只渲染任务数据，不重复注入角色和系统规则。"""

        memory = self.working_memory
        todos = [item.model_dump(mode="json") for item in memory.todos]
        history = [
            {
                "record_id": item.record_id,
                "record_type": item.record_type,
                "producer": item.producer,
                "summary": item.summary,
                "payload": item.payload,
                "refs": item.refs,
            }
            for item in memory.relevant_history
        ]
        notes = [
            {
                "note_id": item.note_id,
                "content": item.content,
                "scope": item.scope,
                "source_refs": item.source_refs,
            }
            for item in memory.relevant_notepad
        ]
        return "\n".join(
            [
                f"任务：{memory.task}",
                f"阶段：{memory.phase}",
                f"当前指令：{memory.instruction}",
                "验收标准："
                + json.dumps(memory.acceptance_criteria, ensure_ascii=False),
                "Task Board：" + json.dumps(todos, ensure_ascii=False),
                f"历史摘要：{memory.history_summary or 'null'}",
                "相关历史：" + json.dumps(history, ensure_ascii=False),
                "相关 Notepad：" + json.dumps(notes, ensure_ascii=False),
            ]
        )


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


class PromptBundle(ContextModel):
    """当前应该怎样工作，与 BaseContext 的任务数据分离。"""

    stable_system_rules: list[str]
    agent_role: str = Field(min_length=1)
    phase_rules: list[str] = Field(default_factory=list)

    def render(self) -> str:
        return "\n".join(
            [
                "稳定规则："
                + json.dumps(self.stable_system_rules, ensure_ascii=False),
                f"Agent 角色：{self.agent_role}",
                "阶段规则："
                + json.dumps(self.phase_rules, ensure_ascii=False),
            ]
        )


class ToolView(ContextModel):
    """本轮向模型暴露的工具；它不是 Permission 决策。"""

    exposed_names: set[str] = Field(default_factory=set)
    schemas: list[dict[str, Any]] = Field(default_factory=list)


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


class ContextBudget(ContextModel):
    """模型窗口预算；输入必须为输出预留生成空间。"""

    model_context_limit: int = Field(default=32_000, gt=0)
    reserved_output_tokens: int = Field(default=2_000, ge=0)
    base_context_budget: int = Field(default=12_000, gt=0)
    local_messages_budget: int = Field(default=10_000, gt=0)
    recent_interaction_limit: int = Field(default=4, gt=0)

    @model_validator(mode="after")
    def validate_output_reservation(self) -> ContextBudget:
        if self.reserved_output_tokens >= self.model_context_limit:
            raise ValueError("reserved_output_tokens 必须小于模型窗口")
        return self

    @property
    def available_input_budget(self) -> int:
        return self.model_context_limit - self.reserved_output_tokens


class ContextUsage(ContextModel):
    """Candidate 或 Prepared Model Call 的完整输入估算。"""

    prompt_tokens: int
    base_tokens: int
    history_tokens: int
    notepad_tokens: int
    local_tokens: int
    tool_schema_tokens: int
    response_schema_tokens: int
    total_call_usage: int
    available_input_budget: int
    base_over_budget: bool
    local_over_budget: bool
    total_over_budget: bool


class CandidateModelCall(ContextModel):
    """压缩前准备发送的一次完整模型调用。"""

    prompt: PromptBundle
    base_context: BaseContext
    local_memory: LocalMemory
    tool_view: ToolView
    response_schema: dict[str, Any] | None = None
    messages: list[dict[str, Any]]


class PreparedModelCall(CandidateModelCall):
    """经过预算检查和必要压缩、可以真正发送的调用。"""

    usage: ContextUsage
    compression_actions: list[Literal["base", "local"]] = Field(
        default_factory=list
    )
    notepad_candidates: list[NotepadEntry] = Field(default_factory=list)


class FinalizationReport(ContextModel):
    """幂等 Finalization 的持久化与清理结果。"""

    task_id: str
    final_result_id: str
    history_record_id: str
    already_finalized: bool
    cleared_runtime_fields: list[str]
