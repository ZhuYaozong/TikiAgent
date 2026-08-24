from __future__ import annotations

import asyncio
from pathlib import Path
import threading

from tikiagent.application.events import EventBus
from tikiagent.application.models import ApplicationOutcome, EventScope
from tikiagent.tui.app import TikiTuiApp
from tikiagent.tui.modals import (
    ApprovalModal,
    QuitWarningModal,
    ReconcileModal,
    RecoveryModal,
    RecoverySubmission,
)
from tikiagent.tui.models import SessionSnapshot, TranscriptItem


class FakeBackend:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.session_id = "session-test"
        self.workspace_id = "workspace-test"
        self.resume_calls = 0
        self.worker_threads: list[int] = []
        self.transcript: list[TranscriptItem] = []

    def _thread(self) -> None:
        self.worker_threads.append(threading.get_ident())

    def new_session(self, workspace_id: str) -> ApplicationOutcome:
        self._thread()
        self.workspace_id = workspace_id
        self.bus.emit("session_started", scope=EventScope(session_id=self.session_id, workspace_id=workspace_id),
                      source="application_controller", correlation_id=self.session_id, message="Session 已创建")
        return ApplicationOutcome(status="session_created", session_id=self.session_id, message="Session 已创建")

    def submit(self, session_id: str, user_input: str) -> ApplicationOutcome:
        self._thread()
        self.transcript.append(TranscriptItem(role="user", content=user_input))
        scope = EventScope(session_id=session_id, workspace_id=self.workspace_id, turn_id="turn-1", task_id="task-1")
        self.bus.emit("turn_received", scope=scope, source="application_controller",
                      correlation_id="task-1", message=user_input)
        if "审批" not in user_input:
            self.bus.emit("intent_routed", scope=scope, source="application_controller",
                          correlation_id="turn-1", message="Intent Router 返回 CHAT")
            answer = "chat answer"
            self.transcript.append(TranscriptItem(role="assistant", content=answer, status="chat_completed"))
            self.bus.emit("final_answer", scope=scope, source="application_controller",
                          correlation_id="turn-1", message=answer)
            return ApplicationOutcome(status="chat_completed", session_id=session_id,
                                      turn_id="turn-1", message=answer)
        self.bus.emit("workflow_started", scope=scope, source="workflow_adapter",
                      correlation_id="task-1", message="Workflow 已启动")
        approval_scope = scope.model_copy(update={"checkpoint_id": "checkpoint-1", "run_id": "run-1"})
        self.bus.emit("approval_required", scope=approval_scope, source="execution_harness_adapter",
                      correlation_id="call-1", message="run_command: approval_requested",
                      data={"revision": 1, "approval_request_id": "approval-1",
                            "execution_id": "execution-1", "attempt": 1})
        return self._pending("awaiting_approval", revision=1)

    def status(self, session_id: str) -> ApplicationOutcome:
        self._thread()
        return ApplicationOutcome(status="active", session_id=session_id, message="active")

    def resume(self, session_id: str, expected_revision: int, request_id: str, approved: bool) -> ApplicationOutcome:
        self._thread()
        self.resume_calls += 1
        scope = EventScope(session_id=session_id, workspace_id=self.workspace_id, task_id="task-1", run_id="run-1")
        self.bus.emit("approval_decided", scope=scope, source="execution_harness_adapter",
                      correlation_id="call-1", message=f"approved={approved}")
        self.bus.emit("workflow_completed", scope=scope, source="workflow_adapter",
                      correlation_id="task-1", message="done")
        self.bus.emit("final_answer", scope=scope, source="application_controller",
                      correlation_id="task-1", message="workflow done")
        return ApplicationOutcome(status="workflow_completed" if approved else "workflow_denied",
                                  session_id=session_id, task_id="task-1", run_id="run-1",
                                  message="workflow done")

    def recover(self, session_id: str, expected_revision: int, execution_id: str,
                action: str, decided_by: str, reason: str) -> ApplicationOutcome:
        self._thread()
        return self._pending("awaiting_reconcile" if action == "confirmed_executed" else "awaiting_approval",
                             revision=expected_revision + 1)

    def reconcile(self, session_id: str, expected_revision: int, execution_id: str,
                  result_file: Path) -> ApplicationOutcome:
        self._thread()
        return ApplicationOutcome(status="workflow_completed", session_id=session_id,
                                  task_id="task-1", message="reconciled")

    def session_snapshot(self, session_id: str) -> SessionSnapshot:
        self._thread()
        return SessionSnapshot(session_id=session_id, workspace_id=self.workspace_id,
                               turn_count=len([x for x in self.transcript if x.role == "user"]),
                               transcript=tuple(self.transcript))

    def _pending(self, status: str, revision: int) -> ApplicationOutcome:
        return ApplicationOutcome.model_validate({
            "status": status, "session_id": self.session_id, "task_id": "task-1", "run_id": "run-1",
            "checkpoint_id": "checkpoint-1", "checkpoint_revision": revision,
            "approval_request_id": "approval-1" if status == "awaiting_approval" else None,
            "execution_id": "execution-1", "attempt": 1,
            "tool_call_id": "call-1", "tool_name": "run_command", "message": status,
        })


def build_app(tmp_path: Path):
    holder: dict[str, FakeBackend] = {}
    def factory(bus: EventBus) -> FakeBackend:
        holder["backend"] = FakeBackend(bus)
        return holder["backend"]
    app = TikiTuiApp(data_dir=tmp_path, env_file=tmp_path / ".env", backend_factory=factory)
    return app, holder


def test_worker_streams_chat_and_updates_widgets_on_main_thread(tmp_path: Path) -> None:
    async def exercise() -> None:
        app, holder = build_app(tmp_path)
        async with app.run_test(size=(130, 42)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app._start_operation("submit", {"session_id": app.view_state.session_id, "user_input": "你好"})
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.view_state.final_answer == "chat answer"
            # 当前进程依赖 Event Stream，不把同一 Turn 的持久 transcript 重复追加到时间线。
            assert app.view_state.transcript == ()
            assert all(thread_id != app.main_thread_id for thread_id in holder["backend"].worker_threads)
    asyncio.run(exercise())


def test_real_runtime_can_start_tui_without_model_environment(tmp_path: Path) -> None:
    async def exercise() -> None:
        app = TikiTuiApp(data_dir=tmp_path, env_file=tmp_path / "missing.env")
        async with app.run_test(size=(130, 42)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.view_state.session_id is not None
            assert app.view_state.status == "session_created"
            assert app.operation_in_flight is None
    asyncio.run(exercise())


def test_approval_modal_submits_resume_once(tmp_path: Path) -> None:
    async def exercise() -> None:
        app, holder = build_app(tmp_path)
        async with app.run_test(size=(130, 42)) as pilot:
            await app.workers.wait_for_complete(); await pilot.pause()
            app._start_operation("submit", {"session_id": app.view_state.session_id, "user_input": "执行审批任务"})
            await app.workers.wait_for_complete(); await pilot.pause()
            assert isinstance(app.screen, ApprovalModal)
            modal = app.screen
            assert isinstance(modal, ApprovalModal)
            modal.submit_once(True)
            modal.submit_once(True)
            await pilot.pause(); await app.workers.wait_for_complete(); await pilot.pause()
            assert holder["backend"].resume_calls == 1
            assert app.resume_submission_count == 1
            assert app.view_state.status == "workflow_completed"
    asyncio.run(exercise())


def test_recovery_routes_confirmed_executed_to_reconcile_and_quit_warns(tmp_path: Path) -> None:
    async def exercise() -> None:
        app, _ = build_app(tmp_path)
        async with app.run_test(size=(130, 42)) as pilot:
            await app.workers.wait_for_complete(); await pilot.pause()
            app.view_state = app.view_state.model_copy(update={
                "status": "recovery_required", "checkpoint_revision": 2,
                "execution_id": "execution-1", "checkpoint_id": "checkpoint-1",
            })
            app.action_show_recovery(); await pilot.pause()
            assert isinstance(app.screen, RecoveryModal)
            app.screen.dismiss(
                RecoverySubmission(
                    action="confirmed_executed",
                    decided_by="operator",
                    reason="已在外部系统观察到副作用",
                )
            )
            await pilot.pause(); await app.workers.wait_for_complete(); await pilot.pause()
            assert app.view_state.status == "awaiting_reconcile"
            assert isinstance(app.screen, ReconcileModal)
            app.screen.dismiss(None); await pilot.pause()
            app.view_state = app.view_state.model_copy(update={"status": "executing", "busy": True})
            app.action_quit(); await pilot.pause()
            assert isinstance(app.screen, QuitWarningModal)
            assert app.quit_warning_count == 1
            app.screen.dismiss(False); await pilot.pause()
            assert app.is_running
    asyncio.run(exercise())
