"""Worker 与 Textual 主线程之间的结构化消息。"""

from textual.message import Message

from tikiagent.application.models import ApplicationEvent, ApplicationOutcome
from tikiagent.tui.models import SessionSnapshot, WorkspaceEntry


class TuiEventReceived(Message):
    def __init__(self, event: ApplicationEvent, *, producer_thread_id: int) -> None:
        super().__init__()
        self.event, self.producer_thread_id = event, producer_thread_id


class OperationFinished(Message):
    def __init__(self, operation_id: str, operation: str, outcome: ApplicationOutcome,
                 session: SessionSnapshot, *, producer_thread_id: int) -> None:
        super().__init__()
        self.operation_id, self.operation = operation_id, operation
        self.outcome, self.session = outcome, session
        self.producer_thread_id = producer_thread_id


class OperationFailed(Message):
    def __init__(self, operation_id: str, operation: str, error: Exception,
                 *, producer_thread_id: int) -> None:
        super().__init__()
        self.operation_id, self.operation = operation_id, operation
        self.error_type, self.error_message = type(error).__name__, str(error)
        self.producer_thread_id = producer_thread_id


class WorkspaceSnapshotReceived(Message):
    def __init__(self, session_id: str, entries: tuple[WorkspaceEntry, ...],
                 *, producer_thread_id: int) -> None:
        super().__init__()
        self.session_id, self.entries = session_id, entries
        self.producer_thread_id = producer_thread_id
