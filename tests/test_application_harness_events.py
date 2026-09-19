"""Harness 事实到 Application EventBus 的转发测试。"""

from tikiagent.application.events import CollectingEventSink, EventBus
from tikiagent.application.harness_events import HarnessEventForwarder
from tikiagent.harness.coordinator import ExecutionLifecycleEvent
from tikiagent.harness.scope import ExecutionScope


def test_harness_forwarder_uses_internal_source_and_stream_sequence() -> None:
    bus = EventBus(stream_id="stream-1")
    collector = CollectingEventSink()
    bus.subscribe(collector)
    forwarder = HarnessEventForwarder(bus)
    scope = ExecutionScope(
        task_id="task-1",
        session_id="session-1",
        workspace_id="workspace-1",
    )

    forwarder.handle(
        ExecutionLifecycleEvent(
            event_type="tool_call_requested",
            scope=scope,
            run_id="run-1",
            tool_call_id="call-1",
            tool_name="run_command",
            details={"arguments": {"token": "secret"}},
        )
    )
    forwarder.handle(
        ExecutionLifecycleEvent(
            event_type="tool_execution_started",
            scope=scope,
            run_id="run-1",
            tool_call_id="call-1",
            tool_name="run_command",
        )
    )

    assert [event.event_type for event in collector.events] == [
        "tool_call_requested",
        "tool_execution_started",
    ]
    assert [event.sequence for event in collector.events] == [1, 2]
    assert all(
        event.source == "execution_harness_adapter" for event in collector.events
    )
    assert collector.events[0].data["arguments"]["token"] == "[REDACTED]"
