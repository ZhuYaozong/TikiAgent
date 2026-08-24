"""Application Plane 的结构化模型。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


Intent = Literal["CHAT", "WORKFLOW"]
ApplicationStatus = Literal[
    "session_created",
    "chat_completed",
    "awaiting_approval",
    "recovery_required",
    "awaiting_reconcile",
    "workflow_completed",
    "workflow_denied",
    "workflow_failed",
    "active",
]
WorkflowOutcomeStatus = Literal[
    "awaiting_approval",
    "recovery_required",
    "awaiting_reconcile",
    "completed",
    "denied",
    "failed",
]
ApplicationEventType = Literal[
    "session_started",
    "turn_received",
    "intent_routed",
    "workflow_started",
    "workflow_resumed",
    "supervisor_decision",
    "agent_started",
    "handoff_created",
    "specialist_result",
    "verification_completed",
    "workflow_retried",
    "workflow_completed",
    "workflow_denied",
    "tool_call_requested",
    "approval_required",
    "approval_decided",
    "tool_execution_started",
    "tool_result_received",
    "recovery_required",
    "recovery_decided",
    "reconciliation_completed",
    "final_answer",
]


def utc_now() -> datetime:
    return datetime.now(UTC)


class ApplicationModel(BaseModel):
    """应用层模型统一拒绝未知字段。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class SessionRecord(ApplicationModel):
    """Session 只保存应用身份、计数和活动 Checkpoint 引用。"""

    session_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    workspace_id: str = Field(min_length=1)
    revision: int = Field(default=1, ge=1)
    turn_count: int = Field(default=0, ge=0)
    last_task_id: str | None = None
    active_checkpoint_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class IntentDecision(ApplicationModel):
    """只决定是否进入 Workflow，不承担 Supervisor 的规划职责。"""

    intent: Intent
    reason: str = Field(min_length=1)


class TurnRecord(ApplicationModel):
    """一轮用户输入及其入口路由结果。"""

    record_type: Literal["turn"] = "turn"
    turn_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    session_id: str = Field(min_length=1)
    task_id: str | None = None
    user_input: str = Field(min_length=1)
    decision: IntentDecision
    created_at: datetime = Field(default_factory=utc_now)


class ResponseRecord(ApplicationModel):
    """同一 Turn 可以先暂停，之后再追加 Resume 的最终响应。"""

    record_type: Literal["response"] = "response"
    response_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    turn_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    task_id: str | None = None
    status: ApplicationStatus
    content: str = Field(min_length=1)
    checkpoint_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class EventScope(ApplicationModel):
    session_id: str = Field(min_length=1)
    workspace_id: str | None = None
    turn_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    checkpoint_id: str | None = None


class ApplicationEvent(ApplicationModel):
    """面向 CLI/UI/API 的实时语义事件，不是恢复事实。"""

    event_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    stream_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    timestamp: datetime = Field(default_factory=utc_now)
    event_type: ApplicationEventType
    scope: EventScope
    source: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    causation_id: str | None = None
    message: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)


class ApplicationOutcome(ApplicationModel):
    """ApplicationController 返回给 CLI/API 的稳定结果。"""

    status: ApplicationStatus
    session_id: str
    message: str
    turn_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    checkpoint_id: str | None = None
    checkpoint_revision: int | None = Field(default=None, ge=1)
    approval_request_id: str | None = None
    execution_id: str | None = None
    attempt: int | None = Field(default=None, ge=1)
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_result: dict[str, Any] | None = None


class WorkflowOutcome(ApplicationModel):
    """Workflow Adapter 暴露给 Controller 的稳定结果。"""

    status: WorkflowOutcomeStatus
    task_id: str
    session_id: str
    workspace_id: str
    message: str
    final_result: str | None = None
    run_id: str | None = None
    checkpoint_id: str | None = None
    checkpoint_revision: int | None = Field(default=None, ge=1)
    approval_request_id: str | None = None
    approval_fingerprint: str | None = None
    execution_id: str | None = None
    attempt: int | None = Field(default=None, ge=1)
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_result: dict[str, Any] | None = None


class ReconcileSubmission(ApplicationModel):
    """CLI 提交的人工核对结果；它必须是实际观察所得。"""

    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    ok: bool
    output: Any = None
    error: dict[str, Any] | None = None
    reconciled_by: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_result_shape(self) -> "ReconcileSubmission":
        if self.ok and self.error is not None:
            raise ValueError("成功 Reconcile Result 不能包含 error")
        if not self.ok and self.error is None:
            raise ValueError("失败 Reconcile Result 必须包含结构化 error")
        return self
