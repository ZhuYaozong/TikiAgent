"""TikiAgent Execution Harness。"""

from tikiagent.harness.approval import (
    ApprovalGate,
    InMemoryApprovalLedger,
    approval_fingerprint,
)
from tikiagent.harness.command_tools import register_command_tool
from tikiagent.harness.dispatcher import Dispatcher
from tikiagent.harness.execution import ExecutionHarness
from tikiagent.harness.guards import ToolExposureGuard
from tikiagent.harness.file_tools import (
    build_file_registry,
    build_read_only_file_registry,
)
from tikiagent.harness.models import (
    ApprovalDecision,
    ApprovalRequest,
    CommandResult,
    ExecutionContext,
    ExecutionScope,
    HarnessOutcome,
    PermissionDecision,
    ToolCall,
    ToolError,
    ToolExecutionError,
    ToolResult,
    ValidatedToolCall,
)
from tikiagent.harness.permission import (
    PermissionPolicy,
    RuleBasedPermissionPolicy,
)
from tikiagent.harness.registry import RegisteredTool, ToolRegistry
from tikiagent.harness.workspace import Workspace
from tikiagent.harness.web_tools import (
    SearchSettings,
    TavilyProvider,
    build_web_registry,
)

__all__ = [
    "ApprovalDecision",
    "ApprovalGate",
    "ApprovalRequest",
    "CommandResult",
    "Dispatcher",
    "ExecutionContext",
    "ExecutionHarness",
    "ExecutionScope",
    "HarnessOutcome",
    "InMemoryApprovalLedger",
    "PermissionDecision",
    "PermissionPolicy",
    "RegisteredTool",
    "RuleBasedPermissionPolicy",
    "SearchSettings",
    "TavilyProvider",
    "ToolCall",
    "ToolError",
    "ToolExecutionError",
    "ToolExposureGuard",
    "ToolRegistry",
    "ToolResult",
    "ValidatedToolCall",
    "Workspace",
    "build_file_registry",
    "build_read_only_file_registry",
    "build_web_registry",
    "approval_fingerprint",
    "register_command_tool",
]
