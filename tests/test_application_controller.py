"""ApplicationController、Workflow Port 与 CLI 边界测试。"""

from __future__ import annotations

import json

from tikiagent.application.cli import main
from tikiagent.application.controller import ApplicationController
from tikiagent.application.events import CollectingEventSink, EventBus
from tikiagent.application.models import EventScope, WorkflowOutcome
from tikiagent.application.routing import RuleBasedIntentRouter
from tikiagent.application.session import JsonSessionStore, JsonlTurnStore, SessionService


class ChatStub:
    def respond(self, user_input, *, recent_messages):
        return f"chat:{user_input}:history={len(recent_messages)}"


class ContextRefsStub:
    def select(self, session_id: str) -> list[str]:
        return [f"final:previous:{session_id}"]


class WorkflowStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.status_value: WorkflowOutcome | None = None

    def start(self, *, task, scope, context_refs, causation_id=None):
        self.calls.append(
            (
                "start",
                {
                    "task": task,
                    "scope": scope,
                    "context_refs": context_refs,
                    "causation_id": causation_id,
                },
            )
        )
        value = outcome(scope, status="awaiting_approval", checkpoint_id="cp-1")
        self.status_value = value
        return value

    def status(self, *, checkpoint_id, scope):
        self.calls.append(("status", {"checkpoint_id": checkpoint_id, "scope": scope}))
        assert self.status_value is not None
        return self.status_value

    def resume_approval(
        self,
        *,
        checkpoint_id,
        expected_revision,
        scope,
        request_id,
        approved,
    ):
        self.calls.append(
            (
                "resume",
                {
                    "checkpoint_id": checkpoint_id,
                    "expected_revision": expected_revision,
                    "request_id": request_id,
                    "approved": approved,
                },
            )
        )
        return outcome(scope, status="completed", checkpoint_id=None)

    def recover(self, **kwargs):  # pragma: no cover - 由独立恢复测试覆盖
        raise NotImplementedError

    def reconcile(self, **kwargs):  # pragma: no cover - 由独立恢复测试覆盖
        raise NotImplementedError


def outcome(
    scope: EventScope,
    *,
    status: str,
    checkpoint_id: str | None,
) -> WorkflowOutcome:
    return WorkflowOutcome.model_validate(
        {
            "status": status,
            "task_id": scope.task_id or "task-1",
            "session_id": scope.session_id,
            "workspace_id": scope.workspace_id or "workspace",
            "message": "done" if status == "completed" else "pending",
            "final_result": "final answer" if status == "completed" else None,
            "run_id": "run-1",
            "checkpoint_id": checkpoint_id,
            "checkpoint_revision": 1 if checkpoint_id else None,
            "approval_request_id": "approval-1" if checkpoint_id else None,
            "execution_id": "execution-1" if checkpoint_id else None,
            "attempt": 1 if checkpoint_id else None,
            "tool_call_id": "call-1" if checkpoint_id else None,
            "tool_name": "run_command" if checkpoint_id else None,
        }
    )


def controller(tmp_path):
    bus = EventBus(stream_id="test-stream")
    collector = CollectingEventSink()
    bus.subscribe(collector)
    sessions = SessionService(
        sessions=JsonSessionStore(tmp_path / "sessions"),
        turns=JsonlTurnStore(tmp_path / "turns"),
    )
    workflow = WorkflowStub()
    value = ApplicationController(
        sessions=sessions,
        router=RuleBasedIntentRouter(),
        chat=ChatStub(),
        workflow=workflow,
        context_refs=ContextRefsStub(),
        event_bus=bus,
    )
    return value, sessions, workflow, collector


def test_chat_turn_does_not_enter_workflow(tmp_path) -> None:
    value, _, workflow, collector = controller(tmp_path)
    created = value.new_session(workspace_id="workspace")

    result = value.submit(session_id=created.session_id, user_input="什么是 Agent？")

    assert result.status == "chat_completed"
    assert workflow.calls == []
    assert collector.events[-1].event_type == "final_answer"


def test_workflow_pause_binds_checkpoint_after_start_and_passes_refs(tmp_path) -> None:
    value, sessions, workflow, _ = controller(tmp_path)
    created = value.new_session(workspace_id="workspace")

    result = value.submit(
        session_id=created.session_id,
        user_input="根据刚才结果生成网页",
    )

    assert result.status == "awaiting_approval"
    assert sessions.sessions.load(created.session_id).active_checkpoint_id == "cp-1"
    assert workflow.calls[0][1]["context_refs"] == [
        f"final:previous:{created.session_id}"
    ]


def test_status_delegates_to_authoritative_workflow_checkpoint(tmp_path) -> None:
    value, _, workflow, _ = controller(tmp_path)
    created = value.new_session(workspace_id="workspace")
    value.submit(session_id=created.session_id, user_input="创建网页")

    result = value.status(session_id=created.session_id)

    assert result.status == "awaiting_approval"
    assert workflow.calls[-1][0] == "status"
    assert workflow.calls[-1][1]["checkpoint_id"] == "cp-1"


def test_resume_clears_session_checkpoint_only_after_terminal_outcome(tmp_path) -> None:
    value, sessions, workflow, _ = controller(tmp_path)
    created = value.new_session(workspace_id="workspace")
    value.submit(session_id=created.session_id, user_input="创建网页")

    result = value.resume(
        session_id=created.session_id,
        expected_revision=1,
        request_id="approval-1",
        approved=True,
    )

    assert result.status == "workflow_completed"
    assert result.message == "final answer"
    assert sessions.sessions.load(created.session_id).active_checkpoint_id is None
    assert workflow.calls[-1][1]["request_id"] == "approval-1"


def test_cli_new_session_does_not_require_model_environment(tmp_path, capsys) -> None:
    exit_code = main(
        [
            "--data-dir",
            str(tmp_path),
            "--env-file",
            str(tmp_path / "missing.env"),
            "--json",
            "new-session",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "session_created"
