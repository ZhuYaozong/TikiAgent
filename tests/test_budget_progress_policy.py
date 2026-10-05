"""预算、收尾诊断、无进展保护与恢复：全部使用离线夹具。"""
import json
from types import SimpleNamespace
import pytest

from tikiagent.harness.persistence.budget import RequestBudget, RequestBudgetExceeded
from tikiagent.providers.llm.staged import StageModel
from tikiagent.providers.llm.openai_compatible import OpenAICompatibleClient
from tikiagent.providers.llm.config import ModelSettings
from tikiagent.runtime.guard import ToolLoopGuard, AgentLoopStopped, workspace_revision
from tikiagent.runtime.diagnostics import finalization_error
from tikiagent.tools.models import ToolResult, ToolError


def test_requests_reserve_finalization_and_survive_reload(tmp_path):
    budget = RequestBudget(tmp_path, model_limit=18)
    budget.bind("s", "t")
    budget.model_request()
    budget.model_request()
    restored = RequestBudget(tmp_path, model_limit=64)
    restored.bind("s", "t")
    with pytest.raises(RequestBudgetExceeded):
        restored.model_request()
    for _ in range(16):
        restored.model_request(final=True)
    with pytest.raises(RequestBudgetExceeded):
        restored.model_request(final=True)


def test_web_budget_shared_between_runs_and_task_scoped(tmp_path):
    first = RequestBudget(tmp_path, web_limit=2)
    first.bind("s", "task")
    first.web_request("search-1")
    second = RequestBudget(tmp_path, web_limit=10)
    second.bind("s", "task")
    with pytest.raises(RequestBudgetExceeded):
        second.web_request("search-1")
    second.web_request("search-2")
    with pytest.raises(RequestBudgetExceeded):
        second.web_request("extract-1")
    second.bind("s", "other-task")
    second.web_request("search-1")


def test_budget_lock_is_fail_closed(tmp_path):
    budget = RequestBudget(tmp_path)
    budget.bind("s", "t")
    with budget._state():
        with pytest.raises(RequestBudgetExceeded):
            budget.model_request()


def test_stage_output_limits_and_safe_diagnostics(tmp_path):
    from test_openai_compatible import DumpableMessage
    requests, events = [], []
    def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=DumpableMessage(), finish_reason="tool_calls")])
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    original = OpenAICompatibleClient(ModelSettings("fake", "http://offline", "model"), client=sdk)
    budget = RequestBudget(tmp_path)
    budget.bind("s", "t")
    model = StageModel(original, "code_agent", account=budget, observer=events.append)
    model.complete([], [])
    model.for_stage("code_final").complete_once([], [])
    assert [r["max_tokens"] for r in requests] == [8192, 3072]
    assert [e["stage"] for e in events] == ["code_agent", "code_final"]
    assert "fake" not in json.dumps(events)


def test_truncated_structured_output_has_diagnosis():
    from test_structured_output import build_client, FakeCompletions, Answer
    from tikiagent.providers.llm.structured_output import StructuredOutputError
    class Truncated(FakeCompletions):
        def create(self, **kwargs):
            response = super().create(**kwargs)
            response.choices[0].finish_reason = "length"
            return response
    client = build_client(Truncated(['{"value":']))
    with pytest.raises(StructuredOutputError) as error:
        client.complete_structured_once([], Answer)
    assert finalization_error(error.value)["error_category"] == "output_truncated"


def test_synthesis_failure_is_not_a_request_to_search_again():
    from tikiagent.orchestration.contracts import Handoff, ResearchResult
    from tikiagent.verification.failures import execution_failure
    handoff = Handoff(from_agent="supervisor", to_agent="research_agent", instruction="search")
    result = ResearchResult(handoff_id=handoff.handoff_id, summary="partial", stop_reason="tool_budget_exhausted",
        finalization_status="failed", finalization_diagnostics={"error_category": "output_truncated"})
    report = execution_failure(handoff, result)
    assert report.failure_category == "model_response" and report.allowed_actions == ["stop"]
    assert "output_truncated" in report.blocking_reason


def test_successful_test_not_repeated_without_workspace_change(tmp_path):
    source = tmp_path / "calculator.py"
    source.write_text("x = 1", encoding="utf-8")
    guard = ToolLoopGuard(max_calls=24, repeat_limit=2, revision_provider=lambda: workspace_revision(tmp_path))
    args = json.dumps({"command": ["python", "-m", "unittest"]})
    guard.check("run_command", args)
    guard.record("run_command", args, ToolResult(tool_call_id="t", tool_name="run_command", ok=True,
        output={"exit_code": 0, "timed_out": False}))
    restored = ToolLoopGuard(max_calls=24, repeat_limit=2, snapshot=guard.snapshot(), revision_provider=lambda: workspace_revision(tmp_path))
    with pytest.raises(AgentLoopStopped):
        restored.check("run_command", args)
    source.write_text("x = 222", encoding="utf-8")
    restored.check("run_command", args)


def test_denial_is_not_reset_by_reads_or_resume():
    guard = ToolLoopGuard(max_calls=24, repeat_limit=2)
    args = '{"command":["python","-m","pip","install","rich"]}'
    guard.record("run_command", args, ToolResult(tool_call_id="denied", tool_name="run_command", ok=False,
        error=ToolError(code="approval_rejected", message="denied")))
    guard.record("read_file", '{"path":"a"}', ToolResult(tool_call_id="read", tool_name="read_file", ok=True, output="a"))
    restored = ToolLoopGuard(max_calls=24, repeat_limit=2, snapshot=guard.snapshot())
    with pytest.raises(AgentLoopStopped, match="明确拒绝"):
        restored.check("run_command", args)


def test_research_result_has_bounded_claims_and_no_generated_source_body():
    from test_research_agent import ScriptedModel, ScriptedStructuredModel, dispatcher, handoff
    from tikiagent.agents.research import ResearchAgent, CompactResearchDraft
    result = ResearchAgent(model=ScriptedModel(), structured_model=ScriptedStructuredModel("https://example.com/release"),
        dispatcher=dispatcher()).run(handoff())
    assert result.delivery_status == "ready" and result.finalization_status == "completed"
    schema = CompactResearchDraft.model_json_schema()
    assert "sources" not in schema["properties"]
    assert result.sources[0].snippet == "new graph features"


def test_verifier_reuses_own_evidence_without_reexecuting(tmp_path):
    from test_verifier_agent import setup, call, submission
    events = []
    model, verifier, result, handoff, context = setup(tmp_path, [call("read_evidence", {"evidence_id": "execution:0"}),
        call("read_evidence", {"evidence_id": "execution:0"}), submission()], observer=lambda *e: events.append(e), deduplicate=True)
    assert verifier.verify(handoff=handoff, result=result, specialist_results={}, execution_context=context).passed
    assert sum(e[0] == "tool_execution_started" for e in events) == 2  # 一次读取、一次提交。


def test_context_and_api_reserve_same_output(tmp_path):
    from tikiagent.application.bootstrap import ApplicationRuntimeFactory
    from tikiagent.context.models import BaseContext, WorkingMemory
    from tikiagent.context.memory.models import LocalMemory
    factory = ApplicationRuntimeFactory(tmp_path / "data", env_file=tmp_path / "none")
    workflow = factory._build_workflow("s", "w")
    runtime = workflow.code_agent.agent.context_runtime
    runtime.prepare(base_context=BaseContext(agent="code_agent", working_memory=WorkingMemory(task="test", phase="coding", instruction="test")),
        local_memory=LocalMemory(), registry=workflow.code_agent.agent.dispatcher.registry)
    assert runtime.budget.reserved_output_tokens == 8192
    assert runtime.budget.model_context_limit == 30000


def test_redelegation_requires_concrete_change_and_is_bounded():
    from test_planning_supervisor import workflow, plan, delegate, review, stop, call, Gate
    retry = call("delegate_task", {"todo_id": "a", "instruction": "补充文件取证", "reason": "补证",
        "missing_evidence": "缺少文件内容", "strategy_change": "直接读取文件",
        "expected_evidence": "文件内容及路径"}, "retry-a")
    model, code, graph = workflow([plan("a"), delegate("a"), review("a", "request_changes"), delegate("a"), retry, review("a", "request_changes"), retry, stop()], gate=Gate(False))
    result = graph.invoke("核实本地文件")
    assert len(code.calls) == 2
    assert result["delegation_count"] == 2
    assert "replan_requires_evidence" in str(model.requests)


def test_observer_io_failure_does_not_repeat_model_request():
    from test_openai_compatible import DumpableMessage
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=DumpableMessage(), finish_reason="tool_calls")])
    def broken_observer(data):
        raise OSError("审计磁盘不可写")
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client = OpenAICompatibleClient(ModelSettings("fake", "http://offline", "model"), client=sdk, observer=broken_observer)
    assert client.complete([], []).tool_calls
    assert len(calls) == 1
