"""LangGraph 工作流的结构化 TikiState。"""

from typing import Annotated, Any, Literal, TypedDict
from uuid import uuid4

from tikiagent.orchestration.models import (
    ActorResult,
    Plan,
    PlannerDecision,
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
    "failed",
]


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


class TikiState(TypedDict):
    """当前工作流快照；不是 History、Workspace 或完整 Memory。"""

    # Task identity
    task: str
    session_id: str

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

    # Runtime references and limits
    workspace_id: str
    step_count: int
    max_steps: int

    # Completion
    status: WorkflowStatus
    final_result: str | None


def create_initial_state(
    *,
    task: str,
    system_prompt: str,
    workspace_id: str,
    max_steps: int,
    session_id: str | None = None,
) -> TikiState:
    """创建字段完整、可直接传入 Graph 的初始状态。"""

    if max_steps < 1:
        raise ValueError("max_steps 必须大于 0")

    return {
        "task": task,
        "session_id": session_id or str(uuid4()),
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
        "workspace_id": workspace_id,
        "step_count": 0,
        "max_steps": max_steps,
        "status": "running",
        "final_result": None,
    }


def create_plan_verify_state(
    *,
    task: str,
    workspace_id: str,
    max_steps: int,
    max_attempts: int,
    session_id: str | None = None,
) -> TikiState:
    """创建外层 Plan → Execute → Verify 工作流状态。"""

    if max_steps < 1:
        raise ValueError("max_steps 必须大于 0")
    if max_attempts < 1:
        raise ValueError("max_attempts 必须大于 0")

    return {
        "task": task,
        "session_id": session_id or str(uuid4()),
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
        "workspace_id": workspace_id,
        "step_count": 0,
        "max_steps": max_steps,
        "status": "planning",
        "final_result": None,
    }
