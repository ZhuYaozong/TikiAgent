"""Approval 持久化顺序与人工 Recovery 测试。"""

from typing import Any

from pydantic import BaseModel, ConfigDict
import pytest

from tikiagent.harness.coordinator import ExecutionCoordinator
from tikiagent.harness.execution import ExecutionHarness
from tikiagent.harness.permissions.models import ApprovalDecision, PermissionDecision
from tikiagent.harness.persistence.checkpoint import (
    CheckpointConflictError,
    HistoryResumeReference,
    JsonCheckpointStore,
    PendingModelToolCall,
    ReActRunSnapshot,
    WorkflowResumeSnapshot,
)
from tikiagent.harness.persistence.recovery import ReconcileResult, RecoveryDecision
from tikiagent.harness.persistence.trace import JsonlTraceStore
from tikiagent.harness.scope import ExecutionContext, ExecutionScope
from tikiagent.tools.dispatcher import Dispatcher
from tikiagent.tools.models import ToolResult
from tikiagent.tools.registry import RegisteredTool, ToolRegistry


class EmptyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AskPolicy:
    def decide(self, _call, _context) -> PermissionDecision:
        return PermissionDecision(
            action="ASK",
            rule_id="test.ask",
            reason="需要人工批准",
        )


class Handler:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, **_arguments: Any) -> dict[str, bool]:
        self.calls += 1
        return {"executed": True}


def fixtures(tmp_path):
    handler = Handler()
    registry = ToolRegistry()
    registry.register(RegisteredTool("install", "install", EmptyArgs, handler))
    harness = ExecutionHarness(
        Dispatcher(registry),
        permission_policy=AskPolicy(),
    )
    coordinator = ExecutionCoordinator(
        harness,
        JsonCheckpointStore(tmp_path / "checkpoints"),
        JsonlTraceStore(tmp_path / "events.jsonl"),
    )
    context = ExecutionContext(
        scope=ExecutionScope(
            task_id="task-1",
            session_id="session-1",
            workspace_id="workspace-1",
        ),
        agent="code_agent",
        exposed_tools={"install"},
    )
    workflow = WorkflowResumeSnapshot(
        state={"task_id": "task-1", "status": "executing"},
        history=HistoryResumeReference(
            path=str(tmp_path / "history.jsonl"),
            cursor=0,
        ),
    )
    react = ReActRunSnapshot(
        task="install",
        step=1,
        base_context={"agent": "code_agent"},
        local_memory={"summary": None, "recent_interactions": []},
        pending_assistant_message={"role": "assistant"},
        pending_tool_calls=[
            PendingModelToolCall(
                tool_call_id="call-1",
                name="install",
                arguments_json="{}",
            )
        ],
        next_tool_index=0,
    )
    return handler, coordinator, context, workflow, react


def test_approval_is_consumed_only_after_executing_checkpoint(tmp_path) -> None:
    handler, coordinator, context, workflow, react = fixtures(tmp_path)
    paused = coordinator.execute(
        {"tool_call_id": "call-1", "name": "install", "arguments": {}},
        context=context,
        workflow_snapshot=workflow,
        react_snapshot=react,
        run_id="run-1",
    )
    checkpoint = paused.checkpoint
    request = paused.outcome.approval_request
    assert checkpoint is not None and request is not None

    decision = ApprovalDecision(
        request_id=request.request_id,
        approved=True,
        scope=request.scope,
        fingerprint=request.fingerprint,
    )
    resumed = coordinator.resume_approval(
        checkpoint.checkpoint_id,
        decision=decision,
        expected_revision=checkpoint.revision,
    )

    assert handler.calls == 1
    assert resumed.checkpoint.execution_state == "completed"
    assert resumed.checkpoint.revision == 3
    events = coordinator.trace_store.list_events()
    event_types = [event.event_type for event in events]
    assert event_types.index("checkpoint_saved") < event_types.index(
        "tool_execution_started"
    )


def test_confirmed_executed_blocks_until_real_reconcile_result(tmp_path) -> None:
    _, coordinator, context, workflow, react = fixtures(tmp_path)
    paused = coordinator.execute(
        {"tool_call_id": "call-1", "name": "install", "arguments": {}},
        context=context,
        workflow_snapshot=workflow,
        react_snapshot=react,
        run_id="run-1",
    )
    original = paused.checkpoint
    # 模拟批准后已经保存 executing，但进程在记录结果前退出。
    executing = original.model_copy(
        update={
            "revision": 2,
            "approval_state": "granted",
            "execution_state": "executing",
        }
    )
    coordinator.checkpoint_store.save(executing, expected_revision=1)
    recovery = coordinator.mark_interrupted(
        original.checkpoint_id,
        expected_revision=2,
    )
    waiting = coordinator.apply_recovery(
        original.checkpoint_id,
        decision=RecoveryDecision(
            action="confirmed_executed",
            decided_by="operator",
            reason="工作区中已发现副作用",
        ),
        expected_revision=recovery.revision,
    )

    assert waiting.execution_state == "awaiting_reconcile"
    assert waiting.tool_result is None
    with pytest.raises(ValueError, match="未绑定当前 ToolCall"):
        coordinator.reconcile(
            waiting.checkpoint_id,
            reconciliation=ReconcileResult(
                result=ToolResult(
                    tool_call_id="other-call",
                    tool_name="install",
                    ok=True,
                    output={"manually_checked": True},
                ),
                reconciled_by="operator",
                evidence=["artifact:install-log"],
            ),
            expected_revision=waiting.revision,
        )
    completed = coordinator.reconcile(
        waiting.checkpoint_id,
        reconciliation=ReconcileResult(
            result=ToolResult(
                tool_call_id="call-1",
                tool_name="install",
                ok=True,
                output={"manually_checked": True},
            ),
            reconciled_by="operator",
            evidence=["artifact:install-log"],
        ),
        expected_revision=waiting.revision,
    )
    assert completed.manually_reconciled is True
    assert completed.tool_result.output["manually_checked"] is True


def test_executing_checkpoint_failure_does_not_consume_approval_or_run_handler(
    tmp_path,
    monkeypatch,
) -> None:
    handler, coordinator, context, workflow, react = fixtures(tmp_path)
    paused = coordinator.execute(
        {"tool_call_id": "call-1", "name": "install", "arguments": {}},
        context=context,
        workflow_snapshot=workflow,
        react_snapshot=react,
    )
    checkpoint = paused.checkpoint
    request = paused.outcome.approval_request
    assert checkpoint is not None and request is not None
    original_save = coordinator.checkpoint_store.save

    def fail_executing(value, *, expected_revision=None):
        if value.execution_state == "executing":
            raise OSError("disk full")
        return original_save(value, expected_revision=expected_revision)

    monkeypatch.setattr(coordinator.checkpoint_store, "save", fail_executing)
    with pytest.raises(OSError, match="disk full"):
        coordinator.resume_approval(
            checkpoint.checkpoint_id,
            decision=ApprovalDecision(
                request_id=request.request_id,
                approved=True,
                scope=request.scope,
                fingerprint=request.fingerprint,
            ),
            expected_revision=checkpoint.revision,
        )

    assert handler.calls == 0
    assert request.request_id not in coordinator.harness.approval_gate.ledger.resolved
    assert coordinator.checkpoint_store.load(
        checkpoint.checkpoint_id
    ).execution_state == "awaiting_approval"


def test_invalid_approval_does_not_change_authoritative_checkpoint(tmp_path) -> None:
    _, coordinator, context, workflow, react = fixtures(tmp_path)
    paused = coordinator.execute(
        {"tool_call_id": "call-1", "name": "install", "arguments": {}},
        context=context,
        workflow_snapshot=workflow,
        react_snapshot=react,
    )
    checkpoint = paused.checkpoint
    request = paused.outcome.approval_request
    assert checkpoint is not None and request is not None
    wrong_scope = request.scope.model_copy(update={"task_id": "other-task"})

    with pytest.raises(CheckpointConflictError, match="scope"):
        coordinator.resume_approval(
            checkpoint.checkpoint_id,
            decision=ApprovalDecision(
                request_id=request.request_id,
                approved=True,
                scope=wrong_scope,
                fingerprint=request.fingerprint,
            ),
            expected_revision=checkpoint.revision,
        )

    unchanged = coordinator.checkpoint_store.load(checkpoint.checkpoint_id)
    assert unchanged.revision == checkpoint.revision
    assert unchanged.execution_state == "awaiting_approval"
