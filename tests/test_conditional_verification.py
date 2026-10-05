"""条件审核链：真实 Graph/Workspace/Checkpoint，完全离线的模型夹具。"""

import json

import pytest

from test_planning_supervisor import Gate, Script, call, delegate, plan, review, stop, workflow
from test_verifier_agent import setup, submission
from tikiagent.context.projections import verification_view
from tikiagent.harness.workspace import Workspace
from tikiagent.orchestration.contracts import CodeResult, Handoff, ResearchResult, VerificationReport
from tikiagent.orchestration.state import restore_tiki_state, serialize_tiki_state
from tikiagent.verification.basic import BasicResultVerifier
from tikiagent.verification.gate import VerificationGate


class AuditSpy:
    supports_context = True

    def __init__(self):
        self.calls = []

    def verify(self, *, handoff, result, **kwargs):
        self.calls.append(kwargs)
        return VerificationReport(result_id=result.result_id, handoff_id=handoff.handoff_id,
            subject_agent=handoff.to_agent, todo_id=handoff.todo_id, mode="agent", passed=True,
            checks=[], failures=[], evidence=[], recommendation="根据证据判断",
            assessments=[{"criterion_id": c.criterion_id, "status": "passed", "evidence_refs": ["audit:0"],
                          "reason": "离线独立证据"} for c in handoff.acceptance_criteria],
            evidence_records={"audit:0": {"usable": True}})


def gate(tmp_path, audit=None):
    spy = audit or AuditSpy()
    return VerificationGate(research_verifier=spy, code_verifier=spy, basic_verifier=BasicResultVerifier(Workspace(tmp_path))), spy


def linked(agent="code_agent", *, level="basic", mode="artifact", **kwargs):
    handoff = Handoff(handoff_id="h", result_id="r", todo_id="t", from_agent="supervisor",
        to_agent=agent, status="completed", instruction="交付有来源的结果或文件", delivery_mode=mode,
        verification_level=level, verification_reason="当前任务所需检查",
        acceptance_criteria=[{"criterion_id": "done", "description": "满足用户目标"}])
    if agent == "research_agent":
        result = ResearchResult(result_id="r", handoff_id="h", summary="结论", findings=["已查询"],
            sources=[{"observation_id": "o", "url": "https://example.org/news", "title": "原文", "snippet": "证据"}],
            observations=[{"observation_id": "o", "query": "新闻", "urls": ["https://example.org/news"]}],
            queries=["新闻"], delivery_status="ready", **kwargs)
    else:
        result = CodeResult(result_id="r", handoff_id="h", completed=True, steps=1, summary="文件已完成",
                            changed_files=["a.txt"], delivery_status="ready", **kwargs)
    return handoff, result


@pytest.mark.parametrize("agent", ["research_agent", "code_agent"])
def test_basic_path_does_not_call_audit_or_require_prompt(tmp_path, agent):
    (tmp_path / "a.txt").write_text("交付", encoding="utf-8")
    verifier, spy = gate(tmp_path)
    handoff, result = linked(agent)
    assert not verifier.needs_context(handoff)
    report = verifier.verify(handoff=handoff, raw_result=result.model_dump(), specialist_results={})
    assert report.passed and report.verification_status == "checks_only" and not spy.calls
    assert report.todo_id == "t" and not report.assessments
    assert "未进行独立" in report.recommendation
    view = verification_view(report.model_dump())
    assert view["checks"] and view["verification_status"] == "checks_only"


def test_independent_path_keeps_basic_checks_and_uses_frozen_requirement(tmp_path):
    (tmp_path / "a.txt").write_text("文件", encoding="utf-8")
    verifier, spy = gate(tmp_path)
    handoff, result = linked(level="independent")
    assert verifier.needs_context(handoff)
    report = verifier.verify(handoff=handoff, raw_result=result.model_dump(), specialist_results={}, base_context="独立上下文")
    assert report.passed and report.verification_status == "assessed" and len(spy.calls) == 1
    assert spy.calls[0]["base_context"] == "独立上下文"
    assert any(c.name == "artifact_exists" for c in report.checks)


@pytest.mark.parametrize("defect", ["result_id", "handoff_id", "missing_file", "workspace_escape", "no_contract", "none", "source"])
@pytest.mark.parametrize("level", ["basic", "independent"])
def test_mechanical_boundaries_cannot_be_bypassed_by_removing_llm(tmp_path, defect, level):
    (tmp_path / "a.txt").write_text("文件", encoding="utf-8")
    verifier, spy = gate(tmp_path)
    handoff, result = linked("research_agent" if defect == "source" else "code_agent", level=level)
    if defect in {"result_id", "handoff_id"}:
        result = result.model_copy(update={defect: "wrong"})
    elif defect == "missing_file":
        result = result.model_copy(update={"changed_files": ["missing.txt"]})
    elif defect == "workspace_escape":
        result = result.model_copy(update={"changed_files": ["../outside.txt"]})
    elif defect == "no_contract":
        handoff = handoff.model_copy(update={"acceptance_criteria": []})
    elif defect == "none":
        result = result.model_copy(update={"delivery_status": "none"})
    else:
        result.sources[0].url = "https://invented.invalid"
    report = verifier.verify(handoff=handoff, raw_result=result.model_dump(), specialist_results={})
    assert not report.passed and report.hard_blockers and not spy.calls


def test_unresolved_command_error_is_limitation_not_success_and_repaired_command_is_not_stale(tmp_path):
    (tmp_path / "a.txt").write_text("文件", encoding="utf-8")
    verifier, _ = gate(tmp_path)
    command = {"tool_name": "run_command", "ok": True, "output": {"command": ["python", "-m", "unittest"], "cwd": ".", "exit_code": 1}}
    handoff, result = linked(tool_results=(command,))
    report = verifier.verify(handoff=handoff, raw_result=result.model_dump(), specialist_results={})
    assert report.passed and any("非零退出" in s for s in report.limitations)
    success = {**command, "output": {**command["output"], "exit_code": 0}}
    repaired = result.model_copy(update={"tool_results": (command, success)})
    report = verifier.verify(handoff=handoff, raw_result=repaired.model_dump(), specialist_results={})
    assert not report.limitations


def test_basic_path_does_not_refresh_research_budget_on_synthesis_error(tmp_path):
    verifier, spy = gate(tmp_path)
    handoff, result = linked("research_agent", finalization_status="failed",
                            finalization_diagnostics={"error_category": "invalid_structured_output"})
    report = verifier.verify(handoff=handoff, raw_result=result.model_dump(), specialist_results={})
    assert report.verification_status == "not_performed" and report.failure_category == "model_response"
    assert report.allowed_actions == ["stop"] and not report.retryable and not spy.calls
    assert report.checks


def test_new_plan_basic_old_snapshot_independent_and_requirement_freezes(tmp_path):
    new = plan("a")
    audit_plan = json.loads(new.tool_calls[0].arguments_json)
    audit_plan["todos"][0].update(verification_level="independent", verification_reason="用户要求独立审查")
    downgraded = json.loads(new.tool_calls[0].arguments_json)
    downgraded["todos"][0].update(verification_level="basic", verification_reason="试图规避审核")
    model, _, graph = workflow([call("update_plan", audit_plan), delegate("a"), call("update_plan", downgraded), stop()])
    state = graph.invoke("审查文件")
    assert "immutable_verification" in str(model.requests[-1])
    payload = json.loads(json.dumps(serialize_tiki_state(state)))
    restored = restore_tiki_state(payload)
    assert restored["task_board"].items["a"].verification_level == restored["latest_handoff"].verification_level == "independent"
    assert restored["latest_handoff"].verification_reason == "用户要求独立审查"
    payload["task_board"]["items"]["a"].pop("verification_level")
    payload["task_board"]["items"]["a"].pop("verification_reason")
    payload["latest_handoff"].pop("verification_level")
    payload["latest_handoff"].pop("verification_reason")
    old = restore_tiki_state(payload)
    assert old["latest_handoff"].verification_level == old["task_board"].items["a"].verification_level == "independent"
    _, _, ordinary = workflow([new, delegate("a"), review("a"), call("finish_task", {"reason": "完成"})])
    assert ordinary.invoke("简单文件")["task_board"].items["a"].verification_level == "basic"


def test_composite_code_plan_escalates_but_simple_inspection_does_not():
    _, _, graph = workflow([plan("a", "b"), stop()])
    state = graph.invoke("两阶段代码交付")
    assert state["task_board"].items["a"].verification_level == "basic"
    assert state["task_board"].items["b"].verification_level == "independent"
    args = json.loads(plan("a").tool_calls[0].arguments_json)
    args["todos"][0]["required_capabilities"] = ["workspace_write", "command_execution"]
    _, _, graph = workflow([call("update_plan", args), stop()])
    assert graph.invoke("代码及测试")["task_board"].items["a"].verification_level == "independent"


def test_basic_graph_still_requires_supervisor_review_and_does_not_build_verifier_context(tmp_path):
    (tmp_path / "a.txt").write_text("真实交付", encoding="utf-8")
    verifier, spy = gate(tmp_path)
    finish = call("finish_task", {"reason": "已验收"})
    model, _, graph = workflow([plan("a"), delegate("a"), finish, review("a"), finish], gate=verifier)
    builder = graph._build_context
    agents = []
    def capture(**kwargs):
        agents.append(kwargs["agent"])
        return builder(**kwargs)
    graph._build_context = capture
    state = graph.invoke("简单文件")
    assert state["status"] == "completed" and "finish_not_verified" in str(model.requests)
    assert "verifier" not in agents and not spy.calls
    assert state["task_board"].items["a"].review.action == "accept"
    assert "未进行独立 LLM 审核" in state["final_result"]
    assert graph._finish_guard_failures(state) == ([], [])


def test_forged_checks_only_cannot_satisfy_frozen_independent_requirement():
    class FakeGate(Gate):
        def verify(self, **kwargs):
            return super().verify(**kwargs).model_copy(update={"verification_status": "checks_only"})
    args = json.loads(plan("a").tool_calls[0].arguments_json)
    args["todos"][0].update(verification_level="independent", verification_reason="用户要求审计")
    model, _, graph = workflow([call("update_plan", args), delegate("a"), review("a"), stop()], gate=FakeGate())
    state = graph.invoke("独立审查")
    assert not state["task_board"].items["a"].review
    assert "静默降级" in str(model.requests[-1])


def test_missing_refs_can_be_repaired_in_same_bounded_verifier_run(tmp_path):
    model, verifier, result, handoff, context = setup(tmp_path, [
        call("read_evidence", {"evidence_id": "execution:0"}), submission(refs=[]), submission()], max_steps=3)
    report = verifier.verify(handoff=handoff, result=result, specialist_results={}, execution_context=context)
    assert report.passed and len(model.requests) == 3
    assert "missing_evidence_refs" in str(model.requests[-1])


def test_unusable_refs_are_rejected_locally_then_reported_as_insufficient(tmp_path):
    model, verifier, result, handoff, context = setup(tmp_path, [
        call("read_evidence", {"evidence_id": "execution:0"}), submission(), submission("insufficient_evidence")], max_steps=3)
    result.tool_results[0]["output"] = {"exit_code": 1}
    report = verifier.verify(handoff=handoff, result=result, specialist_results={}, execution_context=context)
    assert not report.passed and report.verification_status == "assessed" and len(model.requests) == 3
    assert "unusable_evidence" in str(model.requests[-1])


def test_basic_partial_delivery_requires_explicit_limitations_not_automatic_accept(tmp_path):
    (tmp_path / "a.txt").write_text("已有可用交付", encoding="utf-8")
    verifier, spy = gate(tmp_path)
    class PartialCode:
        max_steps = 4
        def run(self, *, handoff, base_context):
            return CodeResult(handoff_id=handoff.handoff_id, summary="已写文件但测试失败", completed=False,
                steps=1, changed_files=["a.txt"], delivery_status="partial", stop_reason="max_steps",
                tool_results=({"tool_name": "run_command", "ok": True,
                    "output": {"command": ["python", "-m", "unittest"], "exit_code": 1}},))
    model, _, graph = workflow([plan("a"), delegate("a"), review("a"),
        review("a", "accept_with_limitations", limitations=["保留可用文件，测试未通过"]),
        call("finish_task", {"reason": "带限制完成"})], gate=verifier)
    graph.code_agent = PartialCode()
    state = graph.invoke("交付部分成果")
    assert state["status"] == "completed" and "review_limitations_required" in str(model.requests)
    assert "非零退出" in state["final_result"] and "测试未通过" in state["final_result"]
    assert not spy.calls and state["task_board"].items["a"].review.action == "accept_with_limitations"


def test_pending_plan_can_escalate_after_adding_composite_capabilities():
    args = json.loads(plan("a").tool_calls[0].arguments_json)
    args["todos"][0]["required_capabilities"] = ["workspace_write", "command_execution"]
    _, _, graph = workflow([plan("a"), call("update_plan", args), stop()])
    state = graph.invoke("委派前调整计划")
    assert state["task_board"].items["a"].verification_level == "independent"


def test_invalid_audit_is_not_a_result_identity_error_or_research_retry(tmp_path):
    class BrokenAudit(AuditSpy):
        def verify(self, **kwargs):
            return super().verify(**kwargs).model_copy(update={"assessments": []})
    (tmp_path / "a.txt").write_text("交付", encoding="utf-8")
    verifier, _ = gate(tmp_path, BrokenAudit())
    handoff, result = linked(level="independent")
    report = verifier.verify(handoff=handoff, raw_result=result.model_dump(), specialist_results={})
    assert report.verification_status == "not_performed" and report.failure_category == "model_response"
    assert report.allowed_actions == ["stop"] and not report.hard_blockers and not report.retryable
    assert any(c.name == "artifact_exists" for c in report.checks)


def test_audit_service_failure_returns_incomplete_report_with_checks(tmp_path):
    class OfflineAudit(AuditSpy):
        def verify(self, **kwargs):
            raise ConnectionError("不可连接的模拟服务")
    (tmp_path / "a.txt").write_text("交付", encoding="utf-8")
    verifier, _ = gate(tmp_path, OfflineAudit())
    handoff, result = linked(level="independent")
    report = verifier.verify(handoff=handoff, raw_result=result.model_dump(), specialist_results={})
    assert report.verification_status == "not_performed" and report.checks
    assert report.failure_category == "model_response" and report.allowed_actions == ["stop"]


def test_audit_error_cannot_be_reassigned_to_specialist(tmp_path):
    class BrokenAudit(AuditSpy):
        def verify(self, **kwargs):
            return super().verify(**kwargs).model_copy(update={"assessments": []})
    (tmp_path / "a.txt").write_text("交付", encoding="utf-8")
    verifier, _ = gate(tmp_path, BrokenAudit())
    args = json.loads(plan("a").tool_calls[0].arguments_json)
    args["todos"][0].update(verification_level="independent", verification_reason="用户要求审查")
    retry = call("delegate_task", {"todo_id": "a", "instruction": "重新提供文件证据", "reason": "补做",
        "missing_evidence": "审核未完成", "strategy_change": "重新阅读", "expected_evidence": "原文"})
    model, code, graph = workflow([call("update_plan", args), delegate("a"), review("a", "request_changes"), retry, stop()], gate=verifier)
    state = graph.invoke("审核服务错误不能变成文件重复执行")
    assert state["status"] == "stopped" and len(code.calls) == 1
    assert "execution_blocked" in str(model.requests[-1])


@pytest.mark.parametrize("field", ["result_id", "handoff_id", "todo_id", "subject_agent"])
def test_independent_report_identity_mismatch_stays_hard_blocked(tmp_path, field):
    class WrongIdentity(AuditSpy):
        def verify(self, **kwargs):
            return super().verify(**kwargs).model_copy(update={field: "wrong"})
    (tmp_path / "a.txt").write_text("交付", encoding="utf-8")
    verifier, _ = gate(tmp_path, WrongIdentity())
    handoff, result = linked(level="independent")
    report = verifier.verify(handoff=handoff, raw_result=result.model_dump(), specialist_results={})
    assert report.failure_category == "identity" and report.hard_blockers


def test_approval_checkpoint_keeps_basic_requirement_across_new_runtime(tmp_path):
    from tikiagent.agents.planning import PlanningSupervisorAgent
    from tikiagent.context.memory.history import JsonlHistoryStore
    from tikiagent.harness.permissions.models import ApprovalDecision
    from test_multi_agent_resume import build_workflow, ScriptedModel, first_response, final_response

    store = JsonlHistoryStore(tmp_path / "history.jsonl")
    workspace, first = build_workflow(tmp_path, ScriptedModel([first_response()]), history_store=store)
    workspace.resolve("input.txt").write_text("原文", encoding="utf-8")
    first.supervisor = PlanningSupervisorAgent(Script([plan("a"), delegate("a")]))
    first.verification_gate, spy = gate(workspace.root)
    paused = first.invoke("读取后写文件", session_id="s", task_id="t")
    assert paused["status"] == "awaiting_approval"
    checkpoint = first.code_agent.agent.execution_coordinator.checkpoint_store.load(paused["runtime_checkpoint_id"])
    snapshot = checkpoint.workflow_snapshot.state
    assert snapshot["task_board"]["items"]["a"]["verification_level"] == "basic"
    assert snapshot["latest_handoff"]["verification_level"] == "basic"
    _, second = build_workflow(tmp_path, ScriptedModel([final_response()]))
    second.supervisor = PlanningSupervisorAgent(Script([review("a"), call("finish_task", {"reason": "交付已验收"})]))
    second.verification_gate, second_spy = gate(workspace.root)
    approval = checkpoint.approval_request
    state = second.resume(checkpoint.checkpoint_id, expected_revision=checkpoint.revision,
        approval_decision=ApprovalDecision(request_id=approval.request_id, approved=True,
            scope=approval.scope, fingerprint=approval.fingerprint))
    assert state["status"] == "completed" and workspace.resolve("output.txt").read_text() == "graph resumed"
    assert state["latest_handoff"].verification_level == state["task_board"].items["a"].verification_level == "basic"
    assert state["verification_report"].verification_status == "checks_only"
    assert not spy.calls and not second_spy.calls


@pytest.mark.parametrize("status", ["checks_only", "not_performed"])
def test_tui_does_not_present_basic_or_incomplete_as_llm_pass(status):
    from test_execution_visibility import event
    from tikiagent.application.events import EventBus
    from tikiagent.interfaces.tui.adapter import TuiEventAdapter
    from tikiagent.interfaces.tui.models import TuiViewState
    state = TuiEventAdapter().reduce(TuiViewState(), event(EventBus(), "verification_completed",
        passed=status == "checks_only", verification_status=status, advisory=True))
    assert "PASS" not in state.feed[-1].summary and "符合条件" not in state.feed[-1].summary
    assert "基础检查" in state.feed[-1].title if status == "checks_only" else "审核未完成" in state.feed[-1].title
