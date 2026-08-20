"""Execution Harness 的公共数据模型与异常。"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ToolCall(BaseModel):
    """模型请求执行的一次工具调用。"""

    model_config = ConfigDict(strict=True)

    tool_call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


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
