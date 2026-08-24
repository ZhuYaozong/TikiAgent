"""审批、恢复、核对和退出 Modal；只采集输入，不保存业务事实。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static


class ModalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ApprovalPrompt(ModalModel):
    request_id: str
    session_id: str
    expected_revision: int = Field(ge=1)
    tool_name: str | None = None
    tool_call_id: str | None = None
    checkpoint_id: str | None = None


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
        with Vertical(classes="dialog"):
            yield Label("Human Approval Required", classes="dialog-title")
            yield Static("\n".join([
                f"Tool:       {self.prompt.tool_name or '-'}",
                f"ToolCall:   {self.prompt.tool_call_id or '-'}",
                f"Checkpoint: {self.prompt.checkpoint_id or '-'}",
                f"Revision:   {self.prompt.expected_revision}",
                "", "决定将交给 Controller.resume() 并由权威 Checkpoint 校验。",
            ]))
            with Horizontal(classes="dialog-actions"):
                yield Button("稍后", id="approval-later")
                yield Button("拒绝", id="approval-deny", variant="error")
                yield Button("批准", id="approval-approve", variant="success")

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
