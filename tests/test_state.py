"""TikiState 初始值与 Reducer 测试。"""

import pytest

from tikiagent.orchestration.state import (
    append_messages,
    append_tool_results,
    create_initial_state,
    create_plan_verify_state,
)


def test_create_initial_state_separates_runtime_fields_from_messages() -> None:
    state = create_initial_state(
        task="修复代码",
        system_prompt="你是代码 Agent",
        workspace_id="workspace-001",
        max_steps=6,
        session_id="session-001",
    )

    assert state["task"] == "修复代码"
    assert state["session_id"] == "session-001"
    assert state["workspace_id"] == "workspace-001"
    assert state["messages"][1] == {"role": "user", "content": "修复代码"}
    assert state["pending_tool_calls"] == []
    assert state["tool_results"] == []
    assert state["step_count"] == 0
    assert state["status"] == "running"
    assert state["final_result"] is None


def test_append_reducers_do_not_mutate_previous_lists() -> None:
    messages = [{"role": "user", "content": "first"}]
    results = [{"ok": False}]

    new_messages = append_messages(
        messages,
        [{"role": "assistant", "content": "second"}],
    )
    new_results = append_tool_results(results, [{"ok": True}])

    assert len(messages) == 1
    assert len(results) == 1
    assert [message["content"] for message in new_messages] == [
        "first",
        "second",
    ]
    assert [result["ok"] for result in new_results] == [False, True]


def test_max_steps_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_steps"):
        create_initial_state(
            task="task",
            system_prompt="prompt",
            workspace_id="workspace",
            max_steps=0,
        )


def test_create_plan_verify_state_has_control_plane_defaults() -> None:
    state = create_plan_verify_state(
        task="修复代码",
        workspace_id="workspace",
        max_steps=8,
        max_attempts=3,
        session_id="session",
    )

    assert state["status"] == "planning"
    assert state["plan"] is None
    assert state["attempts"] == 0
    assert state["max_attempts"] == 3
    assert state["messages"] == []


def test_max_attempts_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        create_plan_verify_state(
            task="task",
            workspace_id="workspace",
            max_steps=8,
            max_attempts=0,
        )
