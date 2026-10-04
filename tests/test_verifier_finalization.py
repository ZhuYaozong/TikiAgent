"""预算边界不是验收结论：取证结束后仍必须获得一次有界提交机会。"""

import json

import pytest

from test_verifier_agent import call, setup, submission
from tikiagent.providers.llm.models import ModelResponse, ModelToolCall


def batch(*items):
    return ModelResponse(assistant_message={"role": "assistant"}, tool_calls=tuple(
        ModelToolCall(identity, name, json.dumps(args)) for identity, name, args in items))


def assert_pairs(messages):
    # 校验发送给下一轮模型的每一个完整交互，不能漏掉超预算的拒绝结果。
    pending = []
    for message in messages:
        if message.get("tool_calls"):
            assert not pending
            pending = [c["id"] for c in message["tool_calls"]]
        elif message["role"] == "tool":
            pending.remove(message["tool_call_id"])
    assert not pending


def test_eight_evidence_calls_in_2_3_3_rounds_then_submit(tmp_path):
    (tmp_path / "index.html").write_text("<!DOCTYPE html>\n<html><head></head><body></body></html>", encoding="utf-8")
    events = []

    def finalize(messages, tool_schemas):
        assert {s["name"] for s in tool_schemas} == {"submit_verification"}
        assert_pairs(messages)
        # 第八项确实进入本轮输入，而不是预算耗尽后被丢弃。
        last = json.loads(next(m["content"] for m in messages if m.get("tool_call_id") == "e8"))
        assert last["output"]["matches"][0]["text"].endswith("</html>")
        assert '"remaining_evidence_calls": 0' in str(messages)
        assert 'observed_evidence' in str(messages)
        return submission(refs=[last["evidence_id"]])

    responses = [
        batch(("e1", "read_file", {"path": "index.html"}), ("e2", "list_files", {"path": "."})),
        batch(("e3", "grep", {"path": "index.html", "pattern": "script"}),
              ("e4", "grep", {"path": "index.html", "pattern": "html"}),
              ("e5", "read_evidence", {"evidence_id": "execution:0"})),
        batch(("e6", "read_evidence", {"evidence_id": "execution:0"}),
              ("e7", "read_evidence", {"evidence_id": "execution:0"}),
              ("e8", "grep", {"path": "index.html", "pattern": "body"})),
        finalize,
    ]
    model, verifier, result, handoff, context = setup(tmp_path, responses, observer=lambda *e: events.append(e))
    report = verifier.verify(handoff=handoff, result=result, specialist_results={}, execution_context=context)
    assert report.passed and len(model.requests) == 4
    assert sum(e[0] == "tool_execution_started" for e in events) == 9  # 八次取证加一次提交。


def test_model_round_budget_also_preserves_final_results(tmp_path):
    def finalize(messages, tool_schemas):
        assert_pairs(messages)
        assert '"remaining_evidence_rounds": 0' in str(messages)
        assert '"remaining_evidence_calls": 7' in str(messages)
        assert [s["name"] for s in tool_schemas] == ["submit_verification"]
        return submission()
    model, verifier, result, handoff, context = setup(tmp_path,
        [call("read_evidence", {"evidence_id": "execution:0"}), finalize], max_steps=1)
    assert verifier.verify(handoff=handoff, result=result, specialist_results={}, execution_context=context).passed
    assert len(model.requests) == 2


def test_oversized_batch_is_fully_paired_but_does_not_execute_over_budget(tmp_path):
    events = []
    def finalize(messages, tool_schemas):
        assert_pairs(messages)
        results = {m["tool_call_id"]: json.loads(m["content"]) for m in messages if m["role"] == "tool"}
        assert results["e1"]["ok"]
        for name in ("e2", "e3"):
            assert results[name]["error"]["code"] == "verification_evidence_budget_exceeded"
        return submission()
    model, verifier, result, handoff, context = setup(tmp_path, [batch(
        ("e1", "read_evidence", {"evidence_id": "execution:0"}),
        ("e2", "list_files", {}), ("e3", "list_files", {})), finalize],
        max_tool_calls=1, observer=lambda *e: events.append(e))
    assert verifier.verify(handoff=handoff, result=result, specialist_results={}, execution_context=context).passed
    assert len(model.requests) == 2
    assert [e[2] for e in events if e[0] == "tool_execution_started"] == ["e1", "submit_verification"]


@pytest.mark.parametrize("last", [
    call("read_file", {"path": "missing.txt"}),
    call("write_file", {"path": "forbidden", "content": "bad"}),
    call("run_command", {"command": ["python", "-m", "pytest"]}),
    ModelResponse(assistant_message={"role": "assistant", "content": "通过"}, final_text="通过"),
    submission(refs=["invented"]),
    call("submit_verification", {"assessments": [], "recommendation": "通过"}),
    batch(("r", "read_evidence", {"evidence_id": "execution:0"}),
          ("s", "submit_verification", {"assessments": [], "recommendation": "通过"})),
])
def test_finalization_cannot_execute_more_tools_or_retry_forever(tmp_path, last):
    events = []
    model, verifier, result, handoff, context = setup(tmp_path,
        [call("read_evidence", {"evidence_id": "execution:0"}), last],
        max_tool_calls=1, observer=lambda *e: events.append(e))
    report = verifier.verify(handoff=handoff, result=result, specialist_results={}, execution_context=context)
    assert not report.passed and report.failure_category == "model_response" and report.retryable is False
    assert "最终提交机会" in report.blocking_reason
    assert len(model.requests) == 2 and not (tmp_path / "forbidden").exists()
    started = [e[3]["tool_name"] for e in events if e[0] == "tool_execution_started"]
    assert all(name in {"read_evidence", "submit_verification"} for name in started)
    assert started.count("read_evidence") == 1


def test_finalization_insufficient_evidence_is_not_automatic_pass(tmp_path):
    _, verifier, result, handoff, context = setup(tmp_path,
        [call("read_evidence", {"evidence_id": "execution:0"}), submission("insufficient_evidence", [])], max_tool_calls=1)
    report = verifier.verify(handoff=handoff, result=result, specialist_results={}, execution_context=context)
    assert not report.passed and report.failure_category == "insufficient_evidence"
    assert report.assessments[0].status == "insufficient_evidence"


def test_early_submission_does_not_force_extra_model_call(tmp_path):
    model, verifier, result, handoff, context = setup(tmp_path,
        [call("read_evidence", {"evidence_id": "execution:0"}), submission()])
    assert verifier.verify(handoff=handoff, result=result, specialist_results={}, execution_context=context).passed
    assert len(model.requests) == 2


def test_default_round_limit_is_six_plus_one_submission(tmp_path):
    rounds = [batch((f"read-{i}", "read_evidence", {"evidence_id": "execution:0"})) for i in range(6)]
    def finalize(messages, tool_schemas):
        assert_pairs(messages)
        assert [s["name"] for s in tool_schemas] == ["submit_verification"]
        assert '"remaining_evidence_rounds": 0' in str(messages)
        return submission()
    model, verifier, result, handoff, context = setup(tmp_path, [*rounds, finalize])
    assert verifier.verify(handoff=handoff, result=result, specialist_results={}, execution_context=context).passed
    assert len(model.requests) == 7


def test_one_evidence_can_support_multiple_criteria_in_finalization(tmp_path):
    from tikiagent.orchestration.requirements import AcceptanceCriterion
    response = call("submit_verification", {"assessments": [
        {"criterion_id": key, "status": "passed", "evidence_refs": ["execution:0"], "reason": "同一文件内容提供证据"}
        for key in ("file", "content")], "recommendation": "完成"})
    _, verifier, result, handoff, context = setup(tmp_path,
        [call("read_evidence", {"evidence_id": "execution:0"}), response], max_tool_calls=1)
    handoff = handoff.model_copy(update={"acceptance_criteria": [*handoff.acceptance_criteria,
        AcceptanceCriterion(criterion_id="content", description="文件包含 hello")]})
    report = verifier.verify(handoff=handoff, result=result, specialist_results={}, execution_context=context)
    assert report.passed and len(report.assessments) == 2 and len(report.evidence_records) == 1
