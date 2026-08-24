"""把 Harness 生命周期事实转发到统一 Application EventBus。"""

from tikiagent.application.events import EventBus
from tikiagent.application.models import ApplicationEventType, EventScope
from tikiagent.harness.coordinator import ExecutionLifecycleEvent


class HarnessEventForwarder:
    """只映射已发生事件；不读取 Controller 结果反推内部生命周期。"""

    _EVENT_MAP: dict[str, ApplicationEventType] = {
        "tool_call_requested": "tool_call_requested",
        "approval_requested": "approval_required",
        "approval_decided": "approval_decided",
        "tool_execution_started": "tool_execution_started",
        "tool_execution_finished": "tool_result_received",
        "tool_execution_denied": "tool_result_received",
        "recovery_required": "recovery_required",
        "recovery_decided": "recovery_decided",
        "execution_reconciled": "reconciliation_completed",
    }

    def __init__(self, event_bus: EventBus) -> None:
        self.event_bus = event_bus

    def handle(self, event: ExecutionLifecycleEvent) -> None:
        event_type = self._EVENT_MAP.get(event.event_type)
        if event_type is None:
            return
        checkpoint = event.checkpoint
        data = dict(event.details or {})
        if checkpoint is not None:
            data.update(
                {
                    "revision": checkpoint.revision,
                    "execution_id": checkpoint.identity.execution_id,
                    "attempt": checkpoint.identity.attempt,
                }
            )
            if checkpoint.approval_request is not None:
                data["approval_request_id"] = checkpoint.approval_request.request_id
            if event_type == "tool_result_received" and checkpoint.tool_result is not None:
                data["tool_result"] = checkpoint.tool_result.model_dump(mode="json")
        self.event_bus.emit(
            event_type,
            scope=EventScope(
                session_id=event.scope.session_id,
                workspace_id=event.scope.workspace_id,
                task_id=event.scope.task_id,
                run_id=event.run_id,
                checkpoint_id=checkpoint.checkpoint_id if checkpoint else None,
            ),
            source="execution_harness_adapter",
            correlation_id=event.tool_call_id,
            message=f"{event.tool_name}: {event.event_type}",
            data=data,
        )
