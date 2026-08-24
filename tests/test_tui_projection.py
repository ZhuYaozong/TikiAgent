from tikiagent.application.events import EventBus
from tikiagent.application.models import ApplicationOutcome, EventScope
from tikiagent.tui.adapter import TuiEventAdapter
from tikiagent.tui.commands import parse_command
from tikiagent.tui.models import TuiViewState


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

    try:
        adapter.reduce(state, event)
    except ValueError as error:
        assert "重复或乱序" in str(error)
    else:  # pragma: no cover
        raise AssertionError("重复事件必须被拒绝")


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
