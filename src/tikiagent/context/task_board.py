"""Task Board 的不可变状态转换函数。"""

from __future__ import annotations

from uuid import uuid4
from typing import Literal

from tikiagent.context.models import TaskBoard, TodoItem
from tikiagent.context.schema import ContextAgentName
from tikiagent.orchestration.requirements import ResultReview


class TaskBoardTransitionError(RuntimeError):
    """Todo 生命周期转换不合法。"""


def create_task_board(
    *,
    task: str,
    owners: list[ContextAgentName],
    code_task_mode: Literal["artifact", "inspection"] = "artifact",
) -> TaskBoard:
    """根据 SupervisorPlan 创建第一批 Todo，同时保留多 Todo 扩展能力。"""

    board = TaskBoard()
    for owner in owners:
        board = add_todo(
            board,
            description=f"{owner} 完成任务：{task}",
            owner=owner,
            delivery_mode=code_task_mode if owner == "code_agent" else "artifact",
        )
    return board


def add_todo(
    board: TaskBoard,
    *,
    description: str,
    owner: ContextAgentName,
    todo_id: str | None = None,
    delivery_mode: Literal["artifact", "inspection"] = "artifact",
    depends_on: list[str] | None = None,
) -> TaskBoard:
    """添加工作项；同一个 owner 可以拥有多个 Todo。"""

    item = TodoItem(
        todo_id=todo_id or str(uuid4()),
        description=description,
        owner=owner,
        delivery_mode=delivery_mode,
        depends_on=depends_on or [],
    )
    if item.todo_id in board.items:
        raise ValueError(f"Todo 已存在：{item.todo_id}")
    return board.model_copy(update={"items": board.items | {item.todo_id: item}})


def todos_for_owner(
    board: TaskBoard,
    owner: ContextAgentName,
) -> list[TodoItem]:
    return [item for item in board.items.values() if item.owner == owner]


def todos_for_refs(board: TaskBoard, refs: list[str]) -> list[TodoItem]:
    """Verifier 根据 Handoff/Result 引用找到被验证的 Todo。"""

    requested = set(refs)
    return [
        item
        for item in board.items.values()
        if requested
        & {
            item.todo_id,
            item.handoff_id or "",
            item.result_id or "",
            item.verification_id or "",
            item.review.review_id if item.review else "",
        }
    ]


def next_actionable_todo(
    board: TaskBoard,
    owner: ContextAgentName,
) -> TodoItem | None:
    """按插入顺序选择同一 Agent 下一项 pending/failed 工作。"""

    return next(
        (
            item
            for item in todos_for_owner(board, owner)
            if item.status in {"pending", "failed"}
        ),
        None,
    )


def start_todo(
    board: TaskBoard,
    *,
    todo_id: str,
    handoff_id: str,
) -> TaskBoard:
    item = _require_item(board, todo_id)
    if item.status not in {"pending", "failed"}:
        raise TaskBoardTransitionError(
            f"Todo {todo_id} 不能从 {item.status} 开始执行"
        )
    if any(board.items[dependency].status != "completed" for dependency in item.depends_on):
        raise TaskBoardTransitionError("依赖 Todo 尚未完成")
    return _replace(
        board,
        item.model_copy(
            update={
                "status": "in_progress",
                "attempts": item.attempts + 1,
                "handoff_id": handoff_id,
                "result_id": None,
                "verification_id": None,
                "review": None,
            }
        ),
    )


def record_result(
    board: TaskBoard,
    *,
    todo_id: str,
    handoff_id: str,
    result_id: str,
) -> TaskBoard:
    item = _require_item(board, todo_id)
    if item.status != "in_progress" or item.handoff_id != handoff_id:
        raise TaskBoardTransitionError(
            f"Todo {todo_id} 的 Result 与当前 Handoff 不匹配"
        )
    return _replace(
        board,
        item.model_copy(
            update={
                "status": "awaiting_verification",
                "result_id": result_id,
            }
        ),
    )


def record_verification(
    board: TaskBoard,
    *,
    todo_id: str,
    handoff_id: str,
    result_id: str,
    verification_id: str,
    passed: bool,
    await_supervisor: bool = False,
) -> TaskBoard:
    item = _require_item(board, todo_id)
    if (
        item.status != "awaiting_verification"
        or item.handoff_id != handoff_id
        or item.result_id != result_id
    ):
        raise TaskBoardTransitionError(
            f"Todo {todo_id} 的 Verification 与最新 Result 不匹配"
        )
    return _replace(
        board,
        item.model_copy(
            update={
                "status": "awaiting_review" if await_supervisor else "completed" if passed else "failed",
                "verification_id": verification_id,
            }
        ),
    )


def record_review(board: TaskBoard, review: ResultReview) -> TaskBoard:
    """只对当前待验收结果提交一次决定；新执行会清除旧决定。"""
    item = _require_item(board, review.todo_id)
    if (item.status != "awaiting_review" or item.result_id != review.result_id
            or item.handoff_id != review.handoff_id or item.verification_id != review.verification_id):
        raise TaskBoardTransitionError("验收决定与当前待验收 Result/Handoff/Verification 不匹配")
    return _replace(board, item.model_copy(update={
        "status": "completed" if review.action in {"accept", "accept_with_limitations"} else "failed",
        "review": review,
    }))


def _require_item(board: TaskBoard, todo_id: str) -> TodoItem:
    item = board.items.get(todo_id)
    if item is None:
        raise KeyError(f"Todo 不存在：{todo_id}")
    return item


def _replace(board: TaskBoard, item: TodoItem) -> TaskBoard:
    return board.model_copy(
        update={"items": board.items | {item.todo_id: item}}
    )
