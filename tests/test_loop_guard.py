"""实际工具预算、重复失败与跨进程 Resume 的回归实验。"""

import json

import pytest

from tikiagent.context.models import BaseContext, WorkingMemory
from tikiagent.harness.permissions.models import ApprovalDecision
from tikiagent.harness.persistence.checkpoint import HistoryResumeReference, WorkflowResumeSnapshot
from tikiagent.harness.scope import ExecutionContext, ExecutionScope
from tikiagent.providers.llm.models import ModelResponse, ModelToolCall
from tikiagent.runtime.guard import AgentLoopStopped, ToolLoopGuard
from tikiagent.runtime.models import AgentRunPause
from tikiagent.tools.models import ToolError, ToolResult
from test_resumable_react import ScriptedModel, build_runtime, first_response


def response(*calls):
    return ModelResponse(assistant_message={"role": "assistant"}, tool_calls=tuple(calls))


def inputs(tmp_path):
    return {
        "base_context": BaseContext(agent="code_agent", working_memory=WorkingMemory(task="task", phase="coding", instruction="task")),
        "execution_context": ExecutionContext(scope=ExecutionScope(task_id="task-1", session_id="session-1", workspace_id="workspace-1"), agent="code_agent", exposed_tools=set()),
        "workflow_snapshot": WorkflowResumeSnapshot(state={"task_id": "task-1"}, history=HistoryResumeReference(path=str(tmp_path / "history.jsonl"), cursor=0)),
    }


def test_repeat_guard_normalizes_arguments_and_resets_after_success():
    guard = ToolLoopGuard(max_calls=20, repeat_limit=3)
    error = ToolResult(tool_call_id="id", tool_name="read_file", ok=False, error=ToolError(code="file_not_found", message="missing"))
    for _ in range(3):
        guard.record("read_file", '{"path":"missing"}', error)
    with pytest.raises(AgentLoopStopped, match="相同参数"):
        guard.check("read_file", '{ "path": "missing" }')
    guard.check("read_file", '{"path":"corrected"}')
    guard.record("read_file", '{"path":"missing"}', ToolResult(tool_call_id="ok", tool_name="read_file", ok=True, output={}))
    guard.check("read_file", '{"path":"missing"}')


def test_repeated_failure_stops_before_fourth_execution(tmp_path):
    model = ScriptedModel([response(ModelToolCall(f"read-{i}", "read_file", '{"path":"missing"}')) for i in range(4)])
    _, agent = build_runtime(tmp_path, model)
    with pytest.raises(AgentLoopStopped) as stopped:
        agent.run("task", **inputs(tmp_path))
    assert stopped.value.reason == "repeated_tool_failure"
    assert len(stopped.value.run_result.tool_results) == 3


def test_large_tool_batch_cannot_bypass_actual_call_budget(tmp_path):
    model = ScriptedModel([response(*(ModelToolCall(f"read-{i}", "read_file", '{"path":"input.txt"}') for i in range(10)))])
    workspace, agent = build_runtime(tmp_path, model)
    workspace.resolve("input.txt").write_text("input", encoding="utf-8")
    agent.max_tool_calls = 2
    with pytest.raises(AgentLoopStopped) as stopped:
        agent.run("task", **inputs(tmp_path))
    assert stopped.value.reason == "tool_budget_exhausted"
    assert len(stopped.value.run_result.tool_results) == 2
    assert len(model.requests) == 1


def test_resume_preserves_consumed_budget_and_does_not_reexecute_approved_write(tmp_path):
    workspace, first = build_runtime(tmp_path, ScriptedModel([first_response()]))
    workspace.resolve("input.txt").write_text("input", encoding="utf-8")
    first.max_tool_calls = 2
    pause = first.run("task", **inputs(tmp_path))
    assert isinstance(pause, AgentRunPause)
    checkpoint = first.execution_coordinator.checkpoint_store.load(pause.checkpoint_id)
    assert checkpoint.react_snapshot.loop_guard["calls_used"] == 1
    _, second = build_runtime(tmp_path, ScriptedModel([response(ModelToolCall("read-after", "read_file", '{"path":"input.txt"}'))]))
    request = pause.approval_request
    with pytest.raises(AgentLoopStopped) as stopped:
        second.resume(pause.checkpoint_id, expected_revision=pause.revision,
                      approval_decision=ApprovalDecision(request_id=request.request_id, approved=True, scope=request.scope, fingerprint=request.fingerprint))
    assert stopped.value.reason == "tool_budget_exhausted"
    assert len(stopped.value.run_result.tool_results) == 2
    assert workspace.resolve("output.txt").read_text() == "done"


def test_task_remaining_budget_is_enforced_before_more_calls(tmp_path):
    _, agent = build_runtime(tmp_path, ScriptedModel([response(ModelToolCall("read", "read_file", '{"path":"missing"}'))]))
    args = inputs(tmp_path)
    args["workflow_snapshot"].state.update({"code_tool_call_count": 60, "max_code_tool_calls": 60})
    with pytest.raises(AgentLoopStopped) as stopped:
        agent.run("task", **args)
    assert stopped.value.reason == "tool_budget_exhausted"
    assert not stopped.value.run_result.tool_results


def test_nonzero_command_exit_counts_as_failure_even_when_tool_ok():
    guard = ToolLoopGuard(max_calls=20, repeat_limit=3)
    args = json.dumps({"command": ["python", "-m", "pytest"]})
    for _ in range(3):
        guard.record("run_command", args, ToolResult(tool_call_id="id", tool_name="run_command", ok=True, output={"exit_code": 1, "timed_out": False}))
    with pytest.raises(AgentLoopStopped):
        guard.check("run_command", args)


def test_repeat_failures_survive_approval_pause_and_new_runtime(tmp_path):
    calls = [ModelToolCall(f"missing-{i}", "read_file", '{"path":"missing.txt"}') for i in range(3)]
    calls.append(ModelToolCall("write", "write_file", '{"path":"report.md","content":"done"}'))
    _, first = build_runtime(tmp_path, ScriptedModel([response(*calls)]))
    pause = first.run("task", **inputs(tmp_path))
    assert isinstance(pause, AgentRunPause)
    _, second = build_runtime(tmp_path, ScriptedModel([response(ModelToolCall("repeat", "read_file", '{ "path": "missing.txt" }'))]))
    request = pause.approval_request
    with pytest.raises(AgentLoopStopped) as stopped:
        second.resume(pause.checkpoint_id, expected_revision=pause.revision,
                      approval_decision=ApprovalDecision(request_id=request.request_id, approved=True, scope=request.scope, fingerprint=request.fingerprint))
    assert stopped.value.reason == "repeated_tool_failure"
    assert len(stopped.value.run_result.tool_results) == 4


def test_stricter_resume_budget_blocks_pending_handler_before_approval_execution(tmp_path):
    workspace, first = build_runtime(tmp_path, ScriptedModel([first_response()]))
    workspace.resolve("input.txt").write_text("input", encoding="utf-8")
    pause = first.run("task", **inputs(tmp_path))
    _, second = build_runtime(tmp_path, ScriptedModel([]))
    second.max_tool_calls = 1
    request = pause.approval_request
    with pytest.raises(AgentLoopStopped):
        second.resume(pause.checkpoint_id, expected_revision=pause.revision,
                      approval_decision=ApprovalDecision(request_id=request.request_id, approved=True, scope=request.scope, fingerprint=request.fingerprint))
    assert not workspace.resolve("output.txt").exists()
