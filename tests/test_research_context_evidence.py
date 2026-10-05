"""委派作用域、统一证据链和日期边界的离线回归。"""

import json
from copy import deepcopy

import pytest
from pydantic import BaseModel

from tikiagent.agents.research import ResearchAgent
from tikiagent.agents.research_evidence import ResearchEvidence, provenance_valid, relevant_excerpt, source_id
from tikiagent.context.builder import ContextBuilder
from tikiagent.context.memory.history import InMemoryHistoryStore, JsonlHistoryStore
from tikiagent.context.memory.models import HistoryRecord
from tikiagent.context.memory.retriever import Retriever
from tikiagent.context.models import BaseContext, ContextRequest, TaskBoard, TodoItem, WorkingMemory
from tikiagent.orchestration.completion import _research_sections
from tikiagent.orchestration.contracts import Handoff, ResearchResult
from tikiagent.orchestration.state import create_multi_agent_state, restore_tiki_state, serialize_tiki_state
from tikiagent.orchestration.workflow import MultiAgentWorkflow
from tikiagent.providers.llm.models import ModelResponse, ModelToolCall
from tikiagent.tools.dispatcher import Dispatcher
from tikiagent.tools.models import ToolExecutionError, ToolResult
from tikiagent.tools.registry import RegisteredTool, ToolRegistry
from tikiagent.verification.research import ResearchResultVerifier
from test_conditional_verification import gate
from test_multi_agent_resume import build_workflow, first_response, final_response, ScriptedModel
from tikiagent.harness.permissions.models import ApprovalDecision
from test_planning_supervisor import Script, Gate, call, delegate, review
from tikiagent.agents.planning import PlanningSupervisorAgent
from test_web_tools import FakeClient
from tikiagent.providers.search.config import SearchSettings
from tikiagent.providers.search.tavily import TavilyProvider


URL = "https://example.org/2026/10/05/news"


class SearchArgs(BaseModel):
    query: str


class ExtractArgs(BaseModel):
    url: str


def handoff(refs=None):
    return Handoff(handoff_id="h", from_agent="supervisor", to_agent="research_agent", todo_id="news",
        instruction="今天的新闻，正文发布日期和 Agent 功能", context_refs=refs or [],
        verification_level="basic", acceptance_criteria=[{"criterion_id": "news", "description": "提供带来源的新闻"}])


def context():
    return BaseContext(agent="research_agent", working_memory=WorkingMemory(task="搜索并生成网页",
        task_id="task", session_id="session", phase="research", instruction="当前新闻 Todo"))


class Actions:
    def __init__(self, actions):
        self.actions = list(actions)
        self.requests = []

    def complete(self, messages, tool_schemas):
        self.requests.append((messages, tool_schemas))
        if not self.actions:
            return ModelResponse(assistant_message={"role": "assistant", "content": "证据足够"}, final_text="证据足够")
        name, args = self.actions.pop(0)
        return ModelResponse(assistant_message={"role": "assistant"}, tool_calls=(
            ModelToolCall(f"call-{len(self.requests)}", name, json.dumps(args)),))


class Summary:
    def __init__(self, url=URL, fail=False):
        self.url, self.fail, self.requests = url, fail, []

    def complete_structured(self, messages, response_type):
        self.requests.append(messages)
        if self.fail:
            raise ValueError("离线模拟格式错误")
        return response_type(summary="已总结现有证据", findings=[{
            "text": "Agent 发布了新功能，发布日期需按原文判断", "source_ids": [source_id(self.url)]}],
            delivery_status="ready", unresolved_questions=[])


def agent(actions, *, search=None, extract=None, fail_extract=False, summary=None):
    registry = ToolRegistry()
    registry.register(RegisteredTool("web_search", "搜索", SearchArgs,
        lambda query: {"query": query, "results": search or [{"url": URL, "title": "Agent 发布", "snippet": "Agent 新功能"}]}))
    def fetch(url):
        if fail_extract:
            raise ToolExecutionError("extract_failed", "离线提取失败")
        return {"url": url, "content": "发布日期：2026-10-05；Agent 新功能", **(extract or {})}
    registry.register(RegisteredTool("web_extract", "提取", ExtractArgs, fetch))
    model, summarizer = Actions(actions), summary or Summary()
    return ResearchAgent(model=model, structured_model=summarizer, dispatcher=Dispatcher(registry)), model, summarizer


def history_record():
    result = ResearchResult(result_id="previous", handoff_id="old-h", summary="先前搜索结果", findings=["新功能"],
        sources=[{"observation_id": "old-search", "url": URL, "title": "新闻", "snippet": "搜索摘要"}],
        observations=[{"observation_id": "old-search", "query": "新闻", "urls": [URL]}], queries=["新闻"], delivery_status="ready")
    return HistoryRecord(record_id="previous", task_id="old-task", session_id="session", record_type="result",
        producer="research_agent", summary="先前结果", payload=result.model_dump(mode="json"), refs=["old-h"])


def test_extract_only_has_evidence_citations_and_passes_basic_gate(tmp_path):
    researcher, model, summarizer = agent([("web_extract", {"url": URL})])
    result = researcher.run(handoff(), context())
    assert result.sources[0].evidence_kind == "extract"
    assert result.observations[0].kind == "extract" and result.queries == []
    assert result.finding_citations[0].source_ids == [source_id(URL)]
    assert "2026-10-05" in result.sources[0].snippet
    assert len(summarizer.requests) == 1 and len(model.requests) == 2
    hf = handoff().model_copy(update={"result_id": result.result_id, "status": "completed"})
    verifier, spy = gate(tmp_path)
    report = verifier.verify(handoff=hf, raw_result=result.model_dump(), specialist_results={})
    assert report.passed and not spy.calls
    assert ResearchResultVerifier().verify(handoff=hf, result=result, specialist_results={}).passed


@pytest.mark.parametrize("date", [None, "2026-10-05T08:00:00+08:00"])
def test_search_date_is_preserved_without_guessing_from_url(date):
    researcher, _, summarizer = agent([("web_search", {"query": "Agent"})], search=[{
        "title": "新闻", "url": URL, "snippet": "日期未确定", "published_date": date}])
    result = researcher.run(handoff(), context())
    assert result.sources[0].published_date == date
    assert result.sources[0].evidence_kind == "search"
    prompt = "\n".join(m["content"] for m in summarizer.requests[0])
    assert ('"published_date": null' if date is None else date) in prompt


def test_body_date_remains_evidence_not_invented_provider_metadata():
    researcher, _, _ = agent([("web_extract", {"url": URL})])
    result = researcher.run(handoff(), context())
    assert result.sources[0].published_date is None
    assert "发布日期：2026-10-05" in result.sources[0].snippet


def test_failed_extract_preserves_search_but_discloses_partial():
    researcher, _, _ = agent([("web_search", {"query": "Agent"}), ("web_extract", {"url": URL})], fail_extract=True)
    result = researcher.run(handoff(), context())
    assert result.delivery_status == "partial"
    assert result.sources[0].evidence_kind == "search"
    assert [o.kind for o in result.observations] == ["search"]
    assert any("extract_failed" in q for q in result.unresolved_questions)


def test_instruction_url_without_tool_or_authorized_history_is_not_evidence():
    researcher, _, _ = agent([])
    result = researcher.run(handoff().model_copy(update={"instruction": f"总结 {URL}"}), context())
    assert result.delivery_status == "none" and not result.sources and not result.findings


def test_explicit_history_can_summarize_without_new_search_and_gate_checks_original(tmp_path):
    record = history_record()
    hf = handoff([record.record_id])
    researcher, model, _ = agent([])
    result = researcher.run(hf, context(), history_records=[record])
    assert len(model.requests) == 1 and not result.queries
    assert result.sources[0].evidence_kind == "history"
    assert result.observations[0].history_record_id == record.record_id
    assert result.observations[0].original_observation_id == "old-search"
    verifier, _ = gate(tmp_path)
    hf = hf.model_copy(update={"status": "completed", "result_id": result.result_id})
    assert verifier.verify(handoff=hf, raw_result=result.model_dump(), specialist_results={}, research_results=[record.payload]).passed
    denied = verifier.verify(handoff=hf, raw_result=result.model_dump(), specialist_results={})
    assert not denied.passed and denied.failure_category == "identity"


def test_history_plus_current_extract_uses_actual_body_observation():
    researcher, _, _ = agent([("web_extract", {"url": URL})])
    result = researcher.run(handoff(["previous"]), context(), history_records=[history_record()])
    assert result.sources[0].evidence_kind == "extract" and result.sources[0].observation_id == "call-1"
    assert {o.kind for o in result.observations} == {"extract", "history"}
    assert not result.queries


@pytest.mark.parametrize("defect", ["cross_session", "not_explicit", "record_identity", "provenance", "handoff_binding"])
def test_untrusted_history_cannot_become_evidence(defect):
    record = deepcopy(history_record())
    hf = handoff(["previous"])
    if defect == "cross_session":
        record.session_id = "other-session"
    elif defect == "not_explicit":
        hf.context_refs = []
    elif defect == "record_identity":
        record.payload["result_id"] = "stale-result"
    elif defect == "provenance":
        record.payload["sources"][0]["url"] = "https://forged.example"
    else:
        record.refs = []
    researcher, _, _ = agent([])
    result = researcher.run(hf, context(), history_records=[record])
    assert not result.sources and result.delivery_status == "none"


def test_long_body_keeps_late_date_and_catalog_prioritizes_extract_over_first_twelve():
    content = "导航菜单\n" * 700 + "发布日期：2026-10-05\nAgent 关键功能已发布\n" + "页脚\n" * 700
    excerpt = relevant_excerpt(content, "Agent 新功能")
    assert len(excerpt) <= 1400 and "2026-10-05" in excerpt and "关键功能" in excerpt
    store = ResearchEvidence()
    store.observe(ToolResult(tool_call_id="search", tool_name="web_search", ok=True,
        output={"query": "Agent", "results": [{"url": f"https://example.org/{i}", "title": "无关", "snippet": "导航"} for i in range(15)]}))
    store.observe(ToolResult(tool_call_id="extract", tool_name="web_extract", ok=True, output={"url": URL, "content": content}))
    catalog = store.catalog("Agent 新功能")
    assert len(catalog) == 12 and next(iter(catalog)) == source_id(URL)
    assert "2026-10-05" in catalog[source_id(URL)].snippet


def test_current_extract_counts_as_progress_even_when_url_was_found():
    researcher, model, _ = agent([("web_search", {"query": "Agent"}), ("web_search", {"query": "Agent 发布"}),
        ("web_extract", {"url": URL}), ("web_search", {"query": "发布日期"})])
    result = researcher.run(handoff(), context())
    assert result.stop_reason is None and len(model.requests) == 5
    assert result.sources[0].evidence_kind == "extract"


def test_synthesis_failure_does_not_search_again_and_preserves_sources():
    researcher, model, summary = agent([("web_extract", {"url": URL})], summary=Summary(fail=True))
    result = researcher.run(handoff(), context())
    assert result.finalization_status == "failed"
    assert len(model.requests) == 2 and len(summary.requests) == 1
    assert result.sources and result.delivery_status == "partial" and not result.finding_citations


def test_citation_integrity_and_final_rendering(tmp_path):
    researcher, _, _ = agent([("web_extract", {"url": URL})])
    result = researcher.run(handoff(), context())
    rendered = "\n".join(_research_sections(result))
    assert "[来源 1]" in rendered and "日期字段未知" in rendered and "已提取正文" in rendered
    result.finding_citations[0].source_ids = ["invented-source"]
    assert not provenance_valid(result)
    verifier, _ = gate(tmp_path)
    report = verifier.verify(handoff=handoff().model_copy(update={"status": "completed", "result_id": result.result_id}),
        raw_result=result.model_dump(), specialist_results={})
    assert not report.passed and report.failure_category == "identity"


def test_current_todo_context_does_not_inherit_other_owner_todos():
    board = TaskBoard(items={key: TodoItem(todo_id=key, description=key, owner="research_agent",
        acceptance_criteria=[{"criterion_id": key, "description": key + "要求"}]) for key in ("news", "comparison")})
    builder = ContextBuilder(Retriever(InMemoryHistoryStore()))
    req = ContextRequest(agent="research_agent", task_id="t", session_id="s", phase="research", instruction="新闻", current_todo_id="news")
    value = builder.build(request=req, task="新闻和比较后生成HTML", acceptance_criteria=["生成HTML"], task_board=board,
        task_reference_time="2026-10-05T08:00:00+08:00")
    assert [t.todo_id for t in value.working_memory.todos] == ["news"]
    assert value.working_memory.acceptance_criteria == ["news要求"]
    assert value.working_memory.background_acceptance_criteria == ["生成HTML"]
    assert "2026-10-05T08:00:00+08:00" in value.render()
    with pytest.raises(ValueError, match="作用域"):
        builder.build(request=req.model_copy(update={"current_todo_id": "missing"}), task="背景", acceptance_criteria=[], task_board=board)


def test_old_snapshots_and_results_do_not_fabricate_new_evidence():
    state = create_multi_agent_state(task="新闻", workspace_id="w", max_steps=10, max_delegations=2)
    snapshot = serialize_tiki_state(state)
    assert restore_tiki_state(snapshot)["task_reference_time"] == state["task_reference_time"]
    snapshot.pop("task_reference_time")
    assert restore_tiki_state(snapshot)["task_reference_time"] is None
    result = ResearchResult.model_validate(history_record().payload)
    assert result.finding_citations == [] and result.sources[0].published_date is None
    assert provenance_valid(result)


def test_extract_provider_only_passes_real_date_metadata():
    provider = TavilyProvider(SearchSettings(api_key="offline"), client=FakeClient({"results": [{
        "url": URL, "raw_content": "正文发布日期2026-10-05", "title": "新闻", "published_date": "2026-10-05"}]}))
    assert provider.extract(URL)["published_date"] == "2026-10-05"
    provider.client = FakeClient({"results": [{"url": URL, "raw_content": "正文"}]})
    assert provider.extract(URL)["published_date"] is None


def test_real_graph_current_todo_and_authorized_history_path(tmp_path):
    record = history_record()
    history = InMemoryHistoryStore()
    history.append(record)
    researcher, model, _ = agent([("web_extract", {"url": URL})])
    planner = Script([call("update_plan", {"goal": "调研", "acceptance_criteria": ["新闻后生成页面"],
        "todos": [{"todo_id": "news", "description": "搜索新闻", "owner": "research_agent",
                   "required_capabilities": ["web_research"], "acceptance_criteria": [{"criterion_id": "news", "description": "新闻来源"}]}]}),
        call("delegate_task", {"todo_id": "news", "instruction": "复用来源并提取正文", "reason": "取得证据", "context_refs": ["previous"]}),
        review("news"), call("finish_task", {"reason": "已有交付"})])
    verifier, _ = gate(tmp_path)
    graph = MultiAgentWorkflow(supervisor=PlanningSupervisorAgent(planner), research_agent=researcher, code_agent=None,
        verification_gate=verifier, workspace_id="w", history_store=history)
    result = graph.invoke("搜索新闻", session_id="session", task_id="task", session_context_refs=["previous"])
    assert result["status"] == "completed"
    raw = result["specialist_results"]["research_agent"]
    assert {o["kind"] for o in raw["observations"]} == {"history", "extract"}
    first = json.dumps(model.requests[0][0], ensure_ascii=False)
    assert "新闻来源" in first and result["task_reference_time"] in first
    assert history.get_by_id(raw["result_id"]).payload["finding_citations"]


def test_actual_approval_checkpoint_keeps_task_time_and_todo_context(tmp_path):
    # 使用真实 ASK/新进程 Graph Resume，不靠伪造快照证明兼容。
    workspace, first = build_workflow(tmp_path, ScriptedModel([first_response()]),
        history_store=JsonlHistoryStore(tmp_path / "history.jsonl"))
    workspace.resolve("input.txt").write_text("input", encoding="utf-8")
    paused = first.invoke("生成output", task_id="task", session_id="session")
    checkpoint_id = paused["runtime_checkpoint_id"]
    cp = first.code_agent.agent.execution_coordinator.checkpoint_store.load(checkpoint_id)
    frozen = cp.workflow_snapshot.state["task_reference_time"]
    wm = cp.react_snapshot.base_context["working_memory"]
    assert wm["task_reference_time"] == frozen and len(wm["todos"]) == 1
    assert wm["todos"][0]["todo_id"] == cp.workflow_snapshot.state["latest_handoff"]["todo_id"]
    request = cp.approval_request
    _, second = build_workflow(tmp_path, ScriptedModel([final_response()]))
    result = second.resume(checkpoint_id, expected_revision=cp.revision,
        approval_decision=ApprovalDecision(request_id=request.request_id, approved=True, scope=request.scope, fingerprint=request.fingerprint))
    assert result["status"] == "completed" and result["task_reference_time"] == frozen


def test_workflow_does_not_import_same_session_cross_task_without_application_authorization(tmp_path):
    record = history_record()
    history = InMemoryHistoryStore()
    record = history.append(record)
    researcher, _, _ = agent([])
    graph = MultiAgentWorkflow(supervisor=object(), research_agent=researcher, code_agent=None,
        verification_gate=gate(tmp_path)[0], workspace_id="w", history_store=history)
    state = create_multi_agent_state(task="新闻", task_id="task", session_id="session", workspace_id="w", max_steps=10, max_delegations=2)
    assert graph._research_history_arguments(state, handoff(["previous"]))["history_records"] == []
    state["session_context_refs"] = ["previous"]
    assert graph._research_history_arguments(state, handoff(["previous"]))["history_records"] == [record]


def test_supplied_metadata_change_is_progress_but_identical_content_is_not():
    store = ResearchEvidence()
    def search(date=None):
        return ToolResult(tool_call_id="search", tool_name="web_search", ok=True, output={"query": "新闻", "results": [
            {"url": URL, "title": "发布", "snippet": "新功能", "published_date": date}]})
    assert store.observe(search()) is True
    assert store.observe(search()) is False
    assert store.observe(search("2026-10-05")) is True
    assert store.observe(search("2026-10-05")) is False


def test_source_cannot_claim_body_extraction_from_search_observation():
    result = ResearchResult.model_validate(history_record().payload)
    result.sources[0].evidence_kind = "extract"
    assert not provenance_valid(result)


def test_summary_projection_preserves_failures_and_citations_not_page_text():
    from tikiagent.context.projections import research_view
    researcher, _, _ = agent([("web_search", {"query": "新闻"}), ("web_extract", {"url": URL})], fail_extract=True)
    result = researcher.run(handoff(), context())
    view = research_view(result.model_dump(mode="json"))
    assert view["delivery_status"] == "partial" and view["finding_citations"]
    assert any("extract_failed" in q for q in view["unresolved_questions"])
    assert "snippet" not in view["sources"][0]


def test_research_result_and_citations_survive_downstream_code_approval_resume(tmp_path):
    workspace, first = build_workflow(tmp_path, ScriptedModel([first_response()]),
        history_store=JsonlHistoryStore(tmp_path / "hybrid-history.jsonl"))
    workspace.resolve("input.txt").write_text("input", encoding="utf-8")
    researcher, _, _ = agent([("web_extract", {"url": URL})], extract={"published_date": "2026-10-05"})
    first.research_agent = researcher
    first.verification_gate = Gate()
    first.supervisor = PlanningSupervisorAgent(Script([
        call("update_plan", {"goal": "调研后交付文件", "acceptance_criteria": ["调研与文件分别交付"], "todos": [
            {"todo_id": "news", "description": "调研新闻", "owner": "research_agent"},
            {"todo_id": "output", "description": "生成文件", "owner": "code_agent", "depends_on": ["news"]}]}),
        delegate("news"), review("news"), delegate("output")]))
    paused = first.invoke("调研并创建文件", task_id="task", session_id="session")
    assert paused["status"] == "awaiting_approval"
    cp = first.code_agent.agent.execution_coordinator.checkpoint_store.load(paused["runtime_checkpoint_id"])
    research_result = cp.workflow_snapshot.state["specialist_results"]["research_agent"]
    assert research_result["finding_citations"] and research_result["sources"][0]["published_date"] == "2026-10-05"
    code_memory = cp.react_snapshot.base_context["working_memory"]
    assert [t["todo_id"] for t in code_memory["todos"]] == ["output"]
    assert any(r["record_id"] == research_result["result_id"] for r in code_memory["relevant_history"])
    _, second = build_workflow(tmp_path, ScriptedModel([final_response()]))
    second.research_agent, unused_model, _ = agent([])
    second.supervisor = PlanningSupervisorAgent(Script([review("output"), call("finish_task", {"reason": "已接受两项交付"})]))
    second.verification_gate = Gate()
    request = cp.approval_request
    completed = second.resume(cp.checkpoint_id, expected_revision=cp.revision,
        approval_decision=ApprovalDecision(request_id=request.request_id, approved=True, scope=request.scope, fingerprint=request.fingerprint))
    assert completed["status"] == "completed" and unused_model.requests == []
    assert completed["specialist_results"]["research_agent"] == research_result
    assert completed["task_reference_time"] == paused["task_reference_time"]
