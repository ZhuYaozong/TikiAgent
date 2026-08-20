"""LangGraph 工作流的结构化 TikiState。"""

from typing import Annotated, Any, Literal, TypedDict
from uuid import uuid4


MessagePayload = dict[str, Any]
ToolResultPayload = dict[str, Any]
WorkflowStatus = Literal[
    "running",
    "completed",
    "max_steps",
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
        "workspace_id": workspace_id,
        "step_count": 0,
        "max_steps": max_steps,
        "status": "running",
        "final_result": None,
    }
