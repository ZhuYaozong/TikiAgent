"""把统一 EventBus 事件投影为 Demo 应用层摘要。"""

from __future__ import annotations

from tikiagent.application.models import ApplicationEvent
from tikiagent.demo.models import ApplicationDigest


class DemoEventCollector:
    """只接收 EventBus 已完成脱敏和截断后的事件副本。"""

    def __init__(self) -> None:
        self.events: list[ApplicationEvent] = []

    def handle(self, event: ApplicationEvent) -> None:
        self.events.append(event)

    def digest(self) -> ApplicationDigest:
        counts: dict[str, int] = {}
        agents: set[str] = set()
        verification_passed = 0
        verification_failed = 0
        for event in self.events:
            counts[event.event_type] = counts.get(event.event_type, 0) + 1
            if event.event_type == "agent_started":
                agent = event.data.get("agent")
                if isinstance(agent, str) and agent:
                    agents.add(agent)
            if event.event_type == "verification_completed":
                passed = event.data.get("passed")
                verification_passed += int(passed is True)
                verification_failed += int(passed is False)
        return ApplicationDigest(
            event_count=len(self.events),
            event_types=counts,
            agents_observed=sorted(agents),
            tool_calls=counts.get("tool_call_requested", 0),
            approvals=counts.get("approval_required", 0),
            verification_passed=verification_passed,
            verification_failed=verification_failed,
        )
