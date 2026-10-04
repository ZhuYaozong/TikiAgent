"""审批、恢复、核对和退出 Modal；只采集输入，不保存业务事实。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal
import json

from pydantic import BaseModel, ConfigDict, Field
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, Input, Label, Static

from tikiagent.application.models import ApprovalDetails


class ModalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ApprovalPrompt(ModalModel):
    request_id: str
    session_id: str
    expected_revision: int = Field(ge=1)
    tool_name: str | None = None
    tool_call_id: str | None = None
    checkpoint_id: str | None = None
    details: ApprovalDetails | None = None

    @property
    def can_approve(self) -> bool:
        """缺少实际内容或作用域不匹配时，显示层也不能发起批准。"""
        details = self.details
        if details is None or not self.checkpoint_id:
            return False
        if (
            details.request_id != self.request_id
            or details.session_id != self.session_id
            or details.tool_call_id != self.tool_call_id
            or details.tool_name != self.tool_name
        ):
            return False
        if details.tool_name == "run_command":
            argv = details.arguments.get("command")
            return isinstance(argv, list) and bool(argv) and all(isinstance(arg, str) for arg in argv)
        return True


class RecoverySubmission(ModalModel):
    action: Literal["confirmed_not_executed", "confirmed_executed"]
    decided_by: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ReconcileDraft(ModalModel):
    result_file: Path


class ApprovalModal(ModalScreen[bool | None]):
    def __init__(self, prompt: ApprovalPrompt) -> None:
        super().__init__()
        self.prompt = prompt
        self._submitted = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog approval-dialog"):
            yield Label("需要人工批准", classes="dialog-title")
            # 详情可滚动，按钮固定在底部；长 argv 不省略审批所需信息。
            with VerticalScroll(id="approval-content"):
                details = self.prompt.details
                if not self.prompt.can_approve or details is None:
                    yield Static(
                        "审批详情缺失或与当前请求不匹配，暂不能批准。\n"
                        "请选择稍后，并使用 /status 重新读取审批信息。",
                        id="approval-missing", classes="danger", markup=False,
                    )
                else:
                    yield Static(
                        f"工具：{details.tool_name}\n"
                        f"审批原因：{details.reason}\n"
                        f"Workspace：{details.workspace_id}",
                        id="approval-overview", markup=False,
                    )
                    if details.tool_name == "run_command":
                        args = details.arguments
                        yield Static(
                            f"工作目录：Workspace 下的 {args.get('cwd', '.')}\n"
                            f"超时：{args.get('timeout_seconds', '-')} 秒\n"
                            "命令参数（argv，逐项传递，不经过 Shell）：\n"
                            + json.dumps(args["command"], ensure_ascii=False, indent=2),
                            id="approval-command", markup=False,
                        )
                    yield Static(
                        "完整参数（敏感值已遮蔽）：\n"
                        + json.dumps(details.arguments, ensure_ascii=False, indent=2),
                        id="approval-arguments", markup=False,
                    )
                with Collapsible(title="技术详情", collapsed=True, id="approval-technical"):
                    yield Static("\n".join([
                        f"ToolCall：{self.prompt.tool_call_id or '-'}",
                        f"Checkpoint：{self.prompt.checkpoint_id or '-'}",
                        f"Revision：{self.prompt.expected_revision}",
                        f"Session：{self.prompt.session_id}",
                        f"Task：{details.task_id if details else '-'}",
                        f"审批规则：{details.rule_id if details else '-'}",
                    ]), markup=False)
            with Horizontal(classes="dialog-actions"):
                yield Button("稍后", id="approval-later")
                yield Button("拒绝", id="approval-deny", variant="error")
                yield Button("批准本次", id="approval-approve", variant="success",
                             disabled=not self.prompt.can_approve)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "approval-later":
            self.dismiss(None)
        elif event.button.id == "approval-approve":
            self.submit_once(True)
        elif event.button.id == "approval-deny":
            self.submit_once(False)

    def submit_once(self, approved: bool) -> None:
        if self._submitted:
            return
        if approved and not self.prompt.can_approve:
            return
        self._submitted = True
        for button in self.query(Button):
            button.disabled = True
        self.dismiss(approved)


class RecoveryModal(ModalScreen[RecoverySubmission | None]):
    def __init__(self) -> None:
        super().__init__()
        self._submitted = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Manual Recovery Decision", classes="dialog-title")
            yield Static("系统不能猜测副作用是否发生。请填写人工判断依据。")
            yield Input(value="tui-user", placeholder="decided_by", id="recovery-by")
            yield Input(placeholder="判断依据（必填）", id="recovery-reason")
            with Horizontal(classes="dialog-actions"):
                yield Button("稍后", id="recovery-later")
                yield Button("确认未执行", id="recovery-not-executed", variant="success")
                yield Button("确认已执行", id="recovery-executed", variant="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "recovery-later":
            self.dismiss(None)
            return
        action = "confirmed_not_executed" if event.button.id == "recovery-not-executed" else "confirmed_executed"
        decided_by = self.query_one("#recovery-by", Input).value.strip()
        reason = self.query_one("#recovery-reason", Input).value.strip()
        if self._submitted or not decided_by or not reason:
            self.notify("decided_by 和判断依据不能为空", severity="error")
            return
        self._submitted = True
        for button in self.query(Button):
            button.disabled = True
        self.dismiss(RecoverySubmission(action=action, decided_by=decided_by, reason=reason))


class ReconcileModal(ModalScreen[ReconcileDraft | None]):
    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Submit Real Reconcile Result", classes="dialog-title")
            yield Static("confirmed_executed 不能伪造 ToolResult；请选择人工核对后的 JSON 文件。")
            yield Input(placeholder="ReconcileSubmission JSON 文件路径", id="reconcile-file")
            with Horizontal(classes="dialog-actions"):
                yield Button("稍后", id="reconcile-later")
                yield Button("提交核对结果", id="reconcile-submit", variant="success")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "reconcile-later":
            self.dismiss(None)
            return
        value = self.query_one("#reconcile-file", Input).value.strip()
        if not value:
            self.notify("必须提供真实核对结果文件", severity="error")
            return
        self.dismiss(ReconcileDraft(result_file=Path(value)))


class QuitWarningModal(ModalScreen[bool]):
    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Workflow 仍在执行", classes="dialog-title danger")
            yield Static("Ctrl+Q 只关闭 UI，不等于取消 Workflow。\n执行窗口退出后可能需要 recovery。")
            with Horizontal(classes="dialog-actions"):
                yield Button("留在界面", id="quit-stay", variant="primary")
                yield Button("仍然退出", id="quit-confirm", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "quit-confirm")
