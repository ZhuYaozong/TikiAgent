"""审批显示链路：权威 Checkpoint、脱敏投影与可操作的弹窗。"""

import asyncio
import json

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Button, Static

from tikiagent.application.approval_details import build_approval_details
from tikiagent.application.controller import ApplicationController
from tikiagent.application.events import EventBus
from tikiagent.application.models import EventScope
from tikiagent.application.workflow_adapter import TikiWorkflowAdapter
from tikiagent.context.memory.history import JsonlHistoryStore
from tikiagent.harness.permissions.models import ApprovalRequest, PermissionDecision
from tikiagent.harness.permissions.policy import RuleBasedPermissionPolicy
from tikiagent.harness.persistence.checkpoint import JsonCheckpointStore
from tikiagent.harness.scope import ExecutionScope
from tikiagent.interfaces.tui.adapter import TuiEventAdapter
from tikiagent.interfaces.tui.app import TikiTuiApp
from tikiagent.interfaces.tui.modals import ApprovalModal, ApprovalPrompt
from tikiagent.interfaces.tui.models import TuiViewState
from tikiagent.providers.llm.models import ModelResponse, ModelToolCall
from tikiagent.tools.commands import register_command_tool
from tikiagent.tools.models import ValidatedToolCall


def request(arguments=None):
    return ApprovalRequest(
        request_id="approval-1", scope=ExecutionScope(
            task_id="task-1", session_id="session-test", workspace_id="workspace-test",
        ),
        tool_call=ValidatedToolCall(
            tool_call_id="call-1", name="run_command",
            arguments=arguments if arguments is not None else {
                "command": ["python", "-m", "pip", "install", "rich"],
                "cwd": ".", "timeout_seconds": 30.0, "output_limit": 8000,
            },
        ),
        fingerprint="fingerprint-1", permission=PermissionDecision(
            action="ASK", rule_id="command.package-install.ask",
            reason="安装依赖会改变运行环境，需要外部批准",
        ),
    )


def prompt(details):
    return ApprovalPrompt(
        request_id="approval-1", session_id="session-test", expected_revision=1,
        tool_name="run_command", tool_call_id="call-1", checkpoint_id="checkpoint-1",
        details=details,
    )


def test_approval_display_redacts_secrets_without_mutating_arguments():
    original = request({
        "command": ["pip", "install", "rich", "--api-key", "flag-secret",
                    "--password=inline-secret", "-H", "Authorization: Bearer header-secret",
                    "https://name:url-secret@example.com/simple?token=query-secret",
                    "literal[red]", "\x1b[31m"],
        "config": {"api_key": "nested-secret"}, "cwd": ".",
    })
    before = original.model_dump_json()
    details = build_approval_details(original)
    rendered = details.model_dump_json()
    for value in ("flag-secret", "inline-secret", "header-secret", "url-secret",
                  "query-secret", "nested-secret"):
        assert value not in rendered
    assert "[REDACTED]" in rendered
    assert "literal[red]" in details.arguments["command"]
    assert "\x1b" not in details.arguments["command"][-1]
    assert original.model_dump_json() == before


def test_real_approval_checkpoint_restores_full_display_without_executing(tmp_path, monkeypatch):
    from test_multi_agent_resume import build_workflow, ScriptedModel

    argv = ["python", "-m", "pip", "install", "rich"]
    model = ScriptedModel([ModelResponse(
        assistant_message={"role": "assistant", "tool_calls": []},
        tool_calls=(ModelToolCall("install-1", "run_command", json.dumps({"command": argv})),),
    )])
    workspace, workflow = build_workflow(
        tmp_path, model, history_store=JsonlHistoryStore(tmp_path / "history.jsonl"),
    )
    coordinator = workflow.code_agent.agent.execution_coordinator
    # 只运行到 ASK；若有错误导致进入 handler，测试立即失败，绝不安装依赖。
    register_command_tool(coordinator.harness.dispatcher.registry, workspace)
    def unexpected_handler(*args, **kwargs):
        pytest.fail("未批准时不得执行命令")
    monkeypatch.setattr("tikiagent.tools.commands.subprocess.run", unexpected_handler)
    coordinator.harness.permission_policy = RuleBasedPermissionPolicy()
    paused = workflow.invoke("安装 rich", session_id="session-test", task_id="task-1")
    assert paused["status"] == "awaiting_approval"

    # 新 Adapter 只读磁盘 Checkpoint，不依赖上一个 Runtime 或事件历史。
    adapter = TikiWorkflowAdapter(
        workflow_factory=lambda *_: pytest.fail("status 不应启动 Workflow"),
        checkpoint_store=JsonCheckpointStore(tmp_path / "checkpoints"), event_bus=EventBus(),
    )
    outcome = adapter.status(
        checkpoint_id=paused["runtime_checkpoint_id"],
        scope=EventScope(session_id="session-test", workspace_id="workspace-1"),
    )
    application = ApplicationController._to_application(outcome)
    state = TuiEventAdapter().apply_outcome(TuiViewState(), application)
    assert state.approval_details.arguments == {
        "command": argv, "cwd": ".", "timeout_seconds": 30.0, "output_limit": 8000,
    }
    assert state.approval_details.reason == "安装依赖会改变运行环境，需要外部批准"
    assert state.approval_details.request_id == state.approval_request_id


@pytest.mark.parametrize("size", [(100, 35), (60, 20)])
def test_long_approval_scrolls_and_keeps_buttons_visible(tmp_path, size):
    async def exercise():
        app = TikiTuiApp(data_dir=tmp_path, env_file=tmp_path / "missing.env")
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            details = build_approval_details(request({
                "command": ["pip", "install", *[f"package-{i}[literal]" for i in range(80)]],
                "cwd": "sub directory", "timeout_seconds": 90.0,
            }))
            modal = ApprovalModal(prompt(details))
            app.push_screen(modal)
            await pilot.pause()
            content = modal.query_one("#approval-content", VerticalScroll)
            command = modal.query_one("#approval-command", Static)
            assert "package-79[literal]" in str(command.content)
            assert "sub directory" in str(command.content)
            assert "90.0" in str(command.content)
            assert "package-79[literal]" in command.visual.plain
            assert content.max_scroll_y > 0
            for button in modal.query(Button):
                assert button.region.width > 0
                assert button.region.y >= content.region.bottom
                assert button.region.bottom <= size[1]
            assert not modal.query_one("#approval-approve", Button).disabled
            modal.dismiss(None)
    asyncio.run(exercise())


@pytest.mark.parametrize("missing", [True, False])
def test_missing_or_mismatched_details_block_approval_but_allow_deny(tmp_path, missing):
    async def exercise():
        app = TikiTuiApp(data_dir=tmp_path, env_file=tmp_path / "missing.env")
        details = None if missing else build_approval_details(request()).model_copy(
            update={"request_id": "different-request"},
        )
        async with app.run_test(size=(80, 25)) as pilot:
            await app.workers.wait_for_complete()
            modal = ApprovalModal(prompt(details))
            decisions = []
            app.push_screen(modal, decisions.append)
            await pilot.pause()
            assert modal.query_one("#approval-approve", Button).disabled
            assert "暂不能批准" in str(modal.query_one("#approval-missing", Static).content)
            modal.submit_once(True)
            assert app.screen is modal and decisions == []
            modal.submit_once(False)
            await pilot.pause()
            assert decisions == [False]
    asyncio.run(exercise())


def test_attach_session_opens_approval_with_details_and_later_keeps_pending(tmp_path):
    from test_tui_app import FakeBackend

    def factory(bus):
        backend = FakeBackend(bus)
        backend.status = lambda session_id: backend._pending("awaiting_approval", revision=4)
        return backend

    async def exercise():
        app = TikiTuiApp(data_dir=tmp_path, env_file=tmp_path / "missing.env",
                         session_id="session-test", backend_factory=factory)
        async with app.run_test(size=(100, 32)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, ApprovalModal)
            assert app.screen.prompt.expected_revision == 4
            assert "rich" in str(app.screen.query_one("#approval-command", Static).content)
            app.screen.dismiss(None)
            await pilot.pause()
            assert app.view_state.status == "awaiting_approval"
            assert app.resume_submission_count == 0
            app.action_show_approval()
            await pilot.pause()
            assert isinstance(app.screen, ApprovalModal)
    asyncio.run(exercise())
