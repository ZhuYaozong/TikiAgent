"""把 Gate、Checkpoint、Recovery 和 Trace 串成崩溃安全执行生命周期。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from uuid import uuid4

from tikiagent.harness.execution import ExecutionHarness
from tikiagent.harness.models import HarnessOutcome
from tikiagent.harness.permissions.models import ApprovalDecision
from tikiagent.harness.persistence.checkpoint import (
    CheckpointConflictError,
    ExecutionCheckpoint,
    JsonCheckpointStore,
    ReActRunSnapshot,
    WorkflowResumeSnapshot,
    new_execution_identity,
    next_checkpoint,
)
from tikiagent.harness.persistence.recovery import ReconcileResult, RecoveryDecision
from tikiagent.harness.persistence.trace import JsonlTraceStore
from tikiagent.harness.scope import ExecutionContext, ExecutionScope
from tikiagent.tools.models import ToolCall, ToolResult, ValidatedToolCall


@dataclass(frozen=True, slots=True)
class CoordinatedOutcome:
    """Harness 结果及其持久化位置。"""

    outcome: HarnessOutcome
    checkpoint: ExecutionCheckpoint | None


@dataclass(frozen=True, slots=True)
class ExecutionLifecycleEvent:
    """Harness 已实际发生的生命周期事实，不是恢复权威来源。"""

    event_type: str
    scope: ExecutionScope
    run_id: str
    tool_call_id: str
    tool_name: str
    checkpoint: ExecutionCheckpoint | None = None
    details: dict[str, Any] | None = None


class ExecutionLifecycleObserver(Protocol):
    def handle(self, event: ExecutionLifecycleEvent) -> None: ...


class ExecutionCoordinator:
    """Checkpoint 是 Resume source of truth；Trace 只做旁路记录。"""

    def __init__(
        self,
        harness: ExecutionHarness,
        checkpoint_store: JsonCheckpointStore,
        trace_store: JsonlTraceStore,
        lifecycle_observer: ExecutionLifecycleObserver | None = None,
    ) -> None:
        self.harness = harness
        self.checkpoint_store = checkpoint_store
        self.trace_store = trace_store
        self.lifecycle_observer = lifecycle_observer

    def execute(
        self,
        raw_tool_call: Mapping[str, Any],
        *,
        context: ExecutionContext,
        workflow_snapshot: WorkflowResumeSnapshot,
        react_snapshot: ReActRunSnapshot,
        run_id: str | None = None,
    ) -> CoordinatedOutcome:
        """首次执行；ASK 暂停，ALLOW 在 handler 前先保存 executing。"""

        current_run_id = run_id or str(uuid4())
        executing: ExecutionCheckpoint | None = None
        self._notify(
            ExecutionLifecycleEvent(
                event_type="tool_call_requested",
                scope=context.scope,
                run_id=current_run_id,
                tool_call_id=str(raw_tool_call.get("tool_call_id", "unknown")),
                tool_name=str(raw_tool_call.get("name", "unknown")),
                details={"arguments": raw_tool_call.get("arguments", {})},
            )
        )

        def before_execute(call: ValidatedToolCall) -> None:
            nonlocal executing
            executing = self._new_checkpoint(
                call=call,
                context=context,
                workflow_snapshot=workflow_snapshot,
                react_snapshot=react_snapshot,
                run_id=current_run_id,
                approval_state="not_required",
                execution_state="executing",
            )
            self.checkpoint_store.save(executing)
            self._trace(executing, "checkpoint_saved")
            self._trace(executing, "tool_execution_started")

        outcome = self.harness.handle(
            raw_tool_call,
            context=context,
            before_execute=before_execute,
        )
        if outcome.status == "awaiting_approval":
            request = outcome.approval_request
            assert request is not None
            checkpoint = self._new_checkpoint(
                call=request.tool_call,
                context=context,
                workflow_snapshot=workflow_snapshot,
                react_snapshot=react_snapshot,
                run_id=current_run_id,
                approval_state="awaiting",
                execution_state="awaiting_approval",
                approval_request=request,
            )
            self.checkpoint_store.save(checkpoint)
            self._trace(checkpoint, "approval_requested")
            self._trace(checkpoint, "checkpoint_saved")
            return CoordinatedOutcome(outcome, checkpoint)
        if executing is None:
            # Exposure、参数或 Permission DENY 没有副作用，不需要恢复点。
            result = outcome.tool_result
            if result is not None:
                self._notify(
                    ExecutionLifecycleEvent(
                        event_type="tool_execution_denied",
                        scope=context.scope,
                        run_id=current_run_id,
                        tool_call_id=result.tool_call_id,
                        tool_name=result.tool_name,
                        details={"tool_result": result.model_dump(mode="json")},
                    )
                )
            return CoordinatedOutcome(outcome, None)
        completed = self._complete(executing, outcome)
        return CoordinatedOutcome(outcome, completed)

    def resume_approval(
        self,
        checkpoint_id: str,
        *,
        decision: ApprovalDecision,
        expected_revision: int,
    ) -> CoordinatedOutcome:
        """审批恢复；先 CAS 保存 executing，再允许消费审批和启动 handler。"""

        checkpoint = self._load_expected(checkpoint_id, expected_revision)
        if checkpoint.execution_state != "awaiting_approval":
            raise CheckpointConflictError("当前 Checkpoint 不在 awaiting_approval")
        request = checkpoint.approval_request
        assert request is not None
        self.harness.approval_gate.restore_request(request)
        executing: ExecutionCheckpoint | None = None

        def before_execute(_call: ValidatedToolCall) -> None:
            nonlocal executing
            self._trace(
                checkpoint,
                "approval_decided",
                {"approved": True},
            )
            executing = next_checkpoint(
                checkpoint,
                approval_state="granted",
                execution_state="executing",
            )
            self.checkpoint_store.save(
                executing,
                expected_revision=checkpoint.revision,
            )
            self._trace(executing, "checkpoint_saved")
            self._trace(executing, "tool_execution_started")

        outcome = self.harness.handle(
            self._raw_call(checkpoint.tool_call),
            context=self._context(checkpoint),
            approval_request=request,
            approval_decision=decision,
            before_execute=before_execute,
        )
        if executing is None:
            result = outcome.tool_result
            assert result is not None and result.error is not None
            if result.error.code != "approval_rejected":
                # 无效 Decision 不消费请求，也不改变权威 Checkpoint。
                raise CheckpointConflictError(result.error.message)
            self._trace(
                checkpoint,
                "approval_decided",
                {"approved": False},
            )
            denied = next_checkpoint(
                checkpoint,
                approval_state="rejected",
                execution_state="denied",
                tool_result=outcome.tool_result,
            )
            self.checkpoint_store.save(
                denied,
                expected_revision=checkpoint.revision,
            )
            self._trace(denied, "checkpoint_saved")
            return CoordinatedOutcome(outcome, denied)
        return CoordinatedOutcome(outcome, self._complete(executing, outcome))

    def mark_interrupted(
        self,
        checkpoint_id: str,
        *,
        expected_revision: int,
    ) -> ExecutionCheckpoint:
        """新进程看到 executing 时保守进入 recovery_required。"""

        checkpoint = self._load_expected(checkpoint_id, expected_revision)
        if checkpoint.execution_state == "recovery_required":
            return checkpoint
        if checkpoint.execution_state != "executing":
            raise CheckpointConflictError("只有 executing Checkpoint 需要崩溃恢复")
        recovered = next_checkpoint(
            checkpoint,
            execution_state="recovery_required",
            recovery_note="进程在 handler 完成前退出，禁止自动重放",
        )
        self.checkpoint_store.save(
            recovered,
            expected_revision=checkpoint.revision,
        )
        self._trace(recovered, "recovery_required")
        return recovered

    def apply_recovery(
        self,
        checkpoint_id: str,
        *,
        decision: RecoveryDecision,
        expected_revision: int,
    ) -> ExecutionCheckpoint:
        checkpoint = self._load_expected(checkpoint_id, expected_revision)
        if checkpoint.execution_state != "recovery_required":
            raise CheckpointConflictError("RecoveryDecision 只接受 recovery_required")
        if decision.action == "confirmed_executed":
            updated = next_checkpoint(
                checkpoint,
                execution_state="awaiting_reconcile",
                recovery_note=decision.reason,
                tool_result=None,
            )
        else:
            updated = next_checkpoint(
                checkpoint,
                identity=new_execution_identity(
                    run_id=checkpoint.identity.run_id,
                    tool_call_id=checkpoint.tool_call.tool_call_id,
                    attempt=checkpoint.identity.attempt + 1,
                ),
                approval_state="not_required",
                execution_state="pending",
                approval_request=None,
                tool_result=None,
                recovery_note=decision.reason,
            )
        self.checkpoint_store.save(
            updated,
            expected_revision=checkpoint.revision,
        )
        self._trace(
            updated,
            "recovery_decided",
            {"action": decision.action, "decided_by": decision.decided_by},
        )
        return updated

    def retry_confirmed_not_executed(
        self,
        checkpoint_id: str,
        *,
        expected_revision: int,
    ) -> CoordinatedOutcome:
        """人工确认未执行后重走完整 Gate；ASK 会生成全新审批。"""

        checkpoint = self._load_expected(checkpoint_id, expected_revision)
        if checkpoint.execution_state != "pending":
            raise CheckpointConflictError("只有 pending Recovery 可以重试")
        context = self._context(checkpoint)
        executing: ExecutionCheckpoint | None = None

        def before_execute(_call: ValidatedToolCall) -> None:
            nonlocal executing
            executing = next_checkpoint(
                checkpoint,
                approval_state="not_required",
                execution_state="executing",
            )
            self.checkpoint_store.save(
                executing,
                expected_revision=checkpoint.revision,
            )
            self._trace(executing, "tool_execution_started")

        outcome = self.harness.handle(
            self._raw_call(checkpoint.tool_call),
            context=context,
            before_execute=before_execute,
        )
        if outcome.status == "awaiting_approval":
            request = outcome.approval_request
            assert request is not None
            awaiting = next_checkpoint(
                checkpoint,
                approval_state="awaiting",
                execution_state="awaiting_approval",
                approval_request=request,
            )
            self.checkpoint_store.save(
                awaiting,
                expected_revision=checkpoint.revision,
            )
            self._trace(awaiting, "approval_requested")
            return CoordinatedOutcome(outcome, awaiting)
        if executing is None:
            result = outcome.tool_result
            assert result is not None
            denied = next_checkpoint(
                checkpoint,
                execution_state="denied",
                tool_result=result,
            )
            self.checkpoint_store.save(
                denied,
                expected_revision=checkpoint.revision,
            )
            self._trace(denied, "tool_execution_denied")
            return CoordinatedOutcome(outcome, denied)
        return CoordinatedOutcome(outcome, self._complete(executing, outcome))

    def reconcile(
        self,
        checkpoint_id: str,
        *,
        reconciliation: ReconcileResult,
        expected_revision: int,
    ) -> ExecutionCheckpoint:
        """只有人工提供真实 Result 后，confirmed_executed 才能继续。"""

        checkpoint = self._load_expected(checkpoint_id, expected_revision)
        if checkpoint.execution_state != "awaiting_reconcile":
            raise CheckpointConflictError("当前 Checkpoint 不等待 reconcile")
        result = reconciliation.result
        if (
            result.tool_call_id != checkpoint.tool_call.tool_call_id
            or result.tool_name != checkpoint.tool_call.name
        ):
            raise ValueError("ReconcileResult 未绑定当前 ToolCall")
        completed = next_checkpoint(
            checkpoint,
            execution_state="completed",
            tool_result=result,
            manually_reconciled=True,
            reconcile_evidence=reconciliation.evidence,
            recovery_note=(
                f"由 {reconciliation.reconciled_by} 提供人工 reconcile result"
            ),
        )
        self.checkpoint_store.save(
            completed,
            expected_revision=checkpoint.revision,
        )
        self._trace(completed, "execution_reconciled")
        return completed

    def _complete(
        self,
        executing: ExecutionCheckpoint,
        outcome: HarnessOutcome,
    ) -> ExecutionCheckpoint:
        result = outcome.tool_result
        assert result is not None
        completed = next_checkpoint(
            executing,
            execution_state="completed",
            tool_result=result,
        )
        self.checkpoint_store.save(
            completed,
            expected_revision=executing.revision,
        )
        self._trace(
            completed,
            "tool_execution_finished",
            {"harness_status": outcome.status, "tool_ok": result.ok},
        )
        return completed

    def _new_checkpoint(
        self,
        *,
        call: ValidatedToolCall,
        context: ExecutionContext,
        workflow_snapshot: WorkflowResumeSnapshot,
        react_snapshot: ReActRunSnapshot,
        run_id: str,
        approval_state: str,
        execution_state: str,
        approval_request=None,
    ) -> ExecutionCheckpoint:
        return ExecutionCheckpoint.model_validate(
            {
                "identity": new_execution_identity(
                    run_id=run_id,
                    tool_call_id=call.tool_call_id,
                ),
                "scope": context.scope,
                "agent": context.agent,
                "exposed_tools": context.exposed_tools,
                "tool_call": call,
                "approval_state": approval_state,
                "execution_state": execution_state,
                "approval_request": approval_request,
                "workflow_snapshot": workflow_snapshot,
                "react_snapshot": react_snapshot,
            }
        )

    def _load_expected(
        self,
        checkpoint_id: str,
        expected_revision: int,
    ) -> ExecutionCheckpoint:
        checkpoint = self.checkpoint_store.load(checkpoint_id)
        if checkpoint.revision != expected_revision:
            raise CheckpointConflictError(
                f"Checkpoint revision 冲突：expected={expected_revision}, "
                f"actual={checkpoint.revision}"
            )
        return checkpoint

    def _trace(
        self,
        checkpoint: ExecutionCheckpoint,
        event_type: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        try:
            self.trace_store.append(
                run_id=checkpoint.identity.run_id,
                event_type=event_type,
                checkpoint_id=checkpoint.checkpoint_id,
                revision=checkpoint.revision,
                execution_id=checkpoint.identity.execution_id,
                tool_call_id=checkpoint.identity.tool_call_id,
                details=details,
            )
        except (OSError, ValueError):
            # Trace 是 best effort；绝不能回滚或改变已经持久化的执行事实。
            pass
        self._notify(
            ExecutionLifecycleEvent(
                event_type=event_type,
                scope=checkpoint.scope,
                run_id=checkpoint.identity.run_id,
                tool_call_id=checkpoint.identity.tool_call_id,
                tool_name=checkpoint.tool_call.name,
                checkpoint=checkpoint,
                details=details,
            )
        )

    def _notify(self, event: ExecutionLifecycleEvent) -> None:
        if self.lifecycle_observer is None:
            return
        try:
            self.lifecycle_observer.handle(event)
        except Exception:  # noqa: BLE001 - 展示事件绝不能改变执行语义
            pass

    @staticmethod
    def _context(checkpoint: ExecutionCheckpoint) -> ExecutionContext:
        return ExecutionContext(
            scope=checkpoint.scope,
            agent=checkpoint.agent,
            exposed_tools=checkpoint.exposed_tools,
        )

    @staticmethod
    def _raw_call(call: ValidatedToolCall) -> dict[str, Any]:
        return ToolCall(
            tool_call_id=call.tool_call_id,
            name=call.name,
            arguments=call.arguments,
        ).model_dump(mode="python")
