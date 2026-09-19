"""权限决定与作用域绑定的审批协议。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tikiagent.harness.scope import ExecutionScope
from tikiagent.tools.models import ValidatedToolCall


PermissionAction = Literal["ALLOW", "ASK", "DENY"]


class PermissionDecision(BaseModel):
    """确定性 Permission Policy 的结构化决定。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    action: PermissionAction
    rule_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ApprovalRequest(BaseModel):
    """ASK 的暂停产物；它不是失败 ToolResult。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    request_id: str = Field(min_length=1)
    status: Literal["awaiting_approval"] = "awaiting_approval"
    scope: ExecutionScope
    tool_call: ValidatedToolCall
    fingerprint: str = Field(min_length=1)
    permission: PermissionDecision

    @model_validator(mode="after")
    def validate_permission_action(self) -> "ApprovalRequest":
        if self.permission.action != "ASK":
            raise ValueError("只有 ASK Permission 才能创建 ApprovalRequest")
        return self


class ApprovalDecision(BaseModel):
    """外部批准或拒绝某个精确 ApprovalRequest。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    request_id: str = Field(min_length=1)
    approved: bool
    scope: ExecutionScope
    fingerprint: str = Field(min_length=1)
