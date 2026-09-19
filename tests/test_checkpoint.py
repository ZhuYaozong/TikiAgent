"""双层快照、原子 Checkpoint 与 revision 测试。"""

import json

import pytest

from tikiagent.harness.persistence.checkpoint import (
    CheckpointConflictError,
    CheckpointIntegrityError,
    ExecutionCheckpoint,
    ExecutionIdentity,
    HistoryResumeReference,
    JsonCheckpointStore,
    PendingModelToolCall,
    ReActRunSnapshot,
    WorkflowResumeSnapshot,
)
from tikiagent.harness.scope import ExecutionScope
from tikiagent.tools.models import ValidatedToolCall


def checkpoint() -> ExecutionCheckpoint:
    return ExecutionCheckpoint(
        checkpoint_id="checkpoint-1",
        identity=ExecutionIdentity(
            run_id="run-1",
            execution_id="execution-1",
            tool_call_id="call-1",
            attempt=1,
        ),
        scope=ExecutionScope(
            task_id="task-1",
            session_id="session-1",
            workspace_id="workspace-1",
        ),
        agent="code_agent",
        exposed_tools={"read_file"},
        tool_call=ValidatedToolCall(
            tool_call_id="call-1",
            name="read_file",
            arguments={"path": "README.md"},
        ),
        approval_state="not_required",
        execution_state="executing",
        workflow_snapshot=WorkflowResumeSnapshot(
            state={"task_id": "task-1", "status": "executing"},
            history=HistoryResumeReference(
                path="D:/tmp/history.jsonl",
                cursor=3,
            ),
        ),
        react_snapshot=ReActRunSnapshot(
            task="读取 README",
            step=2,
            base_context={"agent": "code_agent"},
            local_memory={"summary": None, "recent_interactions": []},
            pending_assistant_message={"role": "assistant"},
            pending_tool_calls=[
                PendingModelToolCall(
                    tool_call_id="call-1",
                    name="read_file",
                    arguments_json='{"path":"README.md"}',
                )
            ],
            next_tool_index=0,
        ),
    )


def test_checkpoint_atomically_contains_workflow_and_react_snapshots(tmp_path) -> None:
    store = JsonCheckpointStore(tmp_path / "checkpoints")
    value = checkpoint()

    store.save(value)
    loaded = store.load(value.checkpoint_id)

    assert loaded == value
    assert loaded.workflow_snapshot.history.cursor == 3
    assert loaded.react_snapshot.pending_tool_calls[0].tool_call_id == "call-1"


def test_revision_prevents_duplicate_resume(tmp_path) -> None:
    store = JsonCheckpointStore(tmp_path / "checkpoints")
    value = checkpoint()
    store.save(value)
    updated = ExecutionCheckpoint.model_validate(
        {
            **value.model_dump(mode="python"),
            "revision": 2,
            "recovery_note": "first resume",
        }
    )
    store.save(updated, expected_revision=1)

    with pytest.raises(CheckpointConflictError, match="revision"):
        store.save(updated, expected_revision=1)


def test_checksum_detects_accidental_checkpoint_change(tmp_path) -> None:
    store = JsonCheckpointStore(tmp_path / "checkpoints")
    value = checkpoint()
    store.save(value)
    path = tmp_path / "checkpoints" / "checkpoint-1.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["payload"]["agent"] = "tampered"
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(CheckpointIntegrityError, match="checksum"):
        store.load(value.checkpoint_id)
