"""TikiAgent v0.8 Textual TUI 正式入口。"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4

from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Collapsible, Footer, Input, Markdown, Static, TabbedContent, TabPane, Tree

from tikiagent.application.events import EventBus
from tikiagent.tui.adapter import TuiEventAdapter
from tikiagent.tui.backend import TuiBackend, build_backend
from tikiagent.tui.commands import TuiCommand, parse_command
from tikiagent.tui.messages import OperationFailed, OperationFinished, TuiEventReceived, WorkspaceSnapshotReceived
from tikiagent.tui.modals import ApprovalModal, ApprovalPrompt, QuitWarningModal, ReconcileDraft, ReconcileModal, RecoveryModal, RecoverySubmission
from tikiagent.tui.models import FeedItem, TranscriptItem, TuiViewState
from tikiagent.tui.sink import TextualEventSink
from tikiagent.tui.workspace import ReadOnlyWorkspaceSnapshotter


BackendFactory = Callable[[EventBus], TuiBackend]


@dataclass(frozen=True, slots=True)
class OperationRequest:
    operation_id: str
    operation: str
    payload: dict[str, Any]


class TikiTuiApp(App[None]):
    """所有 Widget 更新都发生在 Textual 主线程。"""

    TITLE = "TikiAgent"
    SUB_TITLE = "Multi-Agent Task Execution System · v0.9.1"
    CSS_PATH = "styles.tcss"
    BINDINGS = [
        ("ctrl+q", "quit", "退出"),
        ("ctrl+n", "new_session", "新会话"),
        ("ctrl+b", "toggle_sidebar", "侧栏"),
        ("a", "show_approval", "审批"),
        ("r", "show_recovery", "恢复"),
        ("f5", "refresh_workspace", "刷新目录"),
    ]

    def __init__(
        self,
        *,
        data_dir: Path,
        env_file: Path,
        workspace_id: str = "default-workspace",
        session_id: str | None = None,
        backend_factory: BackendFactory | None = None,
    ) -> None:
        super().__init__()
        self.data_dir = data_dir.resolve()
        self.env_file = env_file
        self.default_workspace_id = workspace_id
        self.initial_session_id = session_id
        self.view_state = TuiViewState()
        self.adapter = TuiEventAdapter()
        self.main_thread_id: int | None = None
        self.operation_in_flight: str | None = None
        self.operation_name: str | None = None
        self.visible_approval: ApprovalPrompt | None = None
        self.quit_warning_count = 0
        self.resume_submission_count = 0
        self._rendered_stream_id: str | None = None
        self._rendered_transcript: tuple[TranscriptItem, ...] = ()
        self._rendered_feed_count = 0
        self.event_bus = EventBus()
        self.event_bus.subscribe(TextualEventSink(self.post_message))
        factory = backend_factory or (
            lambda bus: build_backend(self.data_dir, self.env_file, bus)
        )
        self.backend = factory(self.event_bus)
        self.workspace = ReadOnlyWorkspaceSnapshotter(self.data_dir)

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with Horizontal(id="top-bar"):
                yield Static("◆ TikiAgent", id="brand")
                yield Static("starting", id="top-status")
            with Horizontal(id="body"):
                with Vertical(id="main-panel"):
                    yield VerticalScroll(id="feed")
                with TabbedContent(id="side-panel"):
                    with TabPane("Status", id="status-tab"):
                        yield Static(id="session-card", classes="status-card")
                        yield Static(id="workflow-card", classes="status-card")
                        yield Static(id="runtime-card", classes="status-card")
                    with TabPane("Workspace", id="workspace-tab"):
                        yield Static("只读视图 · F5 刷新", classes="muted")
                        yield Tree("Workspace", id="workspace-tree")
                    with TabPane("Help", id="help-tab"):
                        yield Static(self._help_text(), id="help-card")
            with Horizontal(id="input-row"):
                yield Static("❯", id="prompt-mark")
                yield Input(placeholder="正在初始化 Session…", id="prompt", disabled=True)
                yield Static("Enter 发送 · /help 命令", id="input-hint")
        yield Footer()

    def on_mount(self) -> None:
        self.main_thread_id = threading.get_ident()
        self.render_view_state()
        if self.initial_session_id:
            self._start_operation("attach", {"session_id": self.initial_session_id}, reset=True)
        else:
            self._start_operation("new", {"workspace_id": self.default_workspace_id}, reset=True)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if not value:
            return
        event.input.value = ""
        try:
            command = parse_command(value)
        except ValueError as error:
            self._show_error(str(error))
            return
        if command is not None:
            self._execute_command(command)
            return
        if self.view_state.session_id is None:
            self._show_error("请先创建或连接 Session")
            return
        if self.view_state.status in {"awaiting_approval", "recovery_required", "awaiting_reconcile"}:
            self._show_error("当前 Workflow 已暂停，请先处理审批或恢复")
            return
        self._start_operation("submit", {"session_id": self.view_state.session_id, "user_input": value})

    def _execute_command(self, command: TuiCommand) -> None:
        if command.name == "new":
            self._start_operation("new", {"workspace_id": command.argument or self.default_workspace_id}, reset=True)
        elif command.name == "session":
            assert command.argument is not None
            self._start_operation("attach", {"session_id": command.argument}, reset=True)
        elif command.name == "status":
            if self._require_session():
                self._start_operation("status", {"session_id": self.view_state.session_id})
        elif command.name == "approval":
            self.action_show_approval()
        elif command.name == "recovery":
            self.action_show_recovery()
        elif command.name == "workspace":
            self.action_refresh_workspace()
        elif command.name == "help":
            self.notify(self._help_text(), title="TikiAgent Commands", timeout=8)
        elif command.name == "quit":
            self.action_quit()

    def _start_operation(self, operation: str, payload: dict[str, Any], *, reset: bool = False) -> None:
        if self.operation_in_flight is not None:
            self._show_error(f"{self.operation_name} 仍在运行，不能并发提交")
            return
        if reset:
            self.view_state = TuiViewState(status="starting", busy=True)
            self.visible_approval = None
            self.render_view_state()
        request = OperationRequest(str(uuid4()), operation, payload)
        self.operation_in_flight = request.operation_id
        self.operation_name = operation
        prompt = self.query_one("#prompt", Input)
        prompt.disabled = True
        prompt.placeholder = f"{operation} 正在后台执行…"
        self.run_controller(request)

    @work(thread=True, group="controller")
    def run_controller(self, request: OperationRequest) -> None:
        """Worker 禁止访问 Widget；只调用 Backend 并投递 Message。"""

        thread_id = threading.get_ident()
        try:
            outcome = self._dispatch_backend(request)
            snapshot = self.backend.session_snapshot(outcome.session_id)
        except Exception as error:  # noqa: BLE001 - 统一回主线程显示
            self.post_message(OperationFailed(request.operation_id, request.operation, error,
                                              producer_thread_id=thread_id))
            return
        self.post_message(OperationFinished(request.operation_id, request.operation, outcome, snapshot,
                                            producer_thread_id=thread_id))

    def _dispatch_backend(self, request: OperationRequest):
        p, operation = request.payload, request.operation
        if operation == "new":
            return self.backend.new_session(str(p["workspace_id"]))
        if operation == "attach" or operation == "status":
            return self.backend.status(str(p["session_id"]))
        if operation == "submit":
            return self.backend.submit(str(p["session_id"]), str(p["user_input"]))
        if operation == "resume":
            return self.backend.resume(str(p["session_id"]), int(p["expected_revision"]),
                                       str(p["request_id"]), bool(p["approved"]))
        if operation == "recover":
            return self.backend.recover(str(p["session_id"]), int(p["expected_revision"]),
                                        str(p["execution_id"]), str(p["action"]),
                                        str(p["decided_by"]), str(p["reason"]))
        if operation == "reconcile":
            return self.backend.reconcile(str(p["session_id"]), int(p["expected_revision"]),
                                          str(p["execution_id"]), Path(p["result_file"]))
        raise ValueError(f"未知 TUI operation：{operation}")

    def on_tui_event_received(self, message: TuiEventReceived) -> None:
        self._assert_main_thread()
        self.view_state = self.adapter.reduce(self.view_state, message.event)
        self.render_view_state()

    def on_operation_finished(self, message: OperationFinished) -> None:
        self._assert_main_thread()
        if message.operation_id != self.operation_in_flight:
            return
        self.operation_in_flight = self.operation_name = None
        # 新建/连接时加载持久 transcript；当前进程的新 Turn 已有实时事件，避免重复显示。
        if message.operation in {"new", "attach"} or self.view_state.workspace_id is None:
            self.view_state = self.adapter.apply_session(self.view_state, message.session)
        self.view_state = self.adapter.apply_outcome(self.view_state, message.outcome)
        self.render_view_state()
        self._enable_prompt()
        self.refresh_workspace()
        self._route_outcome_modal(message.outcome.status)

    def on_operation_failed(self, message: OperationFailed) -> None:
        self._assert_main_thread()
        if message.operation_id != self.operation_in_flight:
            return
        self.operation_in_flight = self.operation_name = None
        self.view_state = self.adapter.with_error(
            self.view_state, f"{message.operation}: {message.error_type}: {message.error_message}"
        )
        self.render_view_state()
        self._enable_prompt()

    def refresh_workspace(self) -> None:
        if self.view_state.session_id is not None:
            self.scan_workspace(self.view_state.session_id)

    @work(thread=True, group="workspace", exclusive=True)
    def scan_workspace(self, session_id: str) -> None:
        entries = self.workspace.scan(session_id)
        self.post_message(WorkspaceSnapshotReceived(session_id, entries,
                                                    producer_thread_id=threading.get_ident()))

    def on_workspace_snapshot_received(self, message: WorkspaceSnapshotReceived) -> None:
        self._assert_main_thread()
        if message.session_id != self.view_state.session_id:
            return
        self.view_state = self.adapter.apply_workspace(self.view_state, message.entries)
        self._render_workspace()

    def _route_outcome_modal(self, status: str) -> None:
        if status == "awaiting_approval":
            state = self.view_state
            if not state.approval_request_id or not state.checkpoint_revision:
                self._show_error("审批结果缺少 request_id 或 checkpoint revision")
                return
            self.visible_approval = ApprovalPrompt(
                request_id=state.approval_request_id, session_id=state.session_id or "",
                expected_revision=state.checkpoint_revision, tool_name=state.tool_name,
                tool_call_id=state.tool_call_id, checkpoint_id=state.checkpoint_id,
            )
            self.action_show_approval()
        elif status == "recovery_required":
            self.action_show_recovery()
        elif status == "awaiting_reconcile":
            self._show_reconcile()
        else:
            self.visible_approval = None

    def action_show_approval(self) -> None:
        if self.visible_approval is None or self.operation_in_flight is not None:
            self._show_error("当前没有可处理的 Approval")
            return
        if not isinstance(self.screen, ApprovalModal):
            self.push_screen(ApprovalModal(self.visible_approval), self._approval_decided)

    def _approval_decided(self, approved: bool | None) -> None:
        if approved is None or self.visible_approval is None or self.operation_in_flight:
            return
        prompt = self.visible_approval
        self.resume_submission_count += 1
        self._start_operation("resume", {
            "session_id": prompt.session_id, "expected_revision": prompt.expected_revision,
            "request_id": prompt.request_id, "approved": approved,
        })

    def action_show_recovery(self) -> None:
        state = self.view_state
        if state.status != "recovery_required" or self.operation_in_flight:
            self._show_error("当前没有可处理的 Recovery")
            return
        if not isinstance(self.screen, RecoveryModal):
            self.push_screen(RecoveryModal(), self._recovery_decided)

    def _recovery_decided(self, decision: RecoverySubmission | None) -> None:
        state = self.view_state
        if decision is None or self.operation_in_flight:
            return
        if not state.session_id or not state.checkpoint_revision or not state.execution_id:
            self._show_error("Recovery 缺少权威 scope/revision/execution_id")
            return
        self._start_operation("recover", {
            "session_id": state.session_id, "expected_revision": state.checkpoint_revision,
            "execution_id": state.execution_id, "action": decision.action,
            "decided_by": decision.decided_by, "reason": decision.reason,
        })

    def _show_reconcile(self) -> None:
        if self.operation_in_flight is None and not isinstance(self.screen, ReconcileModal):
            self.push_screen(ReconcileModal(), self._reconcile_submitted)

    def _reconcile_submitted(self, draft: ReconcileDraft | None) -> None:
        state = self.view_state
        if draft is None or self.operation_in_flight:
            return
        if not state.session_id or not state.checkpoint_revision or not state.execution_id:
            self._show_error("Reconcile 缺少权威 scope/revision/execution_id")
            return
        self._start_operation("reconcile", {
            "session_id": state.session_id, "expected_revision": state.checkpoint_revision,
            "execution_id": state.execution_id, "result_file": draft.result_file,
        })

    def action_new_session(self) -> None:
        self._start_operation("new", {"workspace_id": self.default_workspace_id}, reset=True)

    def action_refresh_workspace(self) -> None:
        self.refresh_workspace()

    def action_toggle_sidebar(self) -> None:
        side = self.query_one("#side-panel", TabbedContent)
        side.display = not side.display

    def action_quit(self) -> None:
        active = self.operation_in_flight is not None or self.view_state.busy or self.view_state.status in {"running", "executing"}
        if not active:
            self.exit()
            return
        if not isinstance(self.screen, QuitWarningModal):
            self.quit_warning_count += 1
            self.push_screen(QuitWarningModal(), self._quit_decided)

    def _quit_decided(self, should_quit: bool) -> None:
        if should_quit:
            # 只关闭 Textual App；不调用 Controller/Harness 的取消或恢复入口。
            self.exit()

    def render_view_state(self) -> None:
        if not self.is_mounted:
            return
        state = self.view_state
        self._sync_feed(state)
        self.query_one("#session-card", Static).update(self._session_table(state))
        self.query_one("#workflow-card", Static).update(self._workflow_table(state))
        runtime = self.query_one("#runtime-card", Static)
        runtime.update(self._runtime_table(state))
        runtime.display = any(
            (state.checkpoint_id, state.execution_id, state.tool_name, state.approvals)
        )
        notice = state.error or state.notice or state.status
        notice_line = next((line for line in notice.splitlines() if line.strip()), "ready")
        status = f"{state.current_agent if state.busy else state.status} · {notice_line}"
        self.query_one("#top-status", Static).update(_shorten(status, 110))
        self._render_workspace()

    def _sync_feed(self, state: TuiViewState) -> None:
        feed = self.query_one("#feed", VerticalScroll)
        reset = (
            state.stream_id != self._rendered_stream_id
            or state.transcript != self._rendered_transcript
            or len(state.feed) < self._rendered_feed_count
        )
        if reset:
            feed.remove_children()
            self._mount_welcome(feed)
            for item in state.transcript:
                self._mount_transcript(feed, item)
            self._rendered_stream_id = state.stream_id
            self._rendered_transcript = state.transcript
            self._rendered_feed_count = 0
        for item in state.feed[self._rendered_feed_count :]:
            self._mount_feed_item(feed, item)
        self._rendered_feed_count = len(state.feed)
        feed.scroll_end(animate=False)

    @staticmethod
    def _mount_welcome(feed: VerticalScroll) -> None:
        feed.mount(
            Vertical(
                Static(Text("◆ TikiAgent", style="bold #7fd6c2")),
                Static("输入问题、调研或代码任务；执行细节会保持紧凑并可展开。"),
                classes="feed-card feed-welcome",
            )
        )

    def _mount_transcript(self, feed: VerticalScroll, item: TranscriptItem) -> None:
        kind = "user" if item.role == "user" else "assistant"
        self._mount_message(feed, kind, "You" if kind == "user" else "TikiAgent", item.content)

    def _mount_feed_item(self, feed: VerticalScroll, item: FeedItem) -> None:
        if item.kind in {"user", "assistant"}:
            self._mount_message(
                feed,
                item.kind,
                item.title,
                item.detail or item.summary,
            )
            return
        marker = {
            "routing": "◇",
            "agent": "●",
            "tool": "└",
            "approval": "!",
            "verification": "✓",
            "error": "×",
            "system": "·",
        }[item.kind]
        title = f"{marker} {item.title}"
        summary = Static(Text(item.summary, style=_feed_color(item.kind)), classes="feed-summary")
        if item.detail:
            card = Collapsible(
                summary,
                Static(Text(item.detail), classes="feed-detail"),
                title=title,
                collapsed=item.collapsed,
                classes=f"feed-card feed-{item.kind}",
            )
        else:
            card = Vertical(
                Static(Text(title, style=f"bold {_feed_color(item.kind)}")),
                summary,
                classes=f"feed-card feed-{item.kind}",
            )
        feed.mount(card)

    @staticmethod
    def _mount_message(
        feed: VerticalScroll,
        kind: str,
        title: str,
        content: str,
    ) -> None:
        body = Markdown(content) if kind == "assistant" else Static(Text(content))
        feed.mount(
            Vertical(
                Static(Text(title, style=f"bold {_feed_color(kind)}")),
                body,
                classes=f"feed-card feed-{kind}",
            )
        )

    @staticmethod
    def _session_table(state: TuiViewState) -> Table:
        table = Table.grid(padding=(0, 1))
        table.add_column(style="bold #7fd6c2", no_wrap=True)
        table.add_column()
        table.add_row("session", _short_id(state.session_id))
        table.add_row("workspace", state.workspace_id or "-")
        table.add_row("task", _short_id(state.task_id))
        return table

    @staticmethod
    def _workflow_table(state: TuiViewState) -> Table:
        table = Table.grid(padding=(0, 1))
        table.add_column(style="bold #f4bf75", no_wrap=True)
        table.add_column()
        table.add_row("status", state.status)
        table.add_row("agent", state.current_agent)
        table.add_row("verify", state.verification)
        table.add_row("tools", str(state.tool_calls))
        return table

    @staticmethod
    def _runtime_table(state: TuiViewState) -> Table:
        table = Table.grid(padding=(0, 1))
        table.add_column(style="bold #ef9f76", no_wrap=True)
        table.add_column()
        table.add_row("checkpoint", _short_id(state.checkpoint_id))
        table.add_row("revision", str(state.checkpoint_revision or "-"))
        table.add_row("execution", _short_id(state.execution_id))
        table.add_row("tool", state.tool_name or "-")
        table.add_row("approvals", str(state.approvals))
        return table

    def _render_workspace(self) -> None:
        if not self.is_mounted:
            return
        tree = self.query_one("#workspace-tree", Tree)
        tree.reset(f"Workspace · {self.view_state.session_id or '-'}")
        nodes = {"": tree.root}
        for entry in self.view_state.workspace_entries:
            parent_path = str(Path(entry.path).parent).replace("\\", "/")
            if parent_path == ".":
                parent_path = ""
            parent = nodes.get(parent_path, tree.root)
            label = f"[link={entry.kind}]{entry.name}[/]"
            node = parent.add(label, expand=True) if entry.kind == "directory" else parent.add_leaf(label)
            if entry.kind == "directory":
                nodes[entry.path] = node
        tree.root.expand()

    def _enable_prompt(self) -> None:
        prompt = self.query_one("#prompt", Input)
        prompt.disabled = False
        prompt.placeholder = "输入任务或 /help 查看命令"
        prompt.focus()

    def _show_error(self, message: str) -> None:
        self.view_state = self.adapter.with_error(self.view_state, message)
        self.render_view_state()
        self.notify(message, severity="error")

    def _require_session(self) -> bool:
        if self.view_state.session_id is None:
            self._show_error("当前没有 Session")
            return False
        return True

    def _assert_main_thread(self) -> None:
        if self.main_thread_id is None or threading.get_ident() != self.main_thread_id:
            raise RuntimeError("TUI Message Handler 必须运行在主线程")

    @staticmethod
    def _help_text() -> str:
        return """/new [workspace]  新建 Session
/session <id>    连接已有 Session
/status          读取权威 Checkpoint 状态
/approval        重新打开审批窗口
/recovery        打开人工恢复窗口
/workspace       刷新只读 Workspace Tree
Ctrl+B           显示或隐藏侧栏
/help            显示帮助
/quit            关闭 TUI（不取消 Workflow）"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TikiAgent Textual TUI")
    parser.add_argument("--data-dir", type=Path, default=Path(".tiki"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--workspace-id", default="default-workspace")
    parser.add_argument("--session-id")
    return parser


def _short_id(value: str | None, limit: int = 12) -> str:
    if not value:
        return "-"
    return value if len(value) <= limit else value[:limit] + "…"


def _shorten(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _feed_color(kind: str) -> str:
    return {
        "user": "#f4bf75",
        "assistant": "#7fd6c2",
        "routing": "#b9a7e6",
        "agent": "#7fd6c2",
        "tool": "#f4bf75",
        "approval": "#ef9f76",
        "verification": "#7fd68a",
        "error": "#ef6f6c",
        "system": "#9aa4a6",
    }.get(kind, "#d7d1c9")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    TikiTuiApp(data_dir=args.data_dir, env_file=args.env_file,
               workspace_id=args.workspace_id, session_id=args.session_id).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
