"""ApplicationEvent/ApplicationOutcome 到 TuiViewState 的纯投影。"""

from __future__ import annotations

from tikiagent.application.models import ApplicationEvent, ApplicationOutcome
from tikiagent.interfaces.tui.models import (
    FeedItem,
    SessionSnapshot,
    TimelineItem,
    TimelineKind,
    TuiViewState,
    WorkspaceEntry,
)
from tikiagent.interfaces.tui.presenter import TuiEventPresenter


class TuiEventAdapter:
    def __init__(self, *, timeline_limit: int = 300) -> None:
        if timeline_limit < 1:
            raise ValueError("timeline_limit 必须大于 0")
        self.timeline_limit = timeline_limit
        self.presenter = TuiEventPresenter()

    def reduce(self, state: TuiViewState, event: ApplicationEvent) -> TuiViewState:
        if state.stream_id is not None and state.stream_id != event.stream_id:
            raise ValueError("TuiViewState 不能混入其他 Event Stream")
        if event.sequence <= state.last_sequence:
            raise ValueError("TUI 拒绝重复或乱序事件")
        updates: dict[str, object] = {
            "stream_id": event.stream_id,
            "last_sequence": event.sequence,
            "session_id": event.scope.session_id,
            "workspace_id": event.scope.workspace_id or state.workspace_id,
            "turn_id": event.scope.turn_id or state.turn_id,
            "task_id": event.scope.task_id or state.task_id,
            "run_id": event.scope.run_id or state.run_id,
            "checkpoint_id": event.scope.checkpoint_id or state.checkpoint_id,
            "error": None,
        }
        updates.update(self._event_updates(state, event))
        updates["timeline"] = (*state.timeline, self._timeline_item(event))[-self.timeline_limit :]
        feed_item = self.presenter.present(event)
        if feed_item is not None:
            updates["feed"] = (*state.feed, feed_item)[-self.timeline_limit :]
        return state.model_copy(update=updates)

    def apply_outcome(self, state: TuiViewState, outcome: ApplicationOutcome) -> TuiViewState:
        """Outcome 是显示输入；status/resume 仍由 Controller 读取权威 Checkpoint。"""

        terminal = outcome.status in {
            "chat_completed", "workflow_completed", "workflow_denied", "workflow_failed"
        }
        paused = outcome.status in {
            "awaiting_approval", "recovery_required", "awaiting_reconcile"
        }
        idle = outcome.status in {"session_created", "active"}
        return state.model_copy(
            update={
                "session_id": outcome.session_id,
                "turn_id": outcome.turn_id or state.turn_id,
                "task_id": outcome.task_id or state.task_id,
                "run_id": outcome.run_id or state.run_id,
                "status": outcome.status,
                "busy": not terminal and not paused and not idle,
                "checkpoint_id": outcome.checkpoint_id,
                "checkpoint_revision": outcome.checkpoint_revision,
                "approval_request_id": outcome.approval_request_id,
                "execution_id": outcome.execution_id,
                "attempt": outcome.attempt,
                "tool_call_id": outcome.tool_call_id,
                "tool_name": outcome.tool_name,
                "final_answer": outcome.message if terminal else state.final_answer,
                "notice": outcome.message,
                "error": None,
            }
        )

    @staticmethod
    def apply_session(state: TuiViewState, snapshot: SessionSnapshot) -> TuiViewState:
        return state.model_copy(update={
            "session_id": snapshot.session_id,
            "workspace_id": snapshot.workspace_id,
            "transcript": snapshot.transcript,
        })

    @staticmethod
    def apply_workspace(state: TuiViewState, entries: tuple[WorkspaceEntry, ...]) -> TuiViewState:
        return state.model_copy(update={"workspace_entries": entries})

    @staticmethod
    def with_error(state: TuiViewState, message: str) -> TuiViewState:
        error_item = FeedItem(
            sequence=max(1, state.last_sequence + 1),
            kind="error",
            title="Operation Failed",
            summary=message,
            detail=message,
            collapsed=False,
        )
        return state.model_copy(
            update={
                "busy": False,
                "error": message,
                "notice": message,
                "feed": (*state.feed, error_item)[-300:],
            }
        )

    @staticmethod
    def _event_updates(state: TuiViewState, event: ApplicationEvent) -> dict[str, object]:
        kind, data = event.event_type, event.data
        if kind == "session_started":
            return {"status": "active", "busy": False}
        if kind == "turn_received":
            return {
                "status": "routing", "busy": True, "final_answer": None,
                "current_agent": "-", "verification": "-",
            }
        if kind == "intent_routed":
            return {"status": "routed"}
        if kind in {"workflow_started", "workflow_resumed"}:
            return {"status": "running", "busy": True}
        if kind == "agent_started":
            return {"status": "running", "busy": True, "current_agent": str(data.get("agent", event.message.removeprefix("进入 ")))}
        if kind == "tool_call_requested":
            return {"tool_calls": state.tool_calls + 1}
        if kind == "approval_required":
            return {
                "status": "awaiting_approval", "busy": False,
                "approvals": state.approvals + 1,
                "checkpoint_revision": _optional_int(data.get("revision")),
                "approval_request_id": _optional_str(data.get("approval_request_id")),
                "execution_id": _optional_str(data.get("execution_id")),
                "attempt": _optional_int(data.get("attempt")),
            }
        if kind == "approval_decided":
            return {"status": "running", "busy": True}
        if kind == "tool_execution_started":
            return {"status": "executing", "busy": True}
        if kind == "verification_completed":
            return {"verification": "PASS" if data.get("passed") is True else "FAIL"}
        if kind == "recovery_required":
            return {"status": "recovery_required", "busy": False,
                    "checkpoint_revision": _optional_int(data.get("revision")),
                    "execution_id": _optional_str(data.get("execution_id"))}
        if kind == "reconciliation_completed":
            return {"status": "running", "busy": True}
        if kind == "workflow_denied":
            return {"status": "workflow_denied", "busy": False, "current_agent": "-"}
        if kind == "workflow_completed":
            return {"status": "workflow_completed", "busy": False, "current_agent": "-"}
        if kind == "final_answer":
            return {"status": "completed", "busy": False, "current_agent": "-", "final_answer": event.message}
        return {}

    @staticmethod
    def _timeline_item(event: ApplicationEvent) -> TimelineItem:
        mapping: dict[str, TimelineKind] = {
            "turn_received": "user", "intent_routed": "routing", "supervisor_decision": "routing",
            "agent_started": "agent", "handoff_created": "agent", "specialist_result": "agent",
            "tool_call_requested": "tool", "tool_execution_started": "tool", "tool_result_received": "tool",
            "approval_required": "approval", "approval_decided": "approval",
            "verification_completed": "verification", "final_answer": "final",
        }
        return TimelineItem(sequence=event.sequence, kind=mapping.get(event.event_type, "system"),
                            title=event.event_type.replace("_", " ").title(), detail=event.message)


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 1 else None
