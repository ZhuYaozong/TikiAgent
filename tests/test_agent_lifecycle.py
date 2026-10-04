"""跨 Agent 的预算收尾、安全退化和消费凭据回归。"""

import json

import pytest

from tikiagent.harness.persistence.finalization import FinalizationLedger
from tikiagent.runtime.guard import AgentLoopStopped, ToolLoopGuard
from tikiagent.tools.models import ToolResult
from test_loop_guard import inputs, response
from test_resumable_react import ScriptedModel, build_runtime
from tikiagent.providers.llm.models import ModelToolCall
from test_verifier_finalization import assert_pairs


def test_code_budget_preserves_batch_and_final_submission(tmp_path):
    calls = [ModelToolCall(str(i), "read_file", '{"path":"input.txt"}') for i in range(3)]
    final = response(ModelToolCall("submit", "submit_result", json.dumps({
        "summary": "已经读取文件，等待独立验收", "delivery_status": "ready"})))
    model = ScriptedModel([response(*calls), final])
    workspace, agent = build_runtime(tmp_path, model)
    workspace.resolve("input.txt").write_text("evidence", encoding="utf-8")
    agent.max_tool_calls = 1
    result = agent.run("task", **inputs(tmp_path))
    assert result.stop_reason == "tool_budget_exhausted"
    assert result.delivery_status == "ready" and result.finalization_status == "completed"
    assert len(result.tool_results) == 1 and len(model.requests) == 2
    # 最后一次请求的具体结构由测试模型记录，不能漏掉批次未执行结果。
    request = model.requests[-1]
    messages = request["messages"] if isinstance(request, dict) else request
    assert_pairs(messages)


def test_finalization_cannot_run_work_tool(tmp_path):
    model = ScriptedModel([response(ModelToolCall("x", "write_file", '{"path":"bad","content":"bad"}'))])
    workspace, agent = build_runtime(tmp_path, model)
    agent.max_tool_calls = 0
    result = agent.run("task", **inputs(tmp_path))
    assert result.finalization_status == "failed"
    assert not workspace.resolve("bad").exists()
    assert len(model.requests) == 1


@pytest.mark.parametrize("finish", [False, True])
def test_consumption_survives_new_process_instance(tmp_path, finish):
    first = FinalizationLedger(tmp_path)
    assert first.claim("s/w/task/agent/run", "budget")
    if finish:
        first.finish("s/w/task/agent/run", "completed")
    second = FinalizationLedger(tmp_path)
    assert not second.claim("s/w/task/agent/run", "budget")
    assert second.claim("s/w/task/agent/another-run", "budget")


def test_identical_successful_reads_stop_and_snapshot_preserves_guard():
    guard = ToolLoopGuard(max_calls=20, repeat_limit=3)
    args = '{"path":"input"}'
    for _ in range(3):
        guard.record("read_file", args, ToolResult(tool_call_id="r", tool_name="read_file", ok=True, output="same"))
    restored = ToolLoopGuard(max_calls=20, repeat_limit=3, snapshot=guard.snapshot())
    with pytest.raises(AgentLoopStopped, match="相同只读"):
        restored.check("read_file", args)
    # 写入可能改变文件，允许再次取证。
    restored.record("write_file", '{}', ToolResult(tool_call_id="w", tool_name="write_file", ok=True, output={}))
    restored.check("read_file", args)


def test_research_finalization_receives_real_excerpt_and_constraints():
    from test_research_agent import ScriptedModel, dispatcher, handoff
    from tikiagent.agents.research import ResearchAgent
    class Summary:
        calls = 0
        def complete_structured(self, messages, response_type):
            self.calls += 1
            assert "new graph features" in str(messages)
            assert "原始委派约束" in str(messages)
            import hashlib
            ref = "s-" + hashlib.sha256(b"https://example.com/release").hexdigest()[:12]
            return response_type(summary="已经发现来源", findings=[{"text": "new graph features", "source_ids": [ref]}], delivery_status="ready")
    summary = Summary()
    result = ResearchAgent(model=ScriptedModel(), structured_model=summary, dispatcher=dispatcher(), max_steps=1).run(handoff=handoff())
    assert result.stop_reason == "max_steps"
    assert summary.calls == 1 and result.finalization_status == "completed"


def test_supervisor_finalization_cannot_bypass_finish_guard():
    from test_planning_supervisor import workflow, plan, call
    model, code, graph = workflow([plan("a"), call("finish_task", {"reason": "声称完成"})], max_steps=1)
    result = graph.invoke("交付 a", session_id="s")
    assert result["status"] != "completed" and not code.calls
    assert len(model.requests) == 2


def test_verifier_final_request_exception_returns_not_performed(tmp_path):
    from test_verifier_agent import setup, call
    def failed(messages, tool_schemas):
        raise RuntimeError("模拟网络失败，不包含凭据")
    model, verifier, result, handoff, context = setup(tmp_path,
        [call("read_evidence", {"evidence_id": "execution:0"}), failed], max_tool_calls=1)
    report = verifier.verify(handoff=handoff, result=result, specialist_results={}, execution_context=context)
    assert not report.passed and report.verification_status == "not_performed"
    assert len(model.requests) == 2


def test_single_structured_request_disables_sdk_and_generation_retries():
    from test_structured_output import FakeCompletions, build_client, Answer
    from tikiagent.providers.llm.structured_output import StructuredOutputError
    completions = FakeCompletions(["bad", '{"value": 1}'])
    client = build_client(completions)
    options = []
    def with_options(**kwargs):
        options.append(kwargs)
        return client.client
    client.client.with_options = with_options
    with pytest.raises(StructuredOutputError, match="1 次请求"):
        client.complete_structured_once(messages=[], response_type=Answer)
    assert options == [{"max_retries": 0}]
    assert len(completions.requests) == 1


def test_single_tool_request_does_not_regenerate_truncated_response():
    from types import SimpleNamespace
    from test_openai_compatible import DumpableMessage
    from tikiagent.providers.llm.config import ModelSettings
    from tikiagent.providers.llm.openai_compatible import OpenAICompatibleClient, ModelOutputError
    requests = []
    def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=DumpableMessage(), finish_reason="length")])
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client = OpenAICompatibleClient(ModelSettings("fake", "http://offline/v1", "offline"), client=sdk)
    with pytest.raises(ModelOutputError) as error:
        client.complete_once(messages=[], tool_schemas=[])
    assert len(requests) == 1
    assert error.value.diagnostics["attempts"][0]["finish_reason"] == "length"


def test_history_links_expand_only_inside_authorized_session(tmp_path):
    from tikiagent.application.context_refs import SessionContextReferenceProvider
    from tikiagent.context.memory.history import JsonlHistoryStore
    from tikiagent.context.memory.models import HistoryRecord
    history = JsonlHistoryStore(tmp_path / "s.jsonl")
    for key, session, refs in [("final:1", "s", ["result", "foreign"]), ("result", "s", []), ("foreign", "other", [])]:
        history.append(HistoryRecord(record_id=key, session_id=session, task_id="t", record_type="result",
                                     producer="supervisor", summary="成果", refs=refs))
    assert SessionContextReferenceProvider(tmp_path).select("s") == ["final:1", "result"]
