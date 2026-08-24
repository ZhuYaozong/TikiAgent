"""LangGraph 工作流的结构化 TikiState。"""

from typing import Annotated, Any, Literal, TypedDict
from uuid import uuid4

from pydantic import TypeAdapter

from tikiagent.context.models import FinalizationReport, TaskBoard
from tikiagent.orchestration.models import (
    ActorResult,
    Handoff,
    Plan,
    PlannerDecision,
    SpecialistName,
    SupervisorDecision,
    SupervisorPlan,
    VerificationReport,
)


MessagePayload = dict[str, Any]
ToolResultPayload = dict[str, Any]
WorkflowStatus = Literal[
    "running",
    "planning",
    "executing",
    "verifying",
    "completed",
    "max_steps",
    "max_attempts",
    "delegating",
    "validating",
    "finalizing",
    "stopped",
    "failed",
    "awaiting_approval",
    "recovery_required",
    "awaiting_reconcile",
]

RECENT_EVENT_LIMIT = 50


class PendingToolCall(TypedDict):
    """Actor 已请求、等待 Tools Node 执行的调用。"""

    tool_call_id: str
    name: str
    arguments_json: str


def append_messages(
    current: list[MessagePayload],
    updates: list[MessagePayload],
) -> list[MessagePayload]:
    """把 Node 返回的新消息追加到当前工作消息。"""

    return current + updates


def append_tool_results(
    current: list[ToolResultPayload],
    updates: list[ToolResultPayload],
) -> list[ToolResultPayload]:
    """累积本次工作流已经产生的工具结果。"""

    return current + updates


def keep_recent_events(
    current: list[str],
    updates: list[str],
) -> list[str]:
    """临时 Debug 数据只保留最近事件，未来迁移到 Trace。"""

    return (current + updates)[-RECENT_EVENT_LIMIT:]


def merge_specialist_results(
    current: dict[SpecialistName, dict[str, Any]],
    updates: dict[SpecialistName, dict[str, Any]],
) -> dict[SpecialistName, dict[str, Any]]:
    """每个 Specialist 只保存最新 Result。"""

    return current | updates


def merge_specialist_verifications(
    current: dict[SpecialistName, VerificationReport],
    updates: dict[SpecialistName, VerificationReport],
) -> dict[SpecialistName, VerificationReport]:
    """每个 Specialist 只保存最新 VerificationReport。"""

    return current | updates


class TikiState(TypedDict):
    """当前工作流快照；不是 History、Workspace 或完整 Memory。"""

    # Task identity
    task_id: str
    task: str
    session_id: str
    # 应用层显式选择、允许当前 Task 复用的同 Session History 引用。
    session_context_refs: list[str]

    # Current Agent working messages
    messages: Annotated[list[MessagePayload], append_messages]

    # Current action and accumulated observations
    pending_tool_calls: list[PendingToolCall]
    tool_results: Annotated[list[ToolResultPayload], append_tool_results]

    # Planning and verification
    plan: Plan | None
    acceptance_criteria: list[str]
    planner_decision: PlannerDecision | None
    actor_instruction: str
    actor_result: ActorResult | None
    verification_report: VerificationReport | None
    attempts: int
    max_attempts: int

    # Multi-Agent control and collaboration
    current_agent: str
    supervisor_plan: SupervisorPlan | None
    required_specialists: list[SpecialistName]
    supervisor_decision: SupervisorDecision | None
    delegation_count: int
    max_delegations: int
    latest_handoff: Handoff | None
    task_board: TaskBoard
    specialist_results: Annotated[
        dict[SpecialistName, dict[str, Any]],
        merge_specialist_results,
    ]
    specialist_verifications: Annotated[
        dict[SpecialistName, VerificationReport],
        merge_specialist_verifications,
    ]
    recent_events: Annotated[list[str], keep_recent_events]
    history_cursor: int

    # Harness Resume 只保存引用；真正恢复必须从 Graph Resume Entry 重入。
    resume_request: dict[str, Any] | None
    runtime_checkpoint_id: str | None
    runtime_checkpoint_revision: int | None
    trace_cursor: int

    # Runtime references and limits
    workspace_id: str
    step_count: int
    max_steps: int

    # Completion
    status: WorkflowStatus
    final_result: str | None
    final_result_id: str | None
    finalization_report: FinalizationReport | None


_TIKI_STATE_ADAPTER = TypeAdapter(TikiState)


def serialize_tiki_state(state: TikiState) -> dict[str, Any]:
    """把 Pydantic 子模型转成可写入 Checkpoint 的 JSON 快照。"""

    return _TIKI_STATE_ADAPTER.dump_python(state, mode="json")


def restore_tiki_state(payload: dict[str, Any]) -> TikiState:
    """严格按 canonical TikiState schema 恢复 Graph 输入。"""

    # v0.6a2 Checkpoint 尚无该字段；迁移时只补空引用，不猜测历史。
    migrated = {"session_context_refs": [], **payload}
    return _TIKI_STATE_ADAPTER.validate_python(migrated)


def create_initial_state(
    *,
    task: str,
    system_prompt: str,
    workspace_id: str,
    max_steps: int,
    session_id: str | None = None,
    task_id: str | None = None,
    session_context_refs: list[str] | None = None,
) -> TikiState:
    """创建字段完整、可直接传入 Graph 的初始状态。"""

    if max_steps < 1:
        raise ValueError("max_steps 必须大于 0")

    return {
        "task_id": task_id or str(uuid4()),
        "task": task,
        "session_id": session_id or str(uuid4()),
        "session_context_refs": list(dict.fromkeys(session_context_refs or [])),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ],
        "pending_tool_calls": [],
        "tool_results": [],
        "plan": None,
        "acceptance_criteria": [],
        "planner_decision": None,
        "actor_instruction": "",
        "actor_result": None,
        "verification_report": None,
        "attempts": 0,
        "max_attempts": 1,
        "current_agent": "react_agent",
        "supervisor_plan": None,
        "required_specialists": [],
        "supervisor_decision": None,
        "delegation_count": 0,
        "max_delegations": 1,
        "latest_handoff": None,
        "task_board": TaskBoard(),
        "specialist_results": {},
        "specialist_verifications": {},
        "recent_events": [],
        "history_cursor": 0,
        "resume_request": None,
        "runtime_checkpoint_id": None,
        "runtime_checkpoint_revision": None,
        "trace_cursor": 0,
        "workspace_id": workspace_id,
        "step_count": 0,
        "max_steps": max_steps,
        "status": "running",
        "final_result": None,
        "final_result_id": None,
        "finalization_report": None,
    }


def create_plan_verify_state(
    *,
    task: str,
    workspace_id: str,
    max_steps: int,
    max_attempts: int,
    session_id: str | None = None,
    task_id: str | None = None,
    session_context_refs: list[str] | None = None,
) -> TikiState:
    """创建外层 Plan → Execute → Verify 工作流状态。"""

    if max_steps < 1:
        raise ValueError("max_steps 必须大于 0")
    if max_attempts < 1:
        raise ValueError("max_attempts 必须大于 0")

    return {
        "task_id": task_id or str(uuid4()),
        "task": task,
        "session_id": session_id or str(uuid4()),
        "session_context_refs": list(dict.fromkeys(session_context_refs or [])),
        "messages": [],
        "pending_tool_calls": [],
        "tool_results": [],
        "plan": None,
        "acceptance_criteria": [],
        "planner_decision": None,
        "actor_instruction": "",
        "actor_result": None,
        "verification_report": None,
        "attempts": 0,
        "max_attempts": max_attempts,
        "current_agent": "planner",
        "supervisor_plan": None,
        "required_specialists": [],
        "supervisor_decision": None,
        "delegation_count": 0,
        "max_delegations": 1,
        "latest_handoff": None,
        "task_board": TaskBoard(),
        "specialist_results": {},
        "specialist_verifications": {},
        "recent_events": [],
        "history_cursor": 0,
        "resume_request": None,
        "runtime_checkpoint_id": None,
        "runtime_checkpoint_revision": None,
        "trace_cursor": 0,
        "workspace_id": workspace_id,
        "step_count": 0,
        "max_steps": max_steps,
        "status": "planning",
        "final_result": None,
        "final_result_id": None,
        "finalization_report": None,
    }


def create_multi_agent_state(
    *,
    task: str,
    workspace_id: str,
    max_steps: int,
    max_delegations: int,
    session_id: str | None = None,
    task_id: str | None = None,
    session_context_refs: list[str] | None = None,
) -> TikiState:
    """使用同一个 canonical TikiState 创建 Multi-Agent 初始状态。"""

    if max_steps < 1:
        raise ValueError("max_steps 必须大于 0")
    if max_delegations < 1:
        raise ValueError("max_delegations 必须大于 0")

    state = create_initial_state(
        task=task,
        system_prompt="",
        workspace_id=workspace_id,
        max_steps=max_steps,
        session_id=session_id,
        task_id=task_id,
        session_context_refs=session_context_refs,
    )
    return {
        **state,
        "messages": [],
        "current_agent": "supervisor",
        "max_delegations": max_delegations,
        "status": "running",
    }
