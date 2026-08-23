"""Task Board 的不可变状态转换函数。"""

from __future__ import annotations

from uuid import uuid4

from tikiagent.context.models import (
    ContextAgentName,
    TaskBoard,
    TodoItem,
)


class TaskBoardTransitionError(RuntimeError):
    """Todo 生命周期转换不合法。"""


def create_task_board(
    *,
    task: str,
    owners: list[ContextAgentName],
) -> TaskBoard:
    """根据 SupervisorPlan 创建第一批 Todo，同时保留多 Todo 扩展能力。"""

    board = TaskBoard()
    for owner in owners:
        board = add_todo(
            board,
            description=f"{owner} 完成任务：{task}",
            owner=owner,
        )
    return board


def add_todo(
    board: TaskBoard,
    *,
    description: str,
    owner: ContextAgentName,
    todo_id: str | None = None,
) -> TaskBoard:
    """添加工作项；同一个 owner 可以拥有多个 Todo。"""

    item = TodoItem(
        todo_id=todo_id or str(uuid4()),
        description=description,
        owner=owner,
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
    return _replace(
        board,
        item.model_copy(
            update={
                "status": "in_progress",
                "attempts": item.attempts + 1,
                "handoff_id": handoff_id,
                "result_id": None,
                "verification_id": None,
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
                "status": "completed" if passed else "failed",
                "verification_id": verification_id,
            }
        ),
    )


def _require_item(board: TaskBoard, todo_id: str) -> TodoItem:
    item = board.items.get(todo_id)
    if item is None:
        raise KeyError(f"Todo 不存在：{todo_id}")
    return item


def _replace(board: TaskBoard, item: TodoItem) -> TaskBoard:
    return board.model_copy(
        update={"items": board.items | {item.todo_id: item}}
    )
