"""Context Plane 使用的结构化模型。"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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
    allowed_record_types: set[HistoryRecordType]
    include_global_task_board: bool = False
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
    relevant_history: list[HistoryRecord] = Field(default_factory=list)


class BaseContext(ContextModel):
    """ContextBuilder 生成的基础上下文，不包含单次 ReAct 内部 messages。"""

    agent: ContextAgentName
    role: str = Field(min_length=1)
    system_rules: list[str] = Field(min_length=1)
    working_memory: WorkingMemory

    def render(self) -> str:
        """使用稳定分层格式渲染；动态 Prompt 留到 Context II。"""

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
        return "\n".join(
            [
                f"角色：{self.role}",
                "规则：" + json.dumps(self.system_rules, ensure_ascii=False),
                f"任务：{memory.task}",
                f"阶段：{memory.phase}",
                f"当前指令：{memory.instruction}",
                "验收标准："
                + json.dumps(memory.acceptance_criteria, ensure_ascii=False),
                "Task Board：" + json.dumps(todos, ensure_ascii=False),
                "相关历史：" + json.dumps(history, ensure_ascii=False),
            ]
        )
