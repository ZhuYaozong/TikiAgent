"""TUI 使用的同步应用后端；所有方法必须在线程 Worker 中调用。"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from tikiagent.application.controller import ApplicationController
from tikiagent.application.events import EventBus
from tikiagent.application.models import ApplicationOutcome, ReconcileSubmission, ResponseRecord, TurnRecord
from tikiagent.application.runtime import ApplicationRuntimeFactory
from tikiagent.tui.models import SessionSnapshot, TranscriptItem


class TuiBackend(Protocol):
    def new_session(self, workspace_id: str) -> ApplicationOutcome: ...
    def submit(self, session_id: str, user_input: str) -> ApplicationOutcome: ...
    def status(self, session_id: str) -> ApplicationOutcome: ...
    def resume(self, session_id: str, expected_revision: int, request_id: str, approved: bool) -> ApplicationOutcome: ...
    def recover(self, session_id: str, expected_revision: int, execution_id: str,
                action: str, decided_by: str, reason: str) -> ApplicationOutcome: ...
    def reconcile(self, session_id: str, expected_revision: int, execution_id: str,
                  result_file: Path) -> ApplicationOutcome: ...
    def session_snapshot(self, session_id: str) -> SessionSnapshot: ...


class ApplicationTuiBackend:
    """把 TUI 输入映射到既有 Controller，不复制业务规则。"""

    def __init__(self, controller: ApplicationController) -> None:
        self.controller = controller

    def new_session(self, workspace_id: str) -> ApplicationOutcome:
        return self.controller.new_session(workspace_id=workspace_id)

    def submit(self, session_id: str, user_input: str) -> ApplicationOutcome:
        return self.controller.submit(session_id=session_id, user_input=user_input)

    def status(self, session_id: str) -> ApplicationOutcome:
        return self.controller.status(session_id=session_id)

    def resume(self, session_id: str, expected_revision: int, request_id: str, approved: bool) -> ApplicationOutcome:
        return self.controller.resume(session_id=session_id, expected_revision=expected_revision,
                                      request_id=request_id, approved=approved)

    def recover(self, session_id: str, expected_revision: int, execution_id: str,
                action: str, decided_by: str, reason: str) -> ApplicationOutcome:
        return self.controller.recover(session_id=session_id, expected_revision=expected_revision,
                                       execution_id=execution_id, action=action,
                                       decided_by=decided_by, reason=reason)

    def reconcile(self, session_id: str, expected_revision: int, execution_id: str,
                  result_file: Path) -> ApplicationOutcome:
        submission = ReconcileSubmission.model_validate_json(result_file.read_text(encoding="utf-8"))
        return self.controller.reconcile(session_id=session_id, expected_revision=expected_revision,
                                         execution_id=execution_id, submission=submission)

    def session_snapshot(self, session_id: str) -> SessionSnapshot:
        session = self.controller.sessions.sessions.load(session_id)
        transcript: list[TranscriptItem] = []
        for record in self.controller.sessions.turns.list_records(session_id):
            if isinstance(record, TurnRecord):
                transcript.append(TranscriptItem(role="user", content=record.user_input))
            elif isinstance(record, ResponseRecord):
                transcript.append(TranscriptItem(role="assistant", content=record.content, status=record.status))
        return SessionSnapshot(session_id=session.session_id, workspace_id=session.workspace_id,
                               turn_count=session.turn_count, transcript=tuple(transcript[-40:]))


def build_backend(data_dir: Path, env_file: Path, event_bus: EventBus) -> ApplicationTuiBackend:
    controller = ApplicationRuntimeFactory(data_dir, env_file=env_file, event_bus=event_bus).build_controller()
    return ApplicationTuiBackend(controller)
