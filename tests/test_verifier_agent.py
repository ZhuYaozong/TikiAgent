"""正式验证链的离线验收：真实工具、假模型，不访问用户会话与网络。"""

import json
from types import SimpleNamespace

import pytest

from tikiagent.agents.verifier import VerifierAgent
from tikiagent.harness.scope import ExecutionContext, ExecutionScope
from tikiagent.harness.workspace import Workspace
from tikiagent.orchestration.contracts import CodeResult, Handoff
from tikiagent.providers.llm.models import ModelResponse, ModelToolCall
from tikiagent.tools.dispatcher import Dispatcher
from tikiagent.tools.files import build_read_only_file_registry
from tikiagent.tools.python_environment import register_python_environment_tools
from tikiagent.verification.gate import VerificationGate


def call(name, args):
    return ModelResponse(assistant_message={"role": "assistant"}, tool_calls=(ModelToolCall(name, name, json.dumps(args)),))


class Script:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def complete(self, **request):
        self.requests.append(request)
        response = next(self.responses)
        return response(**request) if callable(response) else response


def setup(tmp_path, responses, **kwargs):
    registry = build_read_only_file_registry(Workspace(tmp_path))
    register_python_environment_tools(registry, Workspace(tmp_path), tests=True)
    model = Script(responses)
    verifier = VerifierAgent(model, registry, **kwargs)
    result = CodeResult(handoff_id="h", result_id="r", completed=True, summary="文件存在", steps=1,
        tool_results=({"tool_call_id": "read", "tool_name": "read_file", "ok": True, "output": {"path": "a.txt", "content": "hello"}},))
    handoff = Handoff(from_agent="supervisor", to_agent="code_agent", handoff_id="h", todo_id="t", result_id="r", status="completed",
        instruction="确认文件存在", delivery_mode="inspection", acceptance_criteria=[{"criterion_id": "file", "description": "文件存在"}])
    context = ExecutionContext(scope=ExecutionScope(task_id="task", session_id="s", workspace_id="w"), agent="verifier", exposed_tools=set())
    return model, verifier, result, handoff, context


def submission(status="passed", refs=None):
    return call("submit_verification", {"assessments": [{"criterion_id": "file", "status": status,
                 "evidence_refs": ["execution:0"] if refs is None else refs, "reason": "根据实际观察"}], "recommendation": "按报告决定"})


def test_evidence_read_then_submit_and_gate_identity(tmp_path):
    model, verifier, result, handoff, context = setup(tmp_path, [call("read_evidence", {"evidence_id": "execution:0"}), submission()])
    gate = VerificationGate(research_verifier=verifier, code_verifier=verifier)
    report = gate.verify(handoff=handoff, raw_result=result.model_dump(), specialist_results={}, execution_context=context)
    assert report.passed and report.todo_id == "t" and report.mode == "agent"
    assert set(report.evidence_records) == {"execution:0"}
    schemas = {s["name"] for s in model.requests[0]["tool_schemas"]}
    assert not schemas & {"run_command", "write_file", "edit_file", "web_search"}
    assert {"submit_verification", "read_evidence"} <= schemas
    bad = gate.verify(handoff=handoff.model_copy(update={"result_id": "old"}), raw_result=result.model_dump(), specialist_results={}, execution_context=context)
    assert not bad.passed and bad.failure_category == "identity"


@pytest.mark.parametrize("responses", [
    [submission()],  # 没有读过证据不能凭 ID 猜。
    [submission(refs=["invented"])],
    [call("submit_verification", {"assessments": [{"criterion_id": "other", "status": "passed", "evidence_refs": [], "reason": "遗漏原标准"}], "recommendation": "通过"})],
])
def test_invalid_submission_never_passes(tmp_path, responses):
    _, verifier, result, handoff, context = setup(tmp_path, [*responses, ModelResponse(assistant_message={"role": "assistant"})], max_steps=1)
    report = verifier.verify(handoff=handoff, result=result, specialist_results={}, execution_context=context)
    assert not report.passed


def test_failed_command_cannot_be_positive_evidence(tmp_path):
    _, verifier, result, handoff, context = setup(tmp_path, [call("read_evidence", {"evidence_id": "execution:0"}), submission()])
    result.tool_results[0]["output"] = {"exit_code": 1, "timed_out": False}
    report = verifier.verify(handoff=handoff, result=result, specialist_results={}, execution_context=context)
    assert not report.passed


def test_write_exposure_denied_without_touching_workspace(tmp_path):
    _, verifier, result, handoff, context = setup(tmp_path, [call("write_file", {"path": "evil", "content": "bad"}), ModelResponse(assistant_message={"role": "assistant"})], max_steps=1)
    report = verifier.verify(handoff=handoff, result=result, specialist_results={}, execution_context=context)
    assert not report.passed and not (tmp_path / "evil").exists()


def test_environment_tools_real_interpreter_missing_package_and_no_tests(tmp_path):
    _, verifier, _, _, _ = setup(tmp_path, [])
    dispatcher = Dispatcher(verifier.registry)
    def run(name, **args):
        return dispatcher.dispatch({"tool_call_id": name, "name": name, "arguments": args})
    found = run("inspect_python_environment", package="pydantic")
    assert found.ok and found.output["installed"] and found.output["version"]
    missing = run("inspect_python_environment", package="tiki-nonexistent-test-package-782919")
    assert missing.ok and missing.output["installed"] is False
    assert run("probe_python_import", module="json").output["verified"]
    assert not run("probe_python_import", module="json; print('injected')").ok
    assert run("run_verification_tests").output["verified"] is False
    (tmp_path / "test_sample.py").write_text("import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertEqual(2+3,5)\n", encoding="utf-8")
    assert run("run_verification_tests").output["verified"] is True
    assert sorted(p.name for p in tmp_path.iterdir()) == ["test_sample.py"]


def test_token_stats_remain_numeric_secrets_redacted():
    from tikiagent.application.events import _sanitize
    data = _sanitize({"prompt_tokens": 123, "api_key": "secret", "access_token": "secret", "total_tokens": "not-a-stat"}, 1000)
    assert data == {"prompt_tokens": 123, "api_key": "[REDACTED]", "access_token": "[REDACTED]", "total_tokens": "[REDACTED]"}


def test_empty_response_retried_once_with_diagnostics():
    from tikiagent.providers.llm.config import ModelSettings
    from tikiagent.providers.llm.openai_compatible import OpenAICompatibleClient
    class Empty:
        content = None
        tool_calls = []
        def model_dump(self, **kwargs): return {"role": "assistant"}
    requests = []
    def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=Empty(), finish_reason="stop")])
    client = OpenAICompatibleClient(ModelSettings("test", "http://invalid.local", "test"), SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    response = client.complete([], [])
    assert len(requests) == 2 and requests[0] == requests[1]
    assert response.diagnostics["empty_response_retries"] == 1 and response.diagnostics["finish_reason"] == "stop"


def test_formal_environment_workflow_finishes_without_install_or_report(tmp_path):
    from tikiagent.application.bootstrap import ApplicationRuntimeFactory
    from tikiagent.application.events import EventBus, CollectingEventSink

    class EnvironmentModel:
        def __init__(self): self.supervisor_steps = 0

        def complete(self, *, messages, tool_schemas):
            names = {s["name"] for s in tool_schemas}
            if "update_plan" in names:
                self.supervisor_steps += 1
                if self.supervisor_steps == 1:
                    return call("update_plan", {"goal": "检查 pydantic 已安装", "acceptance_criteria": ["发行包存在"],
                        "todos": [{"todo_id": "env", "owner": "code_agent", "description": "确认 pydantic 安装", "delivery_mode": "environment",
                                   "required_capabilities": ["python_environment"], "acceptance_criteria": [{"criterion_id": "installed", "description": "pydantic 已安装"}]}]})
                if self.supervisor_steps == 2:
                    return call("delegate_task", {"todo_id": "env", "instruction": "查询 pydantic 安装版本，不要重复安装", "reason": "环境检查"})
                if self.supervisor_steps == 3:
                    from test_planning_supervisor import review
                    return review("env")(messages)
                return call("finish_task", {"reason": "当前结果已验证"})
            if "submit_verification" in names:
                evidence = next((json.loads(m["content"]) for m in messages if m.get("tool_call_id") == "inspect_python_environment"), None)
                if evidence is None:
                    return call("inspect_python_environment", {"package": "pydantic"})
                return call("submit_verification", {"assessments": [{"criterion_id": "installed", "status": "passed",
                    "evidence_refs": [evidence["evidence_id"]], "reason": "独立读取了当前解释器的发行包版本"}], "recommendation": "完成"})
            assert names == {"inspect_python_environment", "probe_python_import", "run_command"}
            if not any(m.get("role") == "tool" for m in messages):
                return call("inspect_python_environment", {"package": "pydantic"})
            return ModelResponse(assistant_message={"role": "assistant", "content": "pydantic 已安装，无需重复安装"}, final_text="pydantic 已安装，无需重复安装")

    sink = CollectingEventSink()
    bus = EventBus()
    bus.subscribe(sink)
    factory = ApplicationRuntimeFactory(tmp_path / "data", env_file=tmp_path / "missing.env", event_bus=bus)
    factory.model = EnvironmentModel()
    workflow = factory._build_workflow("session", "workspace")
    state = workflow.invoke("请确认 pydantic 已安装", session_id="session")
    assert state["status"] == "completed" and state["delegation_count"] == 1
    assert state["specialist_verifications"]["code_agent"].passed
    assert not list((tmp_path / "data/workspaces/session").iterdir())
    checkpoints = [factory.checkpoints.load(p.stem) for p in (tmp_path / "data/checkpoints").glob("*.json")]
    assert checkpoints and all(c.execution_state == "completed" and c.approval_state == "not_required" for c in checkpoints)
    assert not any(e.event_type == "approval_required" for e in sink.events)
    # 简单环境查询仅基础检查，不为独立审核再次执行同样的工具。
    assert state["specialist_verifications"]["code_agent"].verification_status == "checks_only"
    assert not any(e.event_type == "tool_execution_started" and e.data.get("agent") == "verifier" for e in sink.events)
    assert "inspect_python_environment" in (tmp_path / "data/traces/session.jsonl").read_text(encoding="utf-8")


def test_missing_dependency_local_wheel_requires_approval_then_real_install(tmp_path, monkeypatch):
    """实际安装只发生在临时 venv，不联网，不污染项目解释器。"""
    import os
    import venv
    import zipfile
    from tikiagent.harness.execution import ExecutionHarness
    from tikiagent.harness.permissions.models import ApprovalDecision
    from tikiagent.harness.persistence.checkpoint import PendingModelToolCall
    from tikiagent.tools import commands, python_environment
    from test_harness_recovery import fixtures

    venv.EnvBuilder(with_pip=True).create(tmp_path / "venv")
    python = tmp_path / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    monkeypatch.setattr(python_environment, "sys", SimpleNamespace(executable=str(python)))
    monkeypatch.setattr(commands, "sys", SimpleNamespace(executable=str(python)))
    workspace = Workspace(tmp_path / "workspace")
    wheel_name = "tiki_probe_fixture-1.0-py3-none-any.whl"
    package_files = {"tiki_probe_fixture/__init__.py": "value = 42\n",
        "tiki_probe_fixture-1.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: tiki-probe-fixture\nVersion: 1.0\n",
        "tiki_probe_fixture-1.0.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: tiki-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
    package_files["tiki_probe_fixture-1.0.dist-info/RECORD"] = "".join(f"{p},,\n" for p in [*package_files, "tiki_probe_fixture-1.0.dist-info/RECORD"])
    with zipfile.ZipFile(workspace.root / wheel_name, "w") as archive:
        for name, content in package_files.items(): archive.writestr(name, content)
    registry = build_read_only_file_registry(workspace)
    register_python_environment_tools(registry, workspace)
    commands.register_command_tool(registry, workspace)
    dispatcher = Dispatcher(registry)
    inspect = lambda: dispatcher.dispatch({"tool_call_id": "inspect", "name": "inspect_python_environment", "arguments": {"package": "tiki-probe-fixture"}})
    assert inspect().output["installed"] is False
    _, coordinator, context, workflow, react = fixtures(tmp_path / "runtime")
    coordinator.harness = ExecutionHarness(dispatcher)
    context = context.model_copy(update={"exposed_tools": {"run_command"}})
    args = {"command": ["python", "-m", "pip", "install", "--no-index", "--no-deps", wheel_name]}
    react = react.model_copy(update={"pending_tool_calls": [PendingModelToolCall(tool_call_id="call-1", name="run_command", arguments_json=json.dumps(args))]})
    paused = coordinator.execute({"tool_call_id": "call-1", "name": "run_command", "arguments": args},
        context=context, workflow_snapshot=workflow, react_snapshot=react)
    assert paused.outcome.status == "awaiting_approval" and inspect().output["installed"] is False
    request = paused.outcome.approval_request
    resumed = coordinator.resume_approval(paused.checkpoint.checkpoint_id, expected_revision=paused.checkpoint.revision,
        decision=ApprovalDecision(request_id=request.request_id, approved=True, scope=request.scope, fingerprint=request.fingerprint))
    assert resumed.outcome.tool_result.output["exit_code"] == 0
    assert resumed.checkpoint.execution_state == "completed" and resumed.checkpoint.revision == 3
    assert inspect().output["version"] == "1.0"
    imported = dispatcher.dispatch({"tool_call_id": "import", "name": "probe_python_import", "arguments": {"module": "tiki_probe_fixture"}})
    assert imported.output["verified"] is True
