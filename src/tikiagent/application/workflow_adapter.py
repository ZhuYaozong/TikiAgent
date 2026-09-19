"""应用用例到正式 Multi-Agent Workflow 的适配层。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from tikiagent.application.events import EventBus
from tikiagent.application.models import (
    EventScope,
    ReconcileSubmission,
    WorkflowOutcome,
)
from tikiagent.harness.permissions.models import ApprovalDecision
from tikiagent.harness.persistence.checkpoint import (
    ExecutionCheckpoint,
    JsonCheckpointStore,
)
from tikiagent.harness.persistence.recovery import ReconcileResult, RecoveryDecision
from tikiagent.orchestration.state import TikiState
from tikiagent.orchestration.workflow import MultiAgentWorkflow
from tikiagent.tools.models import ToolError, ToolResult


class WorkflowPort(Protocol):
    """Controller 不依赖 LangGraph Node、Dispatcher 或 Checkpoint Schema。"""

    def start(
        self,
        *,
        task: str,
        scope: EventScope,
        context_refs: list[str],
        causation_id: str | None = None,
    ) -> WorkflowOutcome: ...

    def status(self, *, checkpoint_id: str, scope: EventScope) -> WorkflowOutcome: ...

    def resume_approval(
        self,
        *,
        checkpoint_id: str,
        expected_revision: int,
        scope: EventScope,
        request_id: str,
        approved: bool,
    ) -> WorkflowOutcome: ...

    def recover(
        self,
        *,
        checkpoint_id: str,
        expected_revision: int,
        scope: EventScope,
        execution_id: str,
        decision: RecoveryDecision,
    ) -> WorkflowOutcome: ...

    def reconcile(
        self,
        *,
        checkpoint_id: str,
        expected_revision: int,
        scope: EventScope,
        execution_id: str,
        submission: ReconcileSubmission,
    ) -> WorkflowOutcome: ...


WorkflowFactory = Callable[[str, str], MultiAgentWorkflow]


class TikiWorkflowAdapter:
    """所有 Start/Resume 都通过正式 Graph；Checkpoint 只负责恢复事实。"""

    def __init__(
        self,
        *,
        workflow_factory: WorkflowFactory,
        checkpoint_store: JsonCheckpointStore,
        event_bus: EventBus,
    ) -> None:
        self.workflow_factory = workflow_factory
        self.checkpoint_store = checkpoint_store
        self.event_bus = event_bus

    def start(
        self,
        *,
        task: str,
        scope: EventScope,
        context_refs: list[str],
        causation_id: str | None = None,
    ) -> WorkflowOutcome:
        task_id, workspace_id = self._require_task_scope(scope)
        self.event_bus.emit(
            "workflow_started",
            scope=scope,
            source="workflow_adapter",
            correlation_id=task_id,
            causation_id=causation_id,
            message="Multi-Agent Workflow 已启动",
            data={"context_refs": context_refs},
        )
        workflow = self.workflow_factory(scope.session_id, workspace_id)
        state: TikiState | None = None
        previous: TikiState | None = None
        for snapshot in workflow.stream(
            task,
            session_id=scope.session_id,
            task_id=task_id,
            session_context_refs=context_refs,
        ):
            self._emit_snapshot(previous, snapshot, scope)
            previous = snapshot
            state = snapshot
        if state is None:
            raise RuntimeError("Multi-Agent Workflow 没有产生状态")
        self._emit_workflow_result(state, scope)
        return self._from_state(state, workspace_id)

    def status(self, *, checkpoint_id: str, scope: EventScope) -> WorkflowOutcome:
        checkpoint = self._load_scoped(checkpoint_id, scope)
        return self._from_checkpoint(checkpoint)

    def resume_approval(
        self,
        *,
        checkpoint_id: str,
        expected_revision: int,
        scope: EventScope,
        request_id: str,
        approved: bool,
    ) -> WorkflowOutcome:
        checkpoint = self._load_scoped(checkpoint_id, scope)
        request = checkpoint.approval_request
        if request is None:
            raise RuntimeError("当前 Checkpoint 没有待处理 ApprovalRequest")
        if request.request_id != request_id:
            raise RuntimeError("ApprovalRequest ID 与权威 Checkpoint 不匹配")
        decision = ApprovalDecision(
            request_id=request.request_id,
            approved=approved,
            scope=request.scope,
            fingerprint=request.fingerprint,
        )
        return self._resume_graph(
            checkpoint,
            expected_revision=expected_revision,
            scope=scope,
            approval_decision=decision,
        )

    def recover(
        self,
        *,
        checkpoint_id: str,
        expected_revision: int,
        scope: EventScope,
        execution_id: str,
        decision: RecoveryDecision,
    ) -> WorkflowOutcome:
        checkpoint = self._load_scoped(checkpoint_id, scope)
        if checkpoint.identity.execution_id != execution_id:
            raise RuntimeError("execution_id 与权威 Checkpoint 不匹配")
        return self._resume_graph(
            checkpoint,
            expected_revision=expected_revision,
            scope=scope,
            recovery_decision=decision,
        )

    def reconcile(
        self,
        *,
        checkpoint_id: str,
        expected_revision: int,
        scope: EventScope,
        execution_id: str,
        submission: ReconcileSubmission,
    ) -> WorkflowOutcome:
        checkpoint = self._load_scoped(checkpoint_id, scope)
        if checkpoint.identity.execution_id != execution_id:
            raise RuntimeError("execution_id 与权威 Checkpoint 不匹配")
        result = ToolResult(
            tool_call_id=submission.tool_call_id,
            tool_name=submission.tool_name,
            ok=submission.ok,
            output=submission.output,
            error=(
                ToolError.model_validate(submission.error)
                if submission.error is not None
                else None
            ),
        )
        reconciliation = ReconcileResult(
            result=result,
            reconciled_by=submission.reconciled_by,
            evidence=submission.evidence,
        )
        return self._resume_graph(
            checkpoint,
            expected_revision=expected_revision,
            scope=scope,
            reconciliation=reconciliation,
        )

    def _resume_graph(
        self,
        checkpoint: ExecutionCheckpoint,
        *,
        expected_revision: int,
        scope: EventScope,
        approval_decision: ApprovalDecision | None = None,
        recovery_decision: RecoveryDecision | None = None,
        reconciliation: ReconcileResult | None = None,
    ) -> WorkflowOutcome:
        workspace_id = checkpoint.scope.workspace_id
        workflow = self.workflow_factory(scope.session_id, workspace_id)
        self.event_bus.emit(
            "workflow_resumed",
            scope=scope.model_copy(
                update={
                    "task_id": checkpoint.scope.task_id,
                    "run_id": checkpoint.identity.run_id,
                    "checkpoint_id": checkpoint.checkpoint_id,
                }
            ),
            source="workflow_adapter",
            correlation_id=checkpoint.scope.task_id,
            message="通过 Graph Resume Entry 恢复 Workflow",
            data={"expected_revision": expected_revision},
        )
        state = workflow.resume(
            checkpoint.checkpoint_id,
            expected_revision=expected_revision,
            approval_decision=approval_decision,
            recovery_decision=recovery_decision,
            reconciliation=reconciliation,
        )
        resumed_scope = scope.model_copy(update={"task_id": state["task_id"]})
        self._emit_workflow_result(state, resumed_scope)
        return self._from_state(state, workspace_id)

    def _load_scoped(
        self,
        checkpoint_id: str,
        scope: EventScope,
    ) -> ExecutionCheckpoint:
        checkpoint = self.checkpoint_store.load(checkpoint_id)
        if checkpoint.scope.session_id != scope.session_id:
            raise RuntimeError("Checkpoint session scope 不匹配")
        if scope.workspace_id and checkpoint.scope.workspace_id != scope.workspace_id:
            raise RuntimeError("Checkpoint workspace scope 不匹配")
        return checkpoint

    @staticmethod
    def _require_task_scope(scope: EventScope) -> tuple[str, str]:
        if scope.task_id is None or scope.workspace_id is None:
            raise RuntimeError("Workflow 必须绑定 task_id 与 workspace_id")
        return scope.task_id, scope.workspace_id

    def _emit_workflow_result(self, state: TikiState, scope: EventScope) -> None:
        if state["status"] == "completed":
            self.event_bus.emit(
                "workflow_completed",
                scope=scope,
                source="workflow_adapter",
                correlation_id=state["task_id"],
                message=state["final_result"] or "Workflow 已完成",
            )
        # Tool/Approval/Recovery 暂停属于 Harness 生命周期，由 Harness Adapter 发布。

    def _emit_snapshot(
        self,
        previous: TikiState | None,
        current: TikiState,
        scope: EventScope,
    ) -> None:
        """把 Graph 已产生的结构化状态变化映射成 UI 事件。"""

        correlation_id = current["task_id"]
        if previous is None or current["current_agent"] != previous["current_agent"]:
            self.event_bus.emit(
                "agent_started",
                scope=scope,
                source="workflow_adapter",
                correlation_id=correlation_id,
                message=f"进入 {current['current_agent']}",
                data={"agent": current["current_agent"]},
            )
        decision = current["supervisor_decision"]
        old_decision = previous["supervisor_decision"] if previous else None
        if decision is not None and decision != old_decision:
            self.event_bus.emit(
                "supervisor_decision",
                scope=scope,
                source="workflow_adapter",
                correlation_id=correlation_id,
                message=decision.reason,
                data=decision.model_dump(mode="json"),
            )
        handoff = current["latest_handoff"]
        old_handoff = previous["latest_handoff"] if previous else None
        if handoff is not None and (
            old_handoff is None or handoff.handoff_id != old_handoff.handoff_id
        ):
            self.event_bus.emit(
                "handoff_created",
                scope=scope,
                source="workflow_adapter",
                correlation_id=handoff.handoff_id,
                message=f"{handoff.from_agent} → {handoff.to_agent}",
                data={"context_refs": handoff.context_refs},
            )
        old_results = previous["specialist_results"] if previous else {}
        for agent, result in current["specialist_results"].items():
            if old_results.get(agent, {}).get("result_id") != result.get("result_id"):
                self.event_bus.emit(
                    "specialist_result",
                    scope=scope,
                    source="workflow_adapter",
                    correlation_id=str(result.get("result_id", correlation_id)),
                    message=f"{agent} 返回结果",
                    data={"result_id": result.get("result_id")},
                )
        old_reports = previous["specialist_verifications"] if previous else {}
        for agent, report in current["specialist_verifications"].items():
            old = old_reports.get(agent)
            if old is None or old.verification_id != report.verification_id:
                self.event_bus.emit(
                    "verification_completed",
                    scope=scope,
                    source="workflow_adapter",
                    correlation_id=report.result_id,
                    message=f"{agent} verification passed={report.passed}",
                    data=report.model_dump(mode="json"),
                )

    def _from_state(self, state: TikiState, workspace_id: str) -> WorkflowOutcome:
        if state["runtime_checkpoint_id"] is not None:
            checkpoint = self.checkpoint_store.load(state["runtime_checkpoint_id"])
            return self._from_checkpoint(checkpoint)
        if state["status"] == "completed":
            return WorkflowOutcome(
                status="completed",
                task_id=state["task_id"],
                session_id=state["session_id"],
                workspace_id=workspace_id,
                message=state["final_result"] or "Workflow 已完成",
                final_result=state["final_result"],
            )
        return WorkflowOutcome(
            status="failed",
            task_id=state["task_id"],
            session_id=state["session_id"],
            workspace_id=workspace_id,
            message=state["final_result"] or f"Workflow 终止：{state['status']}",
            final_result=state["final_result"],
        )

    @staticmethod
    def _from_checkpoint(checkpoint: ExecutionCheckpoint) -> WorkflowOutcome:
        status_map = {
            "awaiting_approval": "awaiting_approval",
            "executing": "recovery_required",
            "recovery_required": "recovery_required",
            "awaiting_reconcile": "awaiting_reconcile",
            "completed": "completed",
            "denied": "denied",
            "pending": "recovery_required",
        }
        status = status_map[checkpoint.execution_state]
        request = checkpoint.approval_request
        return WorkflowOutcome(
            status=status,
            task_id=checkpoint.scope.task_id,
            session_id=checkpoint.scope.session_id,
            workspace_id=checkpoint.scope.workspace_id,
            run_id=checkpoint.identity.run_id,
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_revision=checkpoint.revision,
            approval_request_id=request.request_id if request else None,
            approval_fingerprint=request.fingerprint if request else None,
            execution_id=checkpoint.identity.execution_id,
            attempt=checkpoint.identity.attempt,
            tool_call_id=checkpoint.tool_call.tool_call_id,
            tool_name=checkpoint.tool_call.name,
            tool_result=(
                checkpoint.tool_result.model_dump(mode="json")
                if checkpoint.tool_result is not None
                else None
            ),
            message={
                "awaiting_approval": "Workflow 等待人工审批",
                "recovery_required": "Workflow 需要人工恢复决定",
                "awaiting_reconcile": "Workflow 等待真实人工核对结果",
                "completed": "工具执行已完成，Workflow 可继续",
                "denied": "工具执行已被拒绝",
            }[status],
        )
