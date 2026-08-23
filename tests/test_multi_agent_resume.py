"""Graph Resume Entry Router 与跨进程 History 恢复集成测试。"""

from collections.abc import Mapping, Sequence
from typing import Any

from tikiagent.agents import MultiAgentCodeAgent, ResumableReActAgent
from tikiagent.context import JsonlHistoryStore
from tikiagent.harness import (
    ApprovalDecision,
    Dispatcher,
    ExecutionCoordinator,
    ExecutionHarness,
    JsonCheckpointStore,
    JsonlTraceStore,
    PermissionDecision,
    Workspace,
    build_file_registry,
)
from tikiagent.llm import ModelResponse, ModelToolCall
from tikiagent.orchestration.models import (
    SupervisorDecision,
    SupervisorPlan,
    VerificationReport,
)
from tikiagent.orchestration.multi_agent import MultiAgentWorkflow


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


class CodeOnlySupervisor:
    def plan(self, _task: str) -> SupervisorPlan:
        return SupervisorPlan(
            goal="生成文件",
            required_specialists=["code_agent"],
            acceptance_criteria=["最新 CodeResult 通过验证"],
        )

    def decide(self, state, _base_context) -> SupervisorDecision:
        report = state["specialist_verifications"].get("code_agent")
        if report is not None and report.passed:
            return SupervisorDecision(
                action="finish",
                target_agent=None,
                instruction="",
                reason="最新结果已通过验证",
            )
        return SupervisorDecision(
            action="delegate",
            target_agent="code_agent",
            instruction="读取 input.txt 并生成 output.txt",
            reason="需要代码执行",
        )


class PassGate:
    def verify(self, *, handoff, raw_result, specialist_results):
        del specialist_results
        return VerificationReport(
            result_id=raw_result["result_id"],
            handoff_id=handoff.handoff_id,
            subject_agent="code_agent",
            mode="environment",
            passed=True,
            checks=[],
            failures=[],
            evidence=["output.txt 已生成"],
            recommendation="finish",
        )


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
        ModelToolCall("read-1", "read_file", '{"path":"input.txt"}'),
        ModelToolCall(
            "write-1",
            "write_file",
            '{"path":"output.txt","content":"graph resumed"}',
        ),
    )
    return ModelResponse(
        assistant_message={"role": "assistant", "tool_calls": []},
        tool_calls=calls,
    )


def final_response() -> ModelResponse:
    return ModelResponse(
        assistant_message={"role": "assistant", "content": "交付完成"},
        final_text="交付完成",
    )


def build_workflow(tmp_path, model, *, history_store=None):
    workspace = Workspace(tmp_path / "workspace")
    registry = build_file_registry(workspace)
    dispatcher = Dispatcher(registry)
    coordinator = ExecutionCoordinator(
        ExecutionHarness(dispatcher, permission_policy=ReadAllowWriteAsk()),
        JsonCheckpointStore(tmp_path / "checkpoints"),
        JsonlTraceStore(tmp_path / "events.jsonl"),
    )
    agent = ResumableReActAgent(
        model,
        dispatcher,
        max_steps=4,
        execution_coordinator=coordinator,
    )
    workflow = MultiAgentWorkflow(
        supervisor=CodeOnlySupervisor(),
        research_agent=None,
        code_agent=MultiAgentCodeAgent(agent),
        verification_gate=PassGate(),
        workspace_id="workspace-1",
        history_store=history_store,
    )
    return workspace, workflow


def test_resume_reenters_graph_and_restores_history_in_new_process(tmp_path) -> None:
    history_path = tmp_path / "history.jsonl"
    workspace, first = build_workflow(
        tmp_path,
        ScriptedModel([first_response()]),
        history_store=JsonlHistoryStore(history_path),
    )
    workspace.resolve("input.txt").write_text("input", encoding="utf-8")

    paused = first.invoke(
        "生成 output.txt",
        task_id="task-1",
        session_id="session-1",
    )

    assert paused["status"] == "awaiting_approval"
    checkpoint_id = paused["runtime_checkpoint_id"]
    revision = paused["runtime_checkpoint_revision"]
    assert checkpoint_id is not None and revision is not None
    checkpoint = first.code_agent.agent.execution_coordinator.checkpoint_store.load(
        checkpoint_id
    )
    request = checkpoint.approval_request
    assert request is not None
    assert len(first.history_for(paused)) == 1  # 暂停前只有 Handoff。

    # 新建完整 Workflow；resume() 只加载快照，实际续跑发生在 resume_entry Node。
    second_model = ScriptedModel([final_response()])
    _, second = build_workflow(tmp_path, second_model)
    completed = second.resume(
        checkpoint_id,
        expected_revision=revision,
        approval_decision=ApprovalDecision(
            request_id=request.request_id,
            approved=True,
            scope=request.scope,
            fingerprint=request.fingerprint,
        ),
    )

    assert "resume_entry" in second.graph.nodes
    assert completed["status"] == "completed"
    assert workspace.resolve("output.txt").read_text(encoding="utf-8") == "graph resumed"
    assert [item.record_type for item in second.history_for(completed)] == [
        "handoff",
        "result",
        "verification",
        "result",
    ]
    assert [
        item["tool_call_id"] for item in second_model.requests[0][-2:]
    ] == ["read-1", "write-1"]
