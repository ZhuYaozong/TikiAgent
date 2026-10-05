"""显式启用才调用 .env API；所有执行产物都放在 pytest 临时目录。"""

import os
from pathlib import Path

import pytest

from tikiagent.application.bootstrap import ApplicationRuntimeFactory
from tikiagent.application.events import CollectingEventSink, EventBus
from tikiagent.interfaces.tui.adapter import TuiEventAdapter
from tikiagent.interfaces.tui.models import TuiViewState


pytestmark = pytest.mark.skipif(os.getenv("TIKI_RUN_VISIBILITY_LIVE") != "1", reason="真实 API 验证必须显式启用")


@pytest.mark.parametrize(("case", "task", "expected_agents", "artifact"), [
    ("research", "请联网检索 Python 官方文档，简要说明 unittest 的用途，并附一个 python.org 文档来源链接；不创建文件。", {"research_agent"}, None),
    ("coding", "请在当前 Workspace 创建 index.html，包含合法 HTML 结构和正文 Hello TikiAgent；只需读取文件确认内容，不安装依赖，不启动服务器。", {"code_agent"}, "index.html"),
    ("hybrid", "先联网检索 Python 官方 unittest 文档，整理一句带原文来源的用途说明；再在当前 Workspace 创建 unittest.html，展示这句说明及可点击的官方来源链接。不要安装依赖或启动服务器。", {"research_agent", "code_agent"}, "unittest.html"),
])
def test_live_visibility_projection(tmp_path, case, task, expected_agents, artifact):
    bus, collector = EventBus(), CollectingEventSink()
    bus.subscribe(collector)
    factory = ApplicationRuntimeFactory(tmp_path / case, env_file=Path(__file__).parents[1] / ".env", event_bus=bus)
    controller = factory.build_controller()
    session = controller.new_session(workspace_id="visibility-" + case)
    outcome = controller.submit(session_id=session.session_id, user_input=task)
    state = TuiViewState()
    adapter = TuiEventAdapter()
    for event in collector.events:
        state = adapter.reduce(state, event)
    print(f"\n{case}: status={outcome.status}; events={len(collector.events)}; requests={state.tool_calls}; tool_cards={len([i for i in state.feed if i.call_key])}; todos={len(state.todos)}")
    assert outcome.status == "workflow_completed"
    observed = {event.data.get("agent") for event in collector.events if event.event_type == "specialist_result"}
    assert expected_agents <= observed
    assert state.final_answer and state.todos
    assert len({item.card_key for item in state.feed}) == len(state.feed)
    for item in state.feed:
        if item.call_key:
            assert item.tool_state != "requested", "正式调用必须有完成、暂停或拦截状态"
            assert item.detail and len(item.detail) < 4000
    if "research_agent" in expected_agents:
        assert any(e.event_type == "tool_call_requested" and e.data.get("tool_name") == "web_search" for e in collector.events)
    if artifact:
        assert (factory.data_dir / "workspaces" / session.session_id / artifact).is_file()
