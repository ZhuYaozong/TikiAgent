"""复现只读调查被强制创建报告的问题，验证任务模式与证据边界。"""

from pathlib import Path
import json

import pytest

from tikiagent.application.bootstrap import ApplicationRuntimeFactory
from tikiagent.orchestration.contracts import SupervisorDecision, SupervisorPlan
from tikiagent.providers.llm.models import ModelResponse, ModelToolCall
from test_artifact_aware_verifier import build_verifier, code_result, handoff


class InspectionModel:
    def __init__(self) -> None:
        self.requests = []
        self.supervisor_steps = 0

    def supervisor_response(self, messages):
        self.supervisor_steps += 1
        if self.supervisor_steps == 1:
            name, args = "update_plan", {"goal": "查询文件", "acceptance_criteria": ["读取文件作为证据"],
                "todos": [{"todo_id": "inspect", "description": "查询 index.html", "owner": "code_agent", "delivery_mode": "inspection",
                           "required_capabilities": ["workspace_read"], "acceptance_criteria": [{"criterion_id": "file", "description": "读取文件证明位置"}]}]}
        elif self.supervisor_steps == 2:
            name, args = "delegate_task", {"todo_id": "inspect", "instruction": "读取 index.html 并回答", "reason": "获取文件证据"}
        elif "repeated_tool_failure" in str(messages):
            name, args = "stop_task", {"reason": "CodeAgent repeated_tool_failure，停止重试"}
        else:
            name, args = "finish_task", {"reason": "证据通过验证"}
        return ModelResponse(assistant_message={"role": "assistant"}, tool_calls=(ModelToolCall(f"sup-{self.supervisor_steps}", name, json.dumps(args)),))

    def complete_structured(self, messages, response_type):
        if response_type is SupervisorPlan:
            return SupervisorPlan(
                goal="查询已有网页位置", required_specialists=["code_agent"],
                acceptance_criteria=["用目录列表和文件读取证明 index.html 存在"],
                code_task_mode="inspection",
            )
        return SupervisorDecision(
            action="delegate", target_agent="code_agent",
            instruction="只读检查 index.html 所在位置并回答，不创建报告或运行测试",
            reason="查询现有文件",
        )

    def complete(self, *, messages, tool_schemas):
        if any(s["name"] == "update_plan" for s in tool_schemas):
            return self.supervisor_response(messages)
        if any(s["name"] == "submit_verification" for s in tool_schemas):
            if not any(m.get("tool_call_id") == "evidence" for m in messages):
                return ModelResponse(assistant_message={"role": "assistant"}, tool_calls=(ModelToolCall("evidence", "read_evidence", '{"evidence_id":"execution:1"}'),))
            return ModelResponse(assistant_message={"role": "assistant"}, tool_calls=(ModelToolCall("verify", "submit_verification", json.dumps({"assessments": [{"criterion_id": "file", "status": "passed", "evidence_refs": ["execution:1"], "reason": "读取了目标文件"}], "recommendation": "完成"})),))
        self.requests.append((messages, tool_schemas))
        if len(self.requests) == 1:
            return ModelResponse(
                assistant_message={"role": "assistant"},
                tool_calls=(
                    ModelToolCall("list-1", "list_files", '{"path":"."}'),
                    ModelToolCall("read-1", "read_file", '{"path":"index.html"}'),
                ),
            )
        text = "index.html 位于当前 Workspace 根目录。宿主端 History/Handoff 的保存位置请使用本地 /paths 查看。"
        return ModelResponse(assistant_message={"role": "assistant", "content": text}, final_text=text)


def test_read_only_question_finishes_after_one_delegation_without_new_files(tmp_path: Path):
    factory = ApplicationRuntimeFactory(tmp_path / "data", env_file=tmp_path / "missing.env")
    model = InspectionModel()
    factory.model = model
    workflow = factory._build_workflow("session-1", "workspace-1")
    workspace = tmp_path / "data/workspaces/session-1"
    (workspace / "index.html").write_text("<html><body>existing</body></html>", encoding="utf-8")
    state = workflow.invoke("生成的网页文件在哪里，handoff 保存在哪里", session_id="session-1")
    assert state["status"] == "completed"
    assert state["delegation_count"] == 1
    assert state["code_tool_call_count"] == 2
    assert state["latest_handoff"].delivery_mode == "inspection"
    assert state["specialist_verifications"]["code_agent"].passed
    assert "index.html" in state["final_result"]
    assert [file.name for file in workspace.iterdir()] == ["index.html"]
    for _, schemas in model.requests:
        assert {s["name"] for s in schemas} == {"read_file", "list_files", "grep", "inspect_python_environment"}


def test_loop_stop_reaches_application_final_answer_and_keeps_error_evidence(tmp_path):
    from tikiagent.application.events import EventBus, CollectingEventSink
    from tikiagent.application.models import IntentDecision

    class RepeatingModel(InspectionModel):
        def complete_structured(self, messages, response_type):
            if response_type is IntentDecision:
                return IntentDecision(intent="WORKFLOW", reason="文件任务")
            if response_type is SupervisorPlan:
                return SupervisorPlan(goal="创建文件", required_specialists=["code_agent"], acceptance_criteria=["交付文件"])
            return super().complete_structured(messages=messages, response_type=response_type)

        def complete(self, *, messages, tool_schemas):
            if any(s["name"] == "update_plan" for s in tool_schemas):
                return self.supervisor_response(messages)
            self.requests.append((messages, tool_schemas))
            return ModelResponse(assistant_message={"role": "assistant"}, tool_calls=(
                ModelToolCall(f"missing-{len(self.requests)}", "read_file", '{"path":"missing.txt"}'),
            ))

    sink = CollectingEventSink()
    bus = EventBus()
    bus.subscribe(sink)
    factory = ApplicationRuntimeFactory(tmp_path / "data", env_file=tmp_path / "missing.env", event_bus=bus)
    factory.model = RepeatingModel()
    controller = factory.build_controller()
    session = controller.new_session(workspace_id="w")
    outcome = controller.submit(session_id=session.session_id, user_input="创建文件")
    assert outcome.status == "workflow_failed"
    assert "repeated_tool_failure" in outcome.message
    assert any(event.event_type == "final_answer" and "repeated_tool_failure" in event.message for event in sink.events)
    history = factory.data_dir / "histories" / f"{session.session_id}.jsonl"
    assert "repeated_tool_failure" in history.read_text(encoding="utf-8")


@pytest.mark.parametrize("mode,expected", [("inspection", True), ("artifact", False)])
def test_verification_mode_is_bound_to_handoff_not_result(tmp_path, mode, expected):
    (tmp_path / "index.html").write_text("existing", encoding="utf-8")
    verifier, dispatcher, context = build_verifier(tmp_path)
    read = dispatcher.dispatch({"tool_call_id": "read", "name": "read_file", "arguments": {"path": "index.html"}})
    result = code_result([]).model_copy(update={"tool_results": (read.model_dump(mode="json"),)})
    report = verifier.verify(
        handoff=handoff().model_copy(update={"delivery_mode": mode}), result=result,
        specialist_results={}, execution_context=context,
    )
    assert report.passed is expected
    assert report.result_id == result.result_id


def test_inspection_rejects_missing_forged_or_modified_evidence(tmp_path):
    (tmp_path / "index.html").write_text("real", encoding="utf-8")
    verifier, dispatcher, context = build_verifier(tmp_path)
    read = dispatcher.dispatch({"tool_call_id": "read", "name": "read_file", "arguments": {"path": "index.html"}})
    forged = read.model_copy(update={"output": {"path": "index.html", "content": "invented"}})
    for observations, changed in [((), []), ((forged.model_dump(mode="json"),), []), ((read.model_dump(mode="json"),), ["report.md"])]:
        report = verifier.verify(
            handoff=handoff().model_copy(update={"delivery_mode": "inspection"}),
            result=code_result(changed).model_copy(update={"tool_results": observations}),
            specialist_results={}, execution_context=context,
        )
        assert not report.passed


def test_debugging_can_create_missing_report_without_shell_workaround(tmp_path):
    factory = ApplicationRuntimeFactory(tmp_path / "data", env_file=tmp_path / "missing.env")
    workflow = factory._build_workflow("session-1", "workspace-1")
    agent = workflow.code_agent.agent
    # 直接在 debugging Profile 下走正式 Harness，证实新建报告工具可用。
    from tikiagent.context.tool_selection import ToolSelector
    from tikiagent.harness.scope import ExecutionContext, ExecutionScope
    view = ToolSelector().select(agent="code_agent", phase="debugging", registry=agent.dispatcher.registry)
    outcome = agent.execution_coordinator.harness.handle(
        {"tool_call_id": "write", "name": "write_file", "arguments": {"path": "report.md", "content": "evidence"}},
        context=ExecutionContext(scope=ExecutionScope(task_id="t", session_id="session-1", workspace_id="workspace-1"), agent="code_agent", exposed_tools=view.exposed_names),
    )
    assert outcome.tool_result.ok
    assert (tmp_path / "data/workspaces/session-1/report.md").read_text() == "evidence"
