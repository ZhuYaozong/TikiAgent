"""Application Event 到 conversation-first Feed Card 的纯展示映射。"""

from __future__ import annotations

from typing import Any

from tikiagent.application.models import ApplicationEvent
from tikiagent.tui.models import FeedItem, FeedKind


class TuiEventPresenter:
    """隐藏内部身份噪声，只选择对用户有意义且已脱敏的信息。"""

    def present(self, event: ApplicationEvent) -> FeedItem | None:
        kind = event.event_type
        if kind == "turn_received":
            return self._item(event, "user", "You", event.message, collapsed=False)
        if kind == "final_answer":
            return self._item(
                event,
                "assistant",
                "TikiAgent",
                _first_line(event.message),
                detail=event.message,
                collapsed=False,
            )
        if kind == "intent_routed":
            return self._item(event, "routing", "Intent Router", event.message)
        if kind == "supervisor_decision":
            return self._item(event, "routing", "Supervisor", event.message)
        if kind == "agent_started":
            agent = str(event.data.get("agent") or event.message.removeprefix("进入 "))
            return self._item(event, "agent", _agent_name(agent), "开始执行")
        if kind == "handoff_created":
            return self._item(event, "agent", "Handoff", event.message)
        if kind == "specialist_result":
            return self._item(event, "agent", "Specialist Result", event.message)
        if kind == "verification_completed":
            passed = event.data.get("passed") is True
            return self._item(
                event,
                "verification" if passed else "error",
                "Verification",
                "PASS" if passed else "FAIL",
                detail=event.message,
                collapsed=False,
            )
        if kind in {"tool_call_requested", "tool_execution_started", "tool_result_received"}:
            return self._tool_item(event)
        if kind in {"approval_required", "approval_decided"}:
            return self._item(event, "approval", "Approval", event.message)
        if kind in {"recovery_required", "recovery_decided", "reconciliation_completed"}:
            return self._item(event, "error", "Recovery", event.message, collapsed=False)
        if kind in {"workflow_denied"}:
            return self._item(event, "error", "Workflow", event.message, collapsed=False)
        # Session、Workflow start/completed 等状态已经显示在顶部和侧栏，避免污染对话。
        return None

    def _tool_item(self, event: ApplicationEvent) -> FeedItem:
        stage = {
            "tool_call_requested": "requested",
            "tool_execution_started": "running",
            "tool_result_received": "completed",
        }[event.event_type]
        tool_name = event.message.split(":", 1)[0].strip() or "tool"
        detail = _tool_detail(event.data)
        return self._item(
            event,
            "tool",
            f"Tool · {tool_name}",
            stage,
            detail=detail,
        )

    @staticmethod
    def _item(
        event: ApplicationEvent,
        kind: FeedKind,
        title: str,
        summary: str,
        *,
        detail: str | None = None,
        collapsed: bool = True,
    ) -> FeedItem:
        return FeedItem.model_validate(
            {
                "sequence": event.sequence,
                "kind": kind,
                "title": title,
                "summary": summary or "-",
                "detail": detail,
                "collapsed": collapsed,
            }
        )


def _tool_detail(data: dict[str, Any]) -> str | None:
    result = data.get("tool_result")
    if not isinstance(result, dict):
        return None
    lines = [f"ok: {result.get('ok')}"]
    output = result.get("output")
    if isinstance(output, dict):
        for key in ("path", "exit_code", "timed_out", "duration_seconds"):
            if key in output:
                lines.append(f"{key}: {output[key]}")
    error = result.get("error")
    if isinstance(error, dict):
        lines.append(f"error: {error.get('code', 'tool_error')}")
        if error.get("message"):
            lines.append(f"message: {error['message']}")
    return "\n".join(lines)


def _agent_name(value: str) -> str:
    return {
        "supervisor": "Supervisor",
        "research_agent": "ResearchAgent",
        "code_agent": "CodeAgent",
        "verification_gate": "Verification Gate",
    }.get(value, value.replace("_", " ").title())


def _first_line(value: str, limit: int = 120) -> str:
    line = next((item.strip() for item in value.splitlines() if item.strip()), "完成")
    return line if len(line) <= limit else line[: limit - 1] + "…"
