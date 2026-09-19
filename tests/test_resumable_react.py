"""多 ToolCall 固定顺序、ASK 暂停和跨进程 ReAct Resume 测试。"""

from collections.abc import Mapping, Sequence
from typing import Any
import json

import pytest

from tikiagent.context.models import BaseContext, WorkingMemory
from tikiagent.harness.coordinator import ExecutionCoordinator
from tikiagent.harness.execution import ExecutionHarness
from tikiagent.harness.permissions.models import ApprovalDecision, PermissionDecision
from tikiagent.harness.persistence.checkpoint import (
    CheckpointConflictError,
    HistoryResumeReference,
    JsonCheckpointStore,
    WorkflowResumeSnapshot,
)
from tikiagent.harness.persistence.trace import JsonlTraceStore
from tikiagent.harness.scope import ExecutionContext, ExecutionScope
from tikiagent.harness.workspace import Workspace
from tikiagent.providers.llm.models import ModelResponse, ModelToolCall
from tikiagent.runtime.models import AgentRunPause, AgentRunResult
from tikiagent.runtime.resumable import ResumableReActAgent
from tikiagent.tools.dispatcher import Dispatcher
from tikiagent.tools.files import build_file_registry


class ScriptedModel:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = responses
        self.requests: list[list[dict[str, Any]]] = []

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tool_schemas: Sequence[Mapping[str, Any]],
    ) -> ModelResponse:
        del tool_schemas
        self.requests.append([dict(item) for item in messages])
        return self.responses.pop(0)


class ReadAllowWriteAsk:
    def decide(self, call, _context) -> PermissionDecision:
        action = "ASK" if call.name == "write_file" else "ALLOW"
        return PermissionDecision(
            action=action,
            rule_id=f"test.{action.lower()}",
            reason=f"test {action}",
        )


def first_response() -> ModelResponse:
    calls = (
        ModelToolCall("call-read", "read_file", '{"path":"input.txt"}'),
        ModelToolCall(
            "call-write",
            "write_file",
            '{"path":"output.txt","content":"done"}',
        ),
    )
    return ModelResponse(
        assistant_message={
            "role": "assistant",
            "tool_calls": [
                {
                    "id": call.tool_call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.arguments_json,
                    },
                }
                for call in calls
            ],
        },
        tool_calls=calls,
    )


def final_response() -> ModelResponse:
    return ModelResponse(
        assistant_message={"role": "assistant", "content": "完成"},
        final_text="完成",
    )


def build_runtime(tmp_path, model):
    workspace = Workspace(tmp_path / "workspace")
    registry = build_file_registry(workspace)
    dispatcher = Dispatcher(registry)
    coordinator = ExecutionCoordinator(
        ExecutionHarness(dispatcher, permission_policy=ReadAllowWriteAsk()),
        JsonCheckpointStore(tmp_path / "checkpoints"),
        JsonlTraceStore(tmp_path / "events.jsonl"),
    )
    return workspace, ResumableReActAgent(
        model,
        dispatcher,
        max_steps=4,
        execution_coordinator=coordinator,
    )


def test_multiple_tool_calls_resume_in_fixed_order_and_pair_atomically(tmp_path) -> None:
    workspace, first_agent = build_runtime(
        tmp_path,
        ScriptedModel([first_response()]),
    )
    workspace.resolve("input.txt").write_text("input", encoding="utf-8")
    context = ExecutionContext(
        scope=ExecutionScope(
            task_id="task-1",
            session_id="session-1",
            workspace_id="workspace-1",
        ),
        agent="code_agent",
        exposed_tools=set(),
    )
    workflow = WorkflowResumeSnapshot(
        state={"task_id": "task-1", "status": "executing"},
        history=HistoryResumeReference(
            path=str(tmp_path / "history.jsonl"),
            cursor=0,
        ),
    )
    base = BaseContext(
        agent="code_agent",
        working_memory=WorkingMemory(
            task="读取后写入",
            phase="coding",
            instruction="读取 input.txt，然后写 output.txt",
        ),
    )

    paused = first_agent.run(
        "读取后写入",
        base_context=base,
        execution_context=context,
        workflow_snapshot=workflow,
    )

    assert isinstance(paused, AgentRunPause)
    assert paused.status == "awaiting_approval"
    checkpoint = first_agent.execution_coordinator.checkpoint_store.load(
        paused.checkpoint_id
    )
    assert checkpoint.react_snapshot.next_tool_index == 1
    assert [
        item.tool_call_id for item in checkpoint.react_snapshot.pending_results
    ] == ["call-read"]
    assert checkpoint.react_snapshot.local_memory["recent_interactions"] == []
    assert not workspace.resolve("output.txt").exists()

    # 模拟新进程：重新创建 Workspace、Ledger、Coordinator 和 Agent。
    _, second_agent = build_runtime(tmp_path, ScriptedModel([final_response()]))
    request = paused.approval_request
    assert request is not None
    result = second_agent.resume(
        paused.checkpoint_id,
        expected_revision=paused.revision,
        approval_decision=ApprovalDecision(
            request_id=request.request_id,
            approved=True,
            scope=request.scope,
            fingerprint=request.fingerprint,
        ),
    )

    assert isinstance(result, AgentRunResult)
    assert [item.tool_call_id for item in result.tool_results] == [
        "call-read",
        "call-write",
    ]
    assert workspace.resolve("output.txt").read_text(encoding="utf-8") == "done"
    tool_messages = second_agent.model.requests[0][-2:]
    assert [item["tool_call_id"] for item in tool_messages] == [
        "call-read",
        "call-write",
    ]
    with pytest.raises(CheckpointConflictError, match="revision"):
        second_agent.resume(
            paused.checkpoint_id,
            expected_revision=paused.revision,
            approval_decision=ApprovalDecision(
                request_id=request.request_id,
                approved=True,
                scope=request.scope,
                fingerprint=request.fingerprint,
            ),
        )
