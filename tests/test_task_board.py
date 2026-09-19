"""Task Board 多 Todo 与身份链状态转换测试。"""

import pytest

from tikiagent.context.models import TaskBoard
from tikiagent.context.task_board import (
    TaskBoardTransitionError,
    add_todo,
    next_actionable_todo,
    record_result,
    record_verification,
    start_todo,
    todos_for_owner,
)


def board_with_two_code_todos() -> TaskBoard:
    board = add_todo(
        TaskBoard(),
        todo_id="code-1",
        owner="code_agent",
        description="创建页面",
    )
    return add_todo(
        board,
        todo_id="code-2",
        owner="code_agent",
        description="补充测试",
    )


def test_same_agent_can_own_multiple_todos() -> None:
    board = board_with_two_code_todos()

    assert [item.todo_id for item in todos_for_owner(board, "code_agent")] == [
        "code-1",
        "code-2",
    ]
    assert next_actionable_todo(board, "code_agent").todo_id == "code-1"


def test_todo_tracks_handoff_result_and_verification() -> None:
    board = board_with_two_code_todos()
    board = start_todo(board, todo_id="code-1", handoff_id="handoff-1")
    board = record_result(
        board,
        todo_id="code-1",
        handoff_id="handoff-1",
        result_id="result-1",
    )
    board = record_verification(
        board,
        todo_id="code-1",
        handoff_id="handoff-1",
        result_id="result-1",
        verification_id="verification-1",
        passed=True,
    )

    item = board.items["code-1"]
    assert item.status == "completed"
    assert item.attempts == 1
    assert item.handoff_id == "handoff-1"
    assert item.result_id == "result-1"
    assert item.verification_id == "verification-1"
    assert next_actionable_todo(board, "code_agent").todo_id == "code-2"


def test_failed_todo_can_retry_with_new_identity_chain() -> None:
    board = board_with_two_code_todos()
    board = start_todo(board, todo_id="code-1", handoff_id="handoff-1")
    board = record_result(
        board,
        todo_id="code-1",
        handoff_id="handoff-1",
        result_id="result-1",
    )
    board = record_verification(
        board,
        todo_id="code-1",
        handoff_id="handoff-1",
        result_id="result-1",
        verification_id="verification-1",
        passed=False,
    )
    board = start_todo(board, todo_id="code-1", handoff_id="handoff-2")

    item = board.items["code-1"]
    assert item.status == "in_progress"
    assert item.attempts == 2
    assert item.handoff_id == "handoff-2"
    assert item.result_id is None
    assert item.verification_id is None


def test_result_must_match_current_handoff() -> None:
    board = start_todo(
        board_with_two_code_todos(),
        todo_id="code-1",
        handoff_id="handoff-1",
    )

    with pytest.raises(TaskBoardTransitionError, match="不匹配"):
        record_result(
            board,
            todo_id="code-1",
            handoff_id="older-handoff",
            result_id="result-1",
        )
