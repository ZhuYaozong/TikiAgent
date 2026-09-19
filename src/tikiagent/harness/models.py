"""Harness 执行状态与互斥结果。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tikiagent.harness.permissions.models import ApprovalRequest
from tikiagent.harness.scope import ExecutionContext, ExecutionScope
from tikiagent.tools.models import ToolResult


HarnessStatus = Literal["completed", "awaiting_approval", "denied"]


class HarnessOutcome(BaseModel):
    """Harness 的互斥结果；status 是唯一状态来源。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    status: HarnessStatus
    tool_result: ToolResult | None = None
    approval_request: ApprovalRequest | None = None

    @model_validator(mode="after")
    def validate_status_payload(self) -> "HarnessOutcome":
        if self.status == "completed":
            if self.tool_result is None or self.approval_request is not None:
                raise ValueError("completed 必须且只能包含 tool_result")
        elif self.status == "awaiting_approval":
            if self.tool_result is not None or self.approval_request is None:
                raise ValueError(
                    "awaiting_approval 必须且只能包含 approval_request"
                )
        else:
            if (
                self.tool_result is None
                or self.tool_result.ok
                or self.tool_result.error is None
                or self.approval_request is not None
            ):
                raise ValueError("denied 必须且只能包含失败 tool_result")
        return self
