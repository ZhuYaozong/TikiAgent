"""Execution Harness 的公共数据模型与异常。"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PermissionAction = Literal["ALLOW", "ASK", "DENY"]
HarnessStatus = Literal["completed", "awaiting_approval", "denied"]


class ToolCall(BaseModel):
    """模型请求执行的一次工具调用。"""

    model_config = ConfigDict(strict=True)

    tool_call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ValidatedToolCall(BaseModel):
    """经过 Registry 参数模型校验和默认值填充的规范化调用。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    tool_call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ExecutionScope(BaseModel):
    """工具执行和审批所属的任务、会话与工作区身份。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    task_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)


class ExecutionContext(BaseModel):
    """Harness 本轮执行上下文；不包含模型 Messages。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    scope: ExecutionScope
    agent: str = Field(min_length=1)
    exposed_tools: set[str] = Field(default_factory=set)


class ToolError(BaseModel):
    """可供 Agent 观察和处理的结构化工具错误。"""

    code: str
    message: str
    details: Any = None


class ToolResult(BaseModel):
    """Dispatcher 对工具成功或失败结果的统一包装。"""

    tool_call_id: str
    tool_name: str
    ok: bool
    output: Any = None
    error: ToolError | None = None


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


class CommandResult(BaseModel):
    """子进程运行结果，与 ToolResult 的协议状态相互独立。"""

    command: list[str]
    cwd: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool


class ToolExecutionError(Exception):
    """工具主动报告的、可预期的执行错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details
