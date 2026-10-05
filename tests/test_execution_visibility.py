"""执行事件到显示投影的身份、边界及生命周期回归。"""

import asyncio

import pytest

from tikiagent.application.events import CollectingEventSink, EventBus
from tikiagent.application.models import EventScope
from tikiagent.application.workflow_adapter import TikiWorkflowAdapter
from tikiagent.harness.persistence.checkpoint import JsonCheckpointStore
from tikiagent.interfaces.tui.adapter import TuiEventAdapter
from tikiagent.interfaces.tui.app import ProjectedFeedCard
from tikiagent.interfaces.tui.models import TuiViewState


def event(bus, kind, *, call="c1", task="t1", run="run1", agent="code_agent", **data):
    return bus.emit(kind, scope=EventScope(session_id="s1", task_id=task, run_id=run),
        source="execution_harness_adapter" if agent == "code_agent" else "workflow_runtime_adapter",
        correlation_id=call, message=f"{data.get('tool_name', 'read_file')}: {kind}", data={"agent": agent, **data})


def test_single_card_tracks_lifecycle_keeps_parameters_and_deduplicates_requests():
    bus, adapter, state = EventBus(), TuiEventAdapter(), TuiViewState()
    for kind, data in [
        ("tool_call_requested", {"arguments": {"path": "news.html"}}),
        ("tool_call_requested", {"arguments": {"path": "news.html"}}),
        ("tool_execution_started", {"execution_id": "e1", "attempt": 1}),
        ("tool_result_received", {"execution_id": "e1", "attempt": 1,
            "tool_result": {"ok": True, "output": {"path": "news.html", "content": "<html>body</html>"}}}),
    ]:
        state = adapter.reduce(state, event(bus, kind, **data))
    assert len(state.feed) == 1 and len(state.timeline) == 4
    assert state.tool_calls == 1
    assert "news.html" in state.feed[0].summary
    assert "参数" in state.feed[0].detail and "<html>body</html>" in state.feed[0].detail
    assert state.feed[0].tool_state == "completed"
    assert state.feed[0].execution_id == "e1"
    state = adapter.reduce(state, event(bus, "tool_call_requested"))
    assert state.feed[0].tool_state == "completed"


def test_call_identity_separates_agents_runs_tasks_and_execution_attempts():
    bus, adapter, state = EventBus(), TuiEventAdapter(), TuiViewState()
    for attrs in ({}, {"agent": "verifier"}, {"run": "run2"}, {"task": "t2"}):
        state = adapter.reduce(state, event(bus, "tool_call_requested", **attrs))
    assert len(state.feed) == 4 and state.tool_calls == 4
    for identity in ("e1", "e2"):
        state = adapter.reduce(state, event(bus, "tool_execution_started", execution_id=identity, attempt=1 if identity == "e1" else 2))
        state = adapter.reduce(state, event(bus, "tool_result_received", execution_id=identity,
            tool_result={"ok": True, "output": {"path": "p", "content": "x"}}))
    assert len(state.feed) == 5
    assert len({i.card_key for i in state.feed}) == 5


@pytest.mark.parametrize(("name", "output", "word"), [
    ("list_files", {"path": ".", "files": ["a.py", "b.py"]}, "2 项"),
    ("grep", {"matches": [{"path": "a.py", "line": 1, "text": "hello"}]}, "匹配 1"),
    ("write_file", {"path": "a.py", "characters_written": 4}, "characters_written=4"),
    ("edit_file", {"path": "a.py", "replacements": 1}, "replacements=1"),
    ("web_search", {"results": [{"title": "新闻", "url": "https://example.com", "snippet": "摘要"}]}, "1 个来源"),
    ("web_extract", {"url": "https://example.com", "content": "正文"}, "example.com"),
    ("custom_tool", {"answer": 42}, "42"),
    ("submit_verification", {"assessments": [{"criterion_id": "html", "status": "passed", "reason": "文件存在"}]}, "审核意见"),
])
def test_tool_specific_summary_and_bounded_preview(name, output, word):
    state = TuiEventAdapter().reduce(TuiViewState(), event(EventBus(), "tool_result_received",
        tool_name=name, tool_result={"ok": True, "output": output}))
    assert word in state.feed[0].summary
    assert state.feed[0].detail


@pytest.mark.parametrize(("result", "status"), [
    ({"ok": False, "error": {"code": "permission_denied", "message": "拒绝"}}, "denied"),
    ({"ok": False, "error": {"code": "file_not_found", "message": "不存在"}}, "failed"),
    ({"ok": False, "error": {"code": "task_web_budget_or_duplicate", "message": "额度或重复"}}, "failed"),
    ({"ok": True, "output": {"exit_code": 1, "stderr": "FAILED"}}, "nonzero"),
    ({"ok": True, "output": {"exit_code": None, "timed_out": True}}, "timeout"),
])
def test_failures_are_not_green_success_and_do_not_fake_start(result, status):
    bus, adapter = EventBus(), TuiEventAdapter()
    state = adapter.reduce(TuiViewState(), event(bus, "tool_call_requested", tool_name="run_command", arguments={"command": ["python", "-m", "unittest"]}))
    state = adapter.reduce(state, event(bus, "tool_result_received", tool_name="run_command", tool_result=result))
    assert len(state.feed) == 1
    assert state.feed[0].tool_state == status and not state.feed[0].collapsed
    assert not any(i.title == "Tool Execution Started" for i in state.timeline)


def test_approval_pauses_same_card_and_resume_completes_without_fake_result():
    bus, adapter = EventBus(), TuiEventAdapter()
    state = adapter.reduce(TuiViewState(), event(bus, "tool_call_requested", tool_name="run_command", arguments={"command": ["pip", "install", "rich"]}))
    state = adapter.reduce(state, event(bus, "approval_required", tool_name="run_command"))
    assert state.feed[0].tool_state == "approval" and state.feed[0].result_detail == ""
    state = adapter.reduce(state, event(bus, "tool_execution_started", tool_name="run_command", execution_id="e1"))
    state = adapter.reduce(state, event(bus, "tool_result_received", tool_name="run_command", execution_id="e1",
        tool_result={"ok": True, "output": {"exit_code": 0}}))
    assert len([i for i in state.feed if i.kind == "tool"]) == 1
    assert state.feed[0].tool_state == "completed"


def todo(name, action=None, result="r1", attempts=1):
    review = {"todo_id": name, "result_id": result, "handoff_id": "h1", "verification_id": "v1", "action": action,
              "reason": "可用", "limitations": ["来源日期不确定"] if action == "accept_with_limitations" else []} if action else None
    return {"todo_id": name, "description": name + "的任务", "owner": "research_agent", "status": "completed" if action else "awaiting_review",
            "attempts": attempts, "result_id": result, "handoff_id": "h1", "verification_id": "v1", "review": review}


def test_latest_review_binding_and_global_limitations_are_not_overwritten():
    bus, adapter = EventBus(), TuiEventAdapter()
    state = adapter.reduce(TuiViewState(), event(bus, "task_board_updated", items=[todo("news", "accept_with_limitations"), todo("html", "accept")]))
    assert state.verification == "含限制"
    assert "2/2" in state.feed[0].summary
    assert "来源日期不确定" in state.feed[0].detail
    state = adapter.reduce(state, event(bus, "result_reviewed", **todo("html", "accept")["review"]))
    assert state.verification == "含限制"
    state = adapter.reduce(state, event(bus, "task_board_updated", items=[todo("news", "accept_with_limitations"), todo("html", result="r2", attempts=2)]))
    state = adapter.reduce(state, event(bus, "result_reviewed", **todo("html", "accept")["review"]))
    assert "html" not in state.review_actions
    assert state.feed[-1].title == "历史审核 · 非最新交付"
    assert state.todos[1].attempts == 2
    state = adapter.reduce(state, event(bus, "turn_received", task="t2"))
    assert not state.todos and not state.review_actions and state.tool_calls == 0


def test_stale_review_inside_board_cannot_count_as_accepted():
    raw = todo("html", "accept")
    raw["result_id"] = "new-result"
    state = TuiEventAdapter().reduce(TuiViewState(), event(EventBus(), "task_board_updated", items=[raw]))
    assert state.todos[0].review_action is None and state.todos[0].status == "awaiting_review"
    assert "0/1" in state.feed[0].summary


def test_request_fields_are_redacted_before_tui_and_long_command_keeps_exit_visible():
    bus, adapter = EventBus(), TuiEventAdapter()
    state = adapter.reduce(TuiViewState(), event(bus, "tool_call_requested", tool_name="run_command",
        arguments={"command": ["python", "--token", "secret-value", "x" * 200]}))
    state = adapter.reduce(state, event(bus, "tool_result_received", tool_name="run_command",
        tool_result={"ok": True, "output": {"exit_code": 7, "stderr": "FAILED"}}))
    assert "secret-value" not in str(state.feed)
    assert state.feed[0].summary.index("exit=7") < state.feed[0].summary.index("python")


def test_audit_displays_checks_evidence_and_recommendation_without_deciding_finish():
    state = TuiEventAdapter().reduce(TuiViewState(), event(EventBus(), "verification_completed", advisory=True,
        passed=False, todo_id="html", assessments=[{"criterion_id": "five_news", "status": "insufficient_evidence",
        "reason": "日期不能确认", "evidence_refs": ["ev1"]}], evidence_records={"ev1": {"path": "news.html", "usable": True}},
        recommendation="披露限制", failures=[]))
    assert state.verification == "待验收" and state.busy is False
    assert "five_news" in state.feed[0].detail and "news.html" in state.feed[0].detail
    assert "披露限制" in state.feed[0].detail


def test_event_bus_redacts_inline_credentials_and_counts_before_truncation():
    bus = EventBus(max_text_length=80)
    emitted = event(bus, "tool_call_requested", arguments={"command": ["curl", "--token", "very-secret", "--password=pw123"],
        "api_key": "key", "text": "Authorization: Bearer secret-value"}, reasoning_content="private reasoning")
    assert "very-secret" not in str(emitted.data) and "pw123" not in str(emitted.data)
    assert "secret-value" not in str(emitted.data) and "private reasoning" not in str(emitted.data)
    result = event(bus, "tool_result_received", tool_result={"ok": True, "output": {"content": "x" * 200, "files": [str(i) for i in range(70)]}})
    output = result.data["tool_result"]["output"]
    assert output["content_characters"] == 200 and output["files_count"] == 70
    assert len(output["content"]) <= 80 and len(output["files"]) == 50 and output["display_truncated"]


def test_graph_node_start_and_result_events_follow_actual_execution(tmp_path):
    from test_multi_agent import workflow
    graph, *_ = workflow(["research_agent", "code_agent"])
    bus, collector = EventBus(), CollectingEventSink()
    bus.subscribe(collector)
    adapter = TikiWorkflowAdapter(workflow_factory=lambda *_: graph, event_bus=bus,
        checkpoint_store=JsonCheckpointStore(tmp_path / "checkpoints"))
    outcome = adapter.start(task="生成网页", scope=EventScope(session_id="s", workspace_id="workspace", task_id="t"), context_refs=[])
    assert outcome.status == "completed"
    events = collector.events
    code_start = next(i for i, e in enumerate(events) if e.event_type == "agent_started" and e.data["agent"] == "code_agent")
    code_result = next(i for i, e in enumerate(events) if e.event_type == "specialist_result" and e.data["agent"] == "code_agent")
    verifier_start = next(i for i, e in enumerate(events) if i > code_start and e.event_type == "agent_started" and e.data["agent"] == "verification_gate")
    code_audit = next(i for i, e in enumerate(events) if e.event_type == "verification_completed" and e.data["subject_agent"] == "code_agent")
    assert code_start < code_result < verifier_start < code_audit
    result = events[code_result].data
    assert result["summary"] == "code" and result["changed_files"] == ["comparison.html"]
    assert any(e.data.get("instruction") for e in events if e.event_type == "handoff_created")


def test_real_checkpoint_resume_publishes_intermediate_graph_events(tmp_path):
    from test_multi_agent_resume import build_workflow, ScriptedModel, first_response, final_response
    from tikiagent.context.memory.history import JsonlHistoryStore
    from tikiagent.application.harness_events import HarnessEventForwarder
    bus, collector = EventBus(), CollectingEventSink()
    bus.subscribe(collector)
    models = iter([ScriptedModel([first_response()]), ScriptedModel([final_response()])])
    def factory(*_):
        workspace, graph = build_workflow(tmp_path, next(models), history_store=JsonlHistoryStore(tmp_path / "history.jsonl"))
        workspace.resolve("input.txt").write_text("input", encoding="utf-8")
        graph.code_agent.agent.execution_coordinator.lifecycle_observer = HarnessEventForwarder(bus)
        return graph
    adapter = TikiWorkflowAdapter(workflow_factory=factory, event_bus=bus, checkpoint_store=JsonCheckpointStore(tmp_path / "checkpoints"))
    scope = EventScope(session_id="session-1", workspace_id="workspace-1", task_id="task-1")
    paused = adapter.start(task="生成 output.txt", scope=scope, context_refs=[])
    assert paused.status == "awaiting_approval"
    before = len(collector.events)
    completed = adapter.resume_approval(checkpoint_id=paused.checkpoint_id, expected_revision=paused.checkpoint_revision,
        scope=scope, request_id=paused.approval_request_id, approved=True)
    assert completed.status == "completed"
    resumed = collector.events[before:]
    assert any(e.event_type == "specialist_result" and "output.txt" in e.data["changed_files"] for e in resumed)
    assert any(e.event_type == "task_board_updated" for e in resumed)
    assert any(e.event_type == "verification_completed" for e in resumed)
    state = TuiViewState()
    projection = TuiEventAdapter()
    for emitted in collector.events:
        state = projection.reduce(state, emitted)
    writes = [i for i in state.feed if i.call_key and '"write-1"' in i.call_key]
    assert len(writes) == 1 and writes[0].tool_state == "completed"


def test_research_web_events_include_query_sources_and_no_start_for_duplicate():
    from test_research_agent import ScriptedModel, ScriptedStructuredModel, dispatcher, handoff
    from tikiagent.agents.research import ResearchAgent
    captured = []
    model = ScriptedModel()
    original = model.complete
    def duplicate_once(messages, tool_schemas):
        if model.calls == 1:
            model.calls += 1
            from tikiagent.providers.llm.models import ModelResponse, ModelToolCall
            return ModelResponse(assistant_message={"role": "assistant", "content": None},
                tool_calls=(ModelToolCall("repeat", "web_search", '{"query":"latest agent framework"}'),))
        return original(messages, tool_schemas)
    model.complete = duplicate_once
    agent = ResearchAgent(model=model, structured_model=ScriptedStructuredModel("https://example.com/release"),
        dispatcher=dispatcher(), observer=lambda kind, state, ref, data: captured.append((kind, ref, data)))
    agent.run(handoff())
    assert [k for k, ref, _ in captured if ref == "search-call-1"] == ["tool_call_requested", "tool_execution_started", "tool_result_received"]
    assert [k for k, ref, _ in captured if ref == "repeat"] == ["tool_call_requested", "tool_result_received"]
    assert captured[0][2]["arguments"]["query"] == "latest agent framework"
    assert captured[2][2]["tool_result"]["output"]["results"][0]["url"] == "https://example.com/release"


def test_textual_updates_card_in_place_preserves_expand_and_reading_position(tmp_path):
    from test_tui_app import build_app
    async def exercise():
        app, _ = build_app(tmp_path)
        async with app.run_test(size=(130, 34)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            bus = app.event_bus
            event(bus, "tool_call_requested", arguments={"path": "news.html"})
            await pilot.pause()
            card = app.query(ProjectedFeedCard).last()
            card.collapsed = False
            event(bus, "tool_result_received", tool_result={"ok": True, "output": {"path": "news.html", "content": "hello"}})
            await pilot.pause()
            assert app.query(ProjectedFeedCard).last() is card and not card.collapsed
            assert "news.html" in card.title and "执行完成" in card.title
            for index in range(20):
                event(bus, "agent_started", call=f"agent-{index}", agent="supervisor")
            event(bus, "tool_call_requested", call="bottom-call", arguments={"path": "missing.txt"})
            await pilot.pause()
            feed = app.query_one("#feed")
            feed.scroll_home(animate=False)
            await pilot.pause()
            before = feed.scroll_y
            event(bus, "agent_started", call="next", agent="verifier")
            event(bus, "tool_result_received", call="bottom-call",
                tool_result={"ok": False, "error": {"code": "file_not_found", "message": "不存在"}})
            await pilot.pause()
            assert feed.max_scroll_y > 0 and feed.scroll_y == before
            # 窗口限长淘汰旧卡片时，不能因总数量不变而忽略新增内容。
            app.adapter.timeline_limit = 3
            for index in range(5):
                event(bus, "agent_started", call=f"bounded-{index}", agent="supervisor")
            await pilot.pause()
            assert len(app.view_state.feed) == 3 and len(app._feed_widgets) == 3
    asyncio.run(exercise())
