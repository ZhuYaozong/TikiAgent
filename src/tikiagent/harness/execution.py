"""Tool Exposure → Permission → Approval → Dispatcher 的安全执行管线。"""

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError

from tikiagent.harness.approval import ApprovalGate
from tikiagent.harness.dispatcher import Dispatcher
from tikiagent.harness.guards import ToolExposureGuard
from tikiagent.harness.models import (
    ApprovalDecision,
    ApprovalRequest,
    ExecutionContext,
    HarnessOutcome,
    ToolCall,
    ToolError,
    ToolResult,
    ValidatedToolCall,
)
from tikiagent.harness.permission import PermissionPolicy, RuleBasedPermissionPolicy


BeforeExecute = Callable[[ValidatedToolCall], None]


class ExecutionHarness:
    """新的正式执行边界；Dispatcher 只是底层校验与 handler 调用器。"""

    def __init__(
        self,
        dispatcher: Dispatcher,
        *,
        permission_policy: PermissionPolicy | None = None,
        approval_gate: ApprovalGate | None = None,
    ) -> None:
        self.dispatcher = dispatcher
        self.permission_policy = permission_policy or RuleBasedPermissionPolicy()
        self.approval_gate = approval_gate or ApprovalGate()

    def handle(
        self,
        raw_tool_call: Mapping[str, Any],
        *,
        context: ExecutionContext,
        approval_request: ApprovalRequest | None = None,
        approval_decision: ApprovalDecision | None = None,
        before_execute: BeforeExecute | None = None,
    ) -> HarnessOutcome:
        try:
            basic_call = ToolCall.model_validate(raw_tool_call)
        except ValidationError as error:
            return self._denied(
                tool_call_id=str(raw_tool_call.get("tool_call_id", "unknown_call")),
                tool_name=str(raw_tool_call.get("name", "unknown_tool")),
                code="invalid_tool_call",
                message="ToolCall 结构不合法",
                details=error.errors(include_url=False),
            )

        # Exposure 必须先于 Registry 参数校验和 Permission。
        if not ToolExposureGuard.allows(basic_call.name, context.exposed_tools):
            return self._denied(
                tool_call_id=basic_call.tool_call_id,
                tool_name=basic_call.name,
                code="tool_not_exposed",
                message=f"工具未向 {context.agent} 暴露：{basic_call.name}",
            )

        prepared = self.dispatcher.prepare(basic_call)
        if isinstance(prepared, ToolResult):
            return HarnessOutcome(status="denied", tool_result=prepared)

        permission = self.permission_policy.decide(prepared, context)
        if permission.action == "DENY":
            return self._denied(
                tool_call_id=prepared.tool_call_id,
                tool_name=prepared.name,
                code="permission_denied",
                message=permission.reason,
                details={"rule_id": permission.rule_id},
            )
        if permission.action == "ASK":
            if approval_request is None and approval_decision is None:
                request = self.approval_gate.create_request(
                    tool_call=prepared,
                    context=context,
                    permission=permission,
                )
                return HarnessOutcome(
                    status="awaiting_approval",
                    approval_request=request,
                )
            if approval_request is None or approval_decision is None:
                return self._denied(
                    tool_call_id=prepared.tool_call_id,
                    tool_name=prepared.name,
                    code="approval_state_incomplete",
                    message="恢复 ASK 时必须同时提供 ApprovalRequest 和 Decision",
                )
            approval_error = self.approval_gate.authorize(
                request=approval_request,
                decision=approval_decision,
                current_call=prepared,
                context=context,
            )
            if approval_error is not None:
                return HarnessOutcome(
                    status="denied",
                    tool_result=ToolResult(
                        tool_call_id=prepared.tool_call_id,
                        tool_name=prepared.name,
                        ok=False,
                        error=approval_error,
                    ),
                )
        elif approval_request is not None or approval_decision is not None:
            return self._denied(
                tool_call_id=prepared.tool_call_id,
                tool_name=prepared.name,
                code="unexpected_approval_state",
                message="ALLOW 调用不应携带 Approval 状态",
            )

        # v0.6a2 将在此先持久化 executing，再允许 handler 启动。
        if before_execute is not None:
            before_execute(prepared)
        return HarnessOutcome(
            status="completed",
            tool_result=self.dispatcher.execute(prepared),
        )

    @staticmethod
    def _denied(
        *,
        tool_call_id: str,
        tool_name: str,
        code: str,
        message: str,
        details: Any = None,
    ) -> HarnessOutcome:
        return HarnessOutcome(
            status="denied",
            tool_result=ToolResult(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                ok=False,
                error=ToolError(code=code, message=message, details=details),
            ),
        )
