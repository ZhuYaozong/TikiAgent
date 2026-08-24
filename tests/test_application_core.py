"""Application Core：Session、Router 与 EventBus 测试。"""

from pathlib import Path

import pytest

from tikiagent.application.events import CollectingEventSink, EventBus
from tikiagent.application.models import EventScope, IntentDecision, ResponseRecord
from tikiagent.application.routing import RuleBasedIntentRouter, StructuredIntentRouter
from tikiagent.application.session import (
    JsonSessionStore,
    JsonlTurnStore,
    SessionConflictError,
    SessionService,
)


class FakeStructuredRouterModel:
    def __init__(self, decision: IntentDecision | None = None) -> None:
        self.decision = decision

    def complete_structured(self, messages, response_type):
        if self.decision is None:
            raise ValueError("invalid model output")
        assert response_type is IntentDecision
        return self.decision


class FailingSink:
    def handle(self, event) -> None:
        raise RuntimeError("UI offline")


def build_service(tmp_path: Path) -> SessionService:
    return SessionService(
        sessions=JsonSessionStore(tmp_path / "sessions"),
        turns=JsonlTurnStore(tmp_path / "turns"),
    )


def test_session_only_keeps_checkpoint_reference(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    session = service.create(workspace_id="workspace-1")
    decision = IntentDecision(intent="WORKFLOW", reason="需要执行")
    updated, turn = service.record_turn(
        session_id=session.session_id,
        user_input="创建网页",
        decision=decision,
        task_id="task-1",
    )
    bound = service.bind_checkpoint(
        session_id=session.session_id,
        checkpoint_id="checkpoint-1",
    )
    session_path = tmp_path / "sessions" / f"{session.session_id}.json"
    raw = session_path.read_text(encoding="utf-8")

    assert updated.turn_count == 1
    assert turn.task_id == "task-1"
    assert bound.active_checkpoint_id == "checkpoint-1"
    assert "创建网页" not in raw
    assert "messages" not in raw
    assert "workflow_status" not in raw


def test_session_revision_prevents_stale_overwrite(tmp_path: Path) -> None:
    store = JsonSessionStore(tmp_path)
    session = store.create(workspace_id="workspace-1")
    current = session.model_copy(update={"revision": 2, "turn_count": 1})
    store.save(current, expected_revision=1)

    with pytest.raises(SessionConflictError, match="revision 冲突"):
        store.save(
            session.model_copy(update={"revision": 2, "turn_count": 99}),
            expected_revision=1,
        )


def test_turn_store_preserves_pause_and_final_response(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    session = service.create(workspace_id="workspace-1")
    _, turn = service.record_turn(
        session_id=session.session_id,
        user_input="运行任务",
        decision=IntentDecision(intent="WORKFLOW", reason="需要执行"),
        task_id="task-1",
    )
    service.record_response(
        ResponseRecord(
            turn_id=turn.turn_id,
            session_id=session.session_id,
            task_id="task-1",
            status="awaiting_approval",
            content="等待批准",
            checkpoint_id="checkpoint-1",
        )
    )
    service.record_response(
        ResponseRecord(
            turn_id=turn.turn_id,
            session_id=session.session_id,
            task_id="task-1",
            status="workflow_completed",
            content="任务完成",
        )
    )

    records = service.turns.list_records(session.session_id)
    assert [record.record_type for record in records] == [
        "turn",
        "response",
        "response",
    ]
    assert service.turns.recent_messages(session.session_id)[-1] == {
        "role": "assistant",
        "content": "任务完成",
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Tool Calling 和 Agent 有什么区别？", "CHAT"),
        ("Verifier 为什么不能直接修改文件？", "CHAT"),
        ("搜索今天的重要 Agent 新闻", "WORKFLOW"),
        ("根据刚才结果生成网页", "WORKFLOW"),
        ("请解释原因然后修复代码", "WORKFLOW"),
    ],
)
def test_rule_router_only_returns_chat_or_workflow(text: str, expected: str) -> None:
    decision = RuleBasedIntentRouter().route(text, has_history=True)
    assert decision.intent == expected
    assert "target_agent" not in type(decision).model_fields
    assert "plan" not in type(decision).model_fields


def test_structured_router_uses_fallback_on_invalid_model_output() -> None:
    router = StructuredIntentRouter(
        FakeStructuredRouterModel(),
        fallback=RuleBasedIntentRouter(),
    )
    assert router.route("运行测试", has_history=False).intent == "WORKFLOW"


def test_event_bus_redacts_bounds_and_isolates_sink_failure() -> None:
    bus = EventBus(stream_id="stream-1", max_text_length=40)
    collector = CollectingEventSink()
    bus.subscribe(FailingSink())
    bus.subscribe(collector)
    first = bus.emit(
        "tool_call_requested",
        scope=EventScope(session_id="session-1", task_id="task-1"),
        source="code_agent",
        correlation_id="task-1",
        message="x" * 100,
        data={"api_key": "secret", "stdout": "y" * 100},
    )
    second = bus.emit(
        "approval_required",
        scope=first.scope,
        source="harness_adapter",
        correlation_id="task-1",
        causation_id=first.event_id,
        message="等待批准",
    )

    assert [item.sequence for item in collector.events] == [1, 2]
    assert first.data["api_key"] == "[REDACTED]"
    assert len(first.data["stdout"]) <= 40
    assert second.causation_id == first.event_id
    assert len(bus.delivery_failures) == 2
