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
    TodoView,
)
from tikiagent.interfaces.tui.presenter import TuiEventPresenter, tool_key, tool_label, clipped, REVIEW_LABELS, TODO_LABELS


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
        if feed_item is not None and event.event_type in {"result_reviewed", "verification_completed"}:
            todo = next((item for item in state.todos if item.todo_id == event.data.get("todo_id")), None)
            if todo and any(event.data.get(k) is not None and event.data[k] != getattr(todo, k)
                            for k in ("result_id", "handoff_id", "verification_id")):
                feed_item = feed_item.model_copy(update={"title": "历史审核 · 非最新交付", "summary": "仅供追溯，不代表当前验收"})
        if feed_item is not None:
            updates["feed"] = self._upsert(state.feed, feed_item)[-self.timeline_limit :]
        if event.event_type in {"approval_required", "recovery_required"}:
            # 暂停不是失败结果；只改变对应显示卡片，不伪造 ToolResult。
            key = tool_key(event)
            feed = list(updates.get("feed", state.feed))
            for index, item in enumerate(feed):
                if item.call_key == key:
                    status = "approval" if event.event_type == "approval_required" else "recovery"
                    feed[index] = item.model_copy(update={"tool_state": status,
                        "summary": tool_label(status, item.target, ""), "collapsed": False})
            updates["feed"] = tuple(feed)
        if event.event_type == "task_board_updated" and updates["todos"]:
            todos = updates["todos"]
            detail = "\n\n".join(f"{todo.todo_id} · {todo.owner} · {REVIEW_LABELS.get(todo.review_action, TODO_LABELS.get(todo.status, todo.status))} · 尝试 {todo.attempts}\n{todo.description}\n{todo.detail}" for todo in todos)
            board = FeedItem(sequence=event.sequence, kind="agent", title="TaskBoard · 任务清单",
                summary=f"{sum(t.review_action in {'accept', 'accept_with_limitations'} or t.status == 'completed' for t in todos)}/{len(todos)} 项已验收",
                detail=clipped(detail, 4800), card_key=f"board:{event.scope.task_id}")
            updates["feed"] = self._upsert(tuple(updates.get("feed", state.feed)), board)[-self.timeline_limit:]
        return state.model_copy(update=updates)

    @staticmethod
    def _upsert(feed: tuple[FeedItem, ...], incoming: FeedItem) -> tuple[FeedItem, ...]:
        items = list(feed)
        for index in range(len(items) - 1, -1, -1):
            old = items[index]
            same = bool(incoming.card_key) and old.card_key == incoming.card_key
            if incoming.call_key:
                same = old.call_key == incoming.call_key and (
                    not old.execution_id or not incoming.execution_id or old.execution_id == incoming.execution_id)
            if not same:
                continue
            if incoming.call_key:
                # 重复请求不能把已完成的调用倒退为待执行。
                if incoming.tool_state == "requested" and old.tool_state != "requested":
                    return feed
                target = incoming.target or old.target
                request = incoming.request_detail or old.request_detail
                detail = incoming.detail if incoming.tool_state in {"completed", "failed", "denied", "nonzero", "timeout"} else old.detail
                if detail and request and not detail.startswith(request):
                    detail = request + "\n" + detail
                incoming = incoming.model_copy(update={"card_key": old.card_key,
                    "execution_id": incoming.execution_id or old.execution_id, "attempt": incoming.attempt or old.attempt,
                    "target": target, "request_detail": request, "detail": detail or request or None,
                    "summary": tool_label(incoming.tool_state, target, incoming.result_detail)})
            items[index] = incoming
            return tuple(items)
        if incoming.call_key and any(x.card_key == incoming.card_key for x in items):
            incoming = incoming.model_copy(update={"card_key": f"{incoming.card_key}:{incoming.execution_id or incoming.sequence}"})
        return (*feed, incoming)

    def apply_outcome(self, state: TuiViewState, outcome: ApplicationOutcome) -> TuiViewState:
        """Outcome 是显示输入；status/resume 仍由 Controller 读取权威 Checkpoint。"""

        terminal = outcome.status in {
            "chat_completed", "chat_failed", "workflow_completed", "workflow_denied", "workflow_failed"
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
                "approval_details": outcome.approval_details,
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
                "todos": (), "review_actions": {}, "tool_calls": 0, "tool_request_keys": (),
            }
        if kind == "intent_routed":
            return {"status": "routed"}
        if kind in {"workflow_started", "workflow_resumed"}:
            return {"status": "running", "busy": True}
        if kind == "agent_started":
            return {"status": "running", "busy": True, "current_agent": str(data.get("agent", event.message.removeprefix("进入 ")))}
        if kind == "tool_call_requested":
            key = tool_key(event)
            keys = state.tool_request_keys if key in state.tool_request_keys else (*state.tool_request_keys, key)
            return {"tool_calls": len(keys), "tool_request_keys": keys}
        if kind == "task_board_updated":
            todos = []
            actions = {}
            for raw in data.get("items", []):
                review = raw.get("review") or {}
                # 只认可此 Todo 最新交付对应的 Review；旧审核不覆盖新尝试。
                valid = bool(review) and all(review.get(k) == raw.get(k) for k in ("todo_id", "result_id", "handoff_id", "verification_id"))
                action = review.get("action") if valid else None
                if action:
                    actions[raw["todo_id"]] = action
                detail = "\n".join([str(review.get("reason", "")) if valid else "",
                    *(review.get("limitations", []) if valid else []),
                    f"Result: {raw.get('result_id') or '-'}", f"Verification: {raw.get('verification_id') or '-'}"])
                todos.append(TodoView(todo_id=raw["todo_id"], description=clipped(raw.get("description", ""), 500),
                    owner=raw.get("owner", "-"), status="awaiting_review" if review and not valid else raw.get("status", "pending"), attempts=raw.get("attempts", 0),
                    result_id=raw.get("result_id"), handoff_id=raw.get("handoff_id"), verification_id=raw.get("verification_id"),
                    review_action=action, detail=clipped(detail, 1200)))
            return {"todos": tuple(todos), "review_actions": actions, "verification": _review_summary(actions) if actions else "待验收"}
        if kind == "approval_required":
            return {
                "status": "awaiting_approval", "busy": False,
                "approvals": state.approvals + 1,
                "checkpoint_revision": _optional_int(data.get("revision")),
                "approval_request_id": _optional_str(data.get("approval_request_id")),
                "approval_details": None,
                "execution_id": _optional_str(data.get("execution_id")),
                "attempt": _optional_int(data.get("attempt")),
            }
        if kind == "approval_decided":
            return {"status": "running", "busy": True}
        if kind == "tool_execution_started":
            return {"status": "executing", "busy": True}
        if kind == "verification_completed":
            todo = next((item for item in state.todos if item.todo_id == data.get("todo_id")), None)
            if todo and (todo.result_id != data.get("result_id") or todo.handoff_id != data.get("handoff_id")):
                return {}
            if data.get("advisory"):
                return {"verification": _review_summary(state.review_actions) if state.review_actions else "待验收"}
            return {"verification": "PASS" if data.get("passed") is True else "FAIL"}
        if kind == "result_reviewed":
            todo = next((item for item in state.todos if item.todo_id == data.get("todo_id")), None)
            if todo and any(data.get(k) is not None and data[k] != getattr(todo, k) for k in ("result_id", "handoff_id", "verification_id")):
                return {}
            actions = dict(state.review_actions)
            actions[data.get("todo_id") or event.correlation_id] = data.get("action", "")
            return {"review_actions": actions, "verification": _review_summary(actions)}
        if kind == "recovery_required":
            return {"status": "recovery_required", "busy": False,
                    "checkpoint_revision": _optional_int(data.get("revision")),
                    "execution_id": _optional_str(data.get("execution_id"))}
        if kind == "reconciliation_completed":
            return {"status": "running", "busy": True}
        if kind == "workflow_denied":
            return {"status": "workflow_denied", "busy": False, "current_agent": "-"}
        if kind == "workflow_failed":
            return {"status": "workflow_failed", "busy": False, "current_agent": "-"}
        if kind == "workflow_completed":
            return {"status": "workflow_completed", "busy": False, "current_agent": "-"}
        if kind == "final_answer":
            return {"status": event.data.get("status", "completed"), "busy": False,
                    "current_agent": "-", "final_answer": event.message}
        return {}

    @staticmethod
    def _timeline_item(event: ApplicationEvent) -> TimelineItem:
        mapping: dict[str, TimelineKind] = {
            "turn_received": "user", "intent_routed": "routing", "supervisor_decision": "routing",
            "agent_started": "agent", "handoff_created": "agent", "specialist_result": "agent",
            "tool_call_requested": "tool", "tool_execution_started": "tool", "tool_result_received": "tool",
            "approval_required": "approval", "approval_decided": "approval",
            "verification_completed": "verification", "result_reviewed": "routing", "final_answer": "final",
        }
        return TimelineItem(sequence=event.sequence, kind=mapping.get(event.event_type, "system"),
                            title=event.event_type.replace("_", " ").title(), detail=event.message)


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 1 else None


def _review_summary(actions: dict[str, str]) -> str:
    # 汇总所有 Todo，最后一项成功不能覆盖上游限制。
    for action in ("stop", "request_changes", "accept_with_limitations", "accept"):
        if action in actions.values():
            return REVIEW_LABELS[action]
    return "待验收"
