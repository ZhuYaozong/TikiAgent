"""审核意见与 Supervisor 验收决定分离的真实 Graph 回归。"""

import json
from copy import deepcopy

import pytest

from test_planning_supervisor import Gate, Script, call, delegate, plan, review, stop, workflow
from tikiagent.agents.planning import PlanningSupervisorAgent
from tikiagent.context.memory.history import JsonlHistoryStore
from tikiagent.context.memory.models import HistoryRecord
from tikiagent.orchestration.state import restore_tiki_state, serialize_tiki_state


def finish():
    return call("finish_task", {"reason": "已验收全部交付并披露限制"})


def test_pass_is_advisory_and_cannot_finish_without_review():
    model, _, graph = workflow([plan("a"), delegate("a"), finish(), stop()])
    state = graph.invoke("交付文件")
    assert state["task_board"].items["a"].status == "awaiting_review"
    assert state["verification_report"].advisory and state["verification_report"].passed
    assert state["status"] == "stopped"
    assert "finish_not_verified" in str(model.requests[-1])


def test_missing_quality_can_be_accepted_with_limitations_then_dependency_runs():
    model, code, graph = workflow([plan("a", "b"), delegate("a"),
        review("a", "accept_with_limitations", limitations=["已有交付可用，但附带事实未核实"]),
        delegate("b"), review("b", "accept_with_limitations", limitations=["只保证已有证据覆盖部分需求"]), finish()], gate=Gate(False))
    state = graph.invoke("两阶段任务")
    assert state["status"] == "completed" and len(code.calls) == 2
    first = state["task_board"].items["a"]
    assert first.review.action == "accept_with_limitations"
    assert first.review.review_id in code.calls[-1].context_refs
    assert "未确认验收项 done" in code.contexts[-1].render()
    assert "任务交付（含限制）" in state["final_result"]
    assert "未确认验收项 done" in state["final_result"]
    assert not state["verification_report"].passed  # 不改写原报告来取得成功。
    assert all(t.acceptance_criteria[0].required for t in state["task_board"].items.values())
    records = graph.history_for(state)
    assert len([r for r in records if r.record_type == "review"]) == 2


def test_normal_accept_cannot_hide_quality_gap():
    model, _, graph = workflow([plan("a"), delegate("a"), review("a"), stop()], gate=Gate(False))
    state = graph.invoke("内容存在缺口")
    assert state["task_board"].items["a"].review is None
    assert "review_limitations_required" in str(model.requests[-1])


def test_request_changes_is_required_before_redelegating():
    retry = call("delegate_task", {"todo_id": "a", "instruction": "按失败条件修复文件", "reason": "补做",
        "missing_evidence": "缺少验收证据", "strategy_change": "读取交付原文", "expected_evidence": "原文"})
    model, code, graph = workflow([plan("a"), delegate("a"), retry,
        review("a", "request_changes"), retry, review("a", "stop")], gate=Gate(False))
    state = graph.invoke("补做文件")
    assert "todo_not_actionable" in str(model.requests)
    assert len(code.calls) == 2 and state["status"] == "stopped"
    assert state["task_board"].items["a"].review.action == "stop"
    reviews = [r.payload for r in graph.history_for(state) if r.record_type == "review"]
    assert reviews[0]["result_id"] != reviews[1]["result_id"]


@pytest.mark.parametrize("field", ["result_id", "handoff_id", "verification_id"])
def test_stale_review_identity_is_rejected(field):
    model, _, graph = workflow([plan("a"), delegate("a"), review("a", overrides={field: "stale"}), stop()])
    state = graph.invoke("禁止旧审核覆盖新结果")
    assert state["task_board"].items["a"].review is None
    assert "review_identity_mismatch" in str(model.requests[-1])


@pytest.mark.parametrize("defect", ["hard_blocker", "false_evidence", "missing_criterion", "permission"])
def test_accept_with_limitations_does_not_bypass_mechanical_guards(defect):
    class BrokenGate(Gate):
        def verify(self, **kwargs):
            report = super().verify(**kwargs)
            updates = {
                "hard_blocker": {"hard_blockers": ["来源不可追溯"]},
                "false_evidence": {"evidence_records": {"fixture": {"usable": False}}},
                "missing_criterion": {"assessments": []},
                "permission": {"failure_category": "permission", "verification_status": "not_performed"},
            }[defect]
            return report.model_copy(update=updates)
    model, _, graph = workflow([plan("a"), delegate("a"),
        review("a", "accept_with_limitations", limitations=["接受部分交付"]), stop()], gate=BrokenGate())
    state = graph.invoke("验收不能越过机械边界")
    assert state["task_board"].items["a"].review is None
    assert "review_hard_blocked" in str(model.requests[-1])


def test_unfinished_audit_may_be_explicitly_accepted_with_limits_not_claimed_verified():
    class UnfinishedGate(Gate):
        def verify(self, **kwargs):
            return super().verify(**kwargs).model_copy(update={"passed": False,
                "verification_status": "not_performed", "failure_category": "budget",
                "blocking_reason": "验证预算耗尽", "assessments": []})
    _, _, graph = workflow([plan("a"), delegate("a"),
        review("a", "accept_with_limitations", limitations=["仅交付现有文件，审核尚未完成"]), finish()], gate=UnfinishedGate())
    state = graph.invoke("提交已有文件")
    assert state["status"] == "completed"
    assert "审核未完成：验证预算耗尽" in state["final_result"]


def test_missing_artifact_cannot_be_accepted_even_if_report_passes():
    model, _, graph = workflow([plan("a"), delegate("a"), review("a"), stop()])
    graph.artifact_guard = lambda todo, result: ["交付文件不存在"]
    state = graph.invoke("创建文件")
    assert state["task_board"].items["a"].review is None
    assert "交付文件不存在" in str(model.requests[-1])


def test_review_survives_snapshot_and_cross_process_history_reload(tmp_path):
    store = JsonlHistoryStore(tmp_path / "history.jsonl")
    _, _, graph = workflow([plan("a", "b"), delegate("a"), review("a"), stop()], history=store)
    state = graph.invoke("先完成 a", session_id="s", task_id="t")
    payload = serialize_tiki_state(state)
    restored = restore_tiki_state(json.loads(json.dumps(payload)))
    persisted = restored["task_board"].items["a"].review
    assert persisted == state["task_board"].items["a"].review
    reloaded = JsonlHistoryStore(tmp_path / "history.jsonl")
    assert reloaded.get_by_id(persisted.review_id).payload == persisted.model_dump(mode="json")
    graph.history_store = reloaded
    graph._bind_context_services()
    graph.supervisor = PlanningSupervisorAgent(Script([delegate("b"), review("b"), finish()]))
    # 快照重新进入 Graph；不是从 Trace 猜状态或手工拼接执行结果。
    result = graph.graph.invoke(restored)
    assert result["status"] == "completed"
    assert result["task_board"].items["a"].review == persisted


def test_old_history_json_remains_readable_and_no_review_is_invented():
    record = HistoryRecord.model_validate({"record_id": "old", "task_id": "t", "session_id": "s",
        "record_type": "verification", "producer": "verifier", "summary": "旧报告"})
    assert record.record_type == "verification"
    _, _, graph = workflow([plan("a"), delegate("a"), review("a"), finish()])
    state = graph.invoke("交付")
    payload = serialize_tiki_state(state)
    payload["task_board"]["items"]["a"].pop("review")
    restored = restore_tiki_state(payload)
    assert restored["task_board"].items["a"].status == "awaiting_review"
    assert graph._finish_guard_failures(restored)[1] == ["a"]


def test_finish_rejects_tampered_acceptance_decision():
    _, _, graph = workflow([plan("a"), delegate("a"), review("a"), finish()])
    state = graph.invoke("交付")
    invalid = deepcopy(state)
    todo = invalid["task_board"].items["a"]
    todo.review = todo.review.model_copy(update={"result_id": "old-result"})
    assert graph._finish_guard_failures(invalid)[1] == ["a"]


def test_review_cannot_be_consumed_twice():
    model, _, graph = workflow([plan("a"), delegate("a"), review("a"), review("a"), finish()])
    state = graph.invoke("交付")
    assert state["status"] == "completed"
    assert "review_not_pending" in str(model.requests)
    assert len([r for r in graph.history_for(state) if r.record_type == "review"]) == 1


def test_accepted_todo_survives_real_approval_checkpoint_and_new_runtime(tmp_path):
    from test_multi_agent_resume import build_workflow, ScriptedModel, first_response, final_response
    from tikiagent.harness.permissions.models import ApprovalDecision
    from tikiagent.providers.llm.models import ModelResponse

    read_only = ModelResponse(assistant_message={}, tool_calls=first_response().tool_calls[:1])
    workspace, first = build_workflow(tmp_path, ScriptedModel([read_only, final_response(), first_response()]),
        history_store=JsonlHistoryStore(tmp_path / "history.jsonl"))
    first.verification_gate = Gate()
    first.supervisor = PlanningSupervisorAgent(Script([plan("a", "b"), delegate("a"), review("a"), delegate("b")]))
    workspace.resolve("input.txt").write_text("input", encoding="utf-8")
    paused = first.invoke("读取后写文件", session_id="s", task_id="t")
    assert paused["status"] == "awaiting_approval"
    checkpoint = first.code_agent.agent.execution_coordinator.checkpoint_store.load(paused["runtime_checkpoint_id"])
    accepted = checkpoint.workflow_snapshot.state["task_board"]["items"]["a"]["review"]
    assert accepted["action"] == "accept"
    _, second = build_workflow(tmp_path, ScriptedModel([final_response()]))
    second.verification_gate = Gate()
    second.supervisor = PlanningSupervisorAgent(Script([review("b"), finish()]))
    approval = checkpoint.approval_request
    result = second.resume(checkpoint.checkpoint_id, expected_revision=checkpoint.revision,
        approval_decision=ApprovalDecision(request_id=approval.request_id, approved=True,
            scope=approval.scope, fingerprint=approval.fingerprint))
    assert result["status"] == "completed"
    assert result["task_board"].items["a"].review.model_dump(mode="json") == accepted
    assert second.history_store.get_by_id(accepted["review_id"]).payload == accepted
    assert workspace.resolve("output.txt").read_text() == "graph resumed"


def test_next_turn_can_recall_accepted_limitations(tmp_path):
    from tikiagent.application.context_refs import SessionContextReferenceProvider
    store = JsonlHistoryStore(tmp_path / "s.jsonl")
    _, _, graph = workflow([plan("a"), delegate("a"),
        review("a", "accept_with_limitations", limitations=["保留未确认事项"]), finish()], gate=Gate(False), history=store)
    state = graph.invoke("交付", session_id="s")
    review_id = state["task_board"].items["a"].review.review_id
    refs = SessionContextReferenceProvider(tmp_path).select("s")
    assert review_id in refs
    assert "未确认验收项" in str(store.get_by_id(review_id).payload)


def test_review_event_and_history_have_same_identity():
    events = []
    _, _, graph = workflow([plan("a"), delegate("a"), review("a"), finish()])
    graph.supervisor.observer = lambda kind, state, ref, data: events.append((kind, ref, data))
    state = graph.invoke("交付")
    event = next(e for e in events if e[0] == "result_reviewed")
    review_record = graph.history_store.get_by_id(event[1])
    assert event[2] == review_record.payload == state["task_board"].items["a"].review.model_dump(mode="json")


def test_long_missing_criterion_is_bounded_without_breaking_finish_guard():
    setup = plan("a")
    args = json.loads(setup.tool_calls[0].arguments_json)
    args["todos"][0]["acceptance_criteria"] = [{"criterion_id": "c" * 100, "description": "条件" * 750}]
    _, _, graph = workflow([call("update_plan", args), delegate("a"),
        review("a", "accept_with_limitations", limitations=["交付可用但未确认长条件"]), finish()], gate=Gate(False))
    state = graph.invoke("长验收条件")
    assert state["status"] == "completed"
    assert all(len(s) <= 500 for s in state["task_board"].items["a"].review.limitations)
