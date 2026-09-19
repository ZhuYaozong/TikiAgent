"""工具调用验证与分发。"""

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from tikiagent.tools.models import (
    ToolCall,
    ToolError,
    ToolExecutionError,
    ToolResult,
    ValidatedToolCall,
)
from tikiagent.tools.registry import ToolRegistry


class Dispatcher:
    """验证参数并调用 handler；不负责 Exposure、Permission 或 Approval。"""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def prepare(
        self,
        raw_tool_call: Mapping[str, Any] | ToolCall,
    ) -> ValidatedToolCall | ToolResult:
        """完成 Registry Lookup、参数校验和 canonicalization，不执行工具。"""

        if isinstance(raw_tool_call, ToolCall):
            tool_call = raw_tool_call
        else:
            try:
                tool_call = ToolCall.model_validate(raw_tool_call)
            except ValidationError as error:
                return ToolResult(
                    tool_call_id=str(
                        raw_tool_call.get("tool_call_id", "unknown_call")
                    ),
                    tool_name=str(raw_tool_call.get("name", "unknown_tool")),
                    ok=False,
                    error=ToolError(
                        code="invalid_tool_call",
                        message="ToolCall 结构不合法",
                        details=error.errors(include_url=False),
                    ),
                )

        registered_tool = self.registry.get(tool_call.name)
        if registered_tool is None:
            return ToolResult(
                tool_call_id=tool_call.tool_call_id,
                tool_name=tool_call.name,
                ok=False,
                error=ToolError(
                    code="unknown_tool",
                    message=f"工具不存在：{tool_call.name}",
                ),
            )

        try:
            arguments = registered_tool.args_model.model_validate(
                tool_call.arguments
            )
        except ValidationError as error:
            return ToolResult(
                tool_call_id=tool_call.tool_call_id,
                tool_name=tool_call.name,
                ok=False,
                error=ToolError(
                    code="invalid_arguments",
                    message="工具参数校验失败",
                    details=error.errors(include_url=False),
                ),
            )

        return ValidatedToolCall(
            tool_call_id=tool_call.tool_call_id,
            name=tool_call.name,
            arguments=arguments.model_dump(mode="json"),
        )

    def execute(self, tool_call: ValidatedToolCall) -> ToolResult:
        """执行已经校验的调用；机械边界由具体 handler 强制。"""

        registered_tool = self.registry.get(tool_call.name)
        if registered_tool is None:
            return ToolResult(
                tool_call_id=tool_call.tool_call_id,
                tool_name=tool_call.name,
                ok=False,
                error=ToolError(
                    code="unknown_tool",
                    message=f"工具不存在：{tool_call.name}",
                ),
            )
        try:
            output = registered_tool.handler(**tool_call.arguments)
        except ToolExecutionError as error:
            return ToolResult(
                tool_call_id=tool_call.tool_call_id,
                tool_name=tool_call.name,
                ok=False,
                error=ToolError(
                    code=error.code,
                    message=str(error),
                    details=error.details,
                ),
            )
        except Exception as error:  # pragma: no cover - 最后一道运行时防线
            return ToolResult(
                tool_call_id=tool_call.tool_call_id,
                tool_name=tool_call.name,
                ok=False,
                error=ToolError(
                    code="tool_exception",
                    message="工具抛出未处理异常",
                    details={
                        "exception_type": type(error).__name__,
                        "exception_message": str(error),
                    },
                ),
            )

        return ToolResult(
            tool_call_id=tool_call.tool_call_id,
            tool_name=tool_call.name,
            ok=True,
            output=output,
        )

    def dispatch(self, raw_tool_call: Mapping[str, Any]) -> ToolResult:
        """Legacy/internal 兼容入口，新 Agent 最终不得用它绕过 ExecutionHarness。"""

        prepared = self.prepare(raw_tool_call)
        if isinstance(prepared, ToolResult):
            return prepared
        return self.execute(prepared)
