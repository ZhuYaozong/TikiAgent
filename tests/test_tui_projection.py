from tikiagent.application.events import EventBus
from tikiagent.application.models import ApplicationOutcome, EventScope
from tikiagent.interfaces.tui.adapter import TuiEventAdapter
from tikiagent.interfaces.tui.commands import parse_command
from tikiagent.interfaces.tui.models import TuiViewState


def test_event_adapter_projects_public_events_and_rejects_bad_order() -> None:
    bus = EventBus(stream_id="stream-1")
    scope = EventScope(session_id="session-1", workspace_id="workspace-1", task_id="task-1")
    adapter = TuiEventAdapter()
    state = TuiViewState()
    event = bus.emit("agent_started", scope=scope, source="workflow_adapter",
                     correlation_id="task-1", message="进入 code_agent", data={"agent": "code_agent"})
    state = adapter.reduce(state, event)
    assert state.current_agent == "code_agent"
    assert state.busy is True
    assert state.timeline[-1].kind == "agent"
    assert state.feed[-1].title == "CodeAgent"
    assert state.feed[-1].summary == "开始执行"

    try:
        adapter.reduce(state, event)
    except ValueError as error:
        assert "重复或乱序" in str(error)
    else:  # pragma: no cover
        raise AssertionError("重复事件必须被拒绝")


def test_conversation_feed_keeps_answers_and_hides_low_level_payloads() -> None:
    bus = EventBus(stream_id="stream-1")
    scope = EventScope(session_id="session-1", task_id="task-1")
    adapter = TuiEventAdapter()
    state = TuiViewState()
    state = adapter.reduce(
        state,
        bus.emit(
            "session_started",
            scope=scope,
            source="application_controller",
            correlation_id="session-1",
            message="Session 已创建",
        ),
    )
    assert state.feed == ()
    state = adapter.reduce(
        state,
        bus.emit(
            "turn_received",
            scope=scope,
            source="application_controller",
            correlation_id="task-1",
            message="搜索 Agent 新闻",
        ),
    )
    state = adapter.reduce(
        state,
        bus.emit(
            "tool_result_received",
            scope=scope,
            source="execution_harness_adapter",
            correlation_id="call-1",
            message="web_search: tool_execution_finished",
            data={
                "tool_result": {
                    "ok": True,
                    "output": {
                        "path": "result.json",
                        "stdout": "不应进入展示层的超长原始正文",
                    },
                }
            },
        ),
    )
    answer = "# 调研总结\n\n发现一\n\n## 来源\n<https://example.com>"
    state = adapter.reduce(
        state,
        bus.emit(
            "final_answer",
            scope=scope,
            source="application_controller",
            correlation_id="task-1",
            message=answer,
        ),
    )

    assert [item.kind for item in state.feed] == ["user", "tool", "assistant"]
    assert state.feed[-1].detail == answer
    assert "超长原始正文" not in (state.feed[1].detail or "")


def test_outcome_projection_keeps_checkpoint_fields_without_becoming_authority() -> None:
    adapter = TuiEventAdapter()
    outcome = ApplicationOutcome(
        status="awaiting_approval", session_id="session-1", task_id="task-1",
        checkpoint_id="checkpoint-1", checkpoint_revision=3,
        approval_request_id="approval-1", execution_id="execution-1",
        attempt=2, tool_call_id="call-1", tool_name="run_command", message="等待审批",
    )
    state = adapter.apply_outcome(TuiViewState(), outcome)
    assert state.status == "awaiting_approval"
    assert state.checkpoint_revision == 3
    assert state.approval_request_id == "approval-1"
    assert state.busy is False

    active = adapter.apply_outcome(
        state,
        ApplicationOutcome(status="active", session_id="session-1", message="active"),
    )
    assert active.busy is False


def test_operation_error_is_visible_in_conversation_feed() -> None:
    adapter = TuiEventAdapter()
    state = adapter.with_error(TuiViewState(last_sequence=7), "ValidationError: 回答过长")

    assert state.busy is False
    assert state.feed[-1].kind == "error"
    assert state.feed[-1].title == "Operation Failed"
    assert "回答过长" in state.feed[-1].detail


def test_slash_commands_are_strict_and_normal_text_is_not_a_command() -> None:
    assert parse_command("创建网页") is None
    assert parse_command('/new "demo workspace"').argument == "demo workspace"
    assert parse_command("/session session-1").name == "session"
    try:
        parse_command("/status extra")
    except ValueError as error:
        assert "不接受" in str(error)
    else:  # pragma: no cover
        raise AssertionError("多余参数必须被拒绝")
