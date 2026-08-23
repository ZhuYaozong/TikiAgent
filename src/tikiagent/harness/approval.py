"""Approval Request 的创建、绑定与一次性消费。"""

import hashlib
import json
from uuid import uuid4

from tikiagent.harness.models import (
    ApprovalDecision,
    ApprovalRequest,
    ExecutionContext,
    PermissionDecision,
    ToolError,
    ValidatedToolCall,
)


class InMemoryApprovalLedger:
    """v0.6a1 单进程 Ledger；v0.6a2 将消费事实交给 Checkpoint。"""

    def __init__(self) -> None:
        self.requests: dict[str, ApprovalRequest] = {}
        self.resolved: set[str] = set()


class ApprovalGate:
    """批准只对一个精确 ToolCall 和 task/session/workspace scope 有效。"""

    def __init__(self, ledger: InMemoryApprovalLedger | None = None) -> None:
        self.ledger = ledger or InMemoryApprovalLedger()

    def create_request(
        self,
        *,
        tool_call: ValidatedToolCall,
        context: ExecutionContext,
        permission: PermissionDecision,
    ) -> ApprovalRequest:
        request = ApprovalRequest(
            request_id=str(uuid4()),
            scope=context.scope,
            tool_call=tool_call,
            fingerprint=approval_fingerprint(tool_call, context),
            permission=permission,
        )
        self.ledger.requests[request.request_id] = request
        return request

    def authorize(
        self,
        *,
        request: ApprovalRequest,
        decision: ApprovalDecision,
        current_call: ValidatedToolCall,
        context: ExecutionContext,
    ) -> ToolError | None:
        error = self.validate_decision(
            request=request,
            decision=decision,
            current_call=current_call,
            context=context,
        )
        if error is not None:
            return error
        self.mark_resolved(request.request_id)
        if not decision.approved:
            return ToolError(
                code="approval_rejected",
                message="外部审批拒绝了工具执行",
            )
        return None

    def validate_decision(
        self,
        *,
        request: ApprovalRequest,
        decision: ApprovalDecision,
        current_call: ValidatedToolCall,
        context: ExecutionContext,
    ) -> ToolError | None:
        """只验证审批绑定，不提前消费执行事实。"""

        stored = self.ledger.requests.get(request.request_id)
        if stored is None or stored != request:
            return ToolError(
                code="approval_request_unknown",
                message="ApprovalRequest 不存在或内容不匹配",
            )
        if request.request_id in self.ledger.resolved:
            return ToolError(
                code="approval_already_resolved",
                message="ApprovalRequest 已消费，不能重复执行",
            )
        if decision.request_id != request.request_id:
            return ToolError(
                code="approval_request_mismatch",
                message="ApprovalDecision 没有绑定当前请求",
            )
        if decision.scope != request.scope or context.scope != request.scope:
            return ToolError(
                code="approval_scope_mismatch",
                message="task/session/workspace scope 与批准请求不匹配",
                details={
                    "request_scope": request.scope.model_dump(mode="json"),
                    "decision_scope": decision.scope.model_dump(mode="json"),
                    "current_scope": context.scope.model_dump(mode="json"),
                },
            )
        current_fingerprint = approval_fingerprint(current_call, context)
        if (
            decision.fingerprint != request.fingerprint
            or current_fingerprint != request.fingerprint
        ):
            return ToolError(
                code="approval_fingerprint_mismatch",
                message="工具名称、规范化参数或 scope 在批准后发生变化",
            )

        return None

    def mark_resolved(self, request_id: str) -> None:
        """在执行状态已经安全持久化后，才消费一次性审批。"""

        self.ledger.resolved.add(request_id)

    def restore_request(self, request: ApprovalRequest) -> None:
        """新进程从 Checkpoint 恢复尚未消费的 ApprovalRequest。"""

        existing = self.ledger.requests.get(request.request_id)
        if existing is not None and existing != request:
            raise ValueError("恢复的 ApprovalRequest 与 Ledger 现有内容冲突")
        self.ledger.requests[request.request_id] = request


def approval_fingerprint(
    tool_call: ValidatedToolCall,
    context: ExecutionContext,
) -> str:
    """对规范化参数和三个 Scope ID 生成确定性 fingerprint。"""

    payload = {
        "scope": context.scope.model_dump(mode="json"),
        "tool_call": tool_call.model_dump(mode="json"),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
