"""Agent 基础上下文、任务视图与模型调用结构。"""

from __future__ import annotations

from typing import Any, Literal
import json

from pydantic import Field

from tikiagent.context.compression.models import ContextUsage
from tikiagent.context.memory.models import (
    HistoryRecord,
    LocalMemory,
    NotepadEntry,
    RetrievalPolicy,
)
from tikiagent.context.schema import (
    ContextAgentName,
    ContextModel,
    HistoryRecordType,
    TodoStatus,
)


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
