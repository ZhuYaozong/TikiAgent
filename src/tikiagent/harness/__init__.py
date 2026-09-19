"""包级兼容导出；内部使用明确模块路径。"""

from importlib import import_module

_EXPORTS = {
    "ApprovalDecision": ("tikiagent.harness.permissions.models", "ApprovalDecision"),
    "ApprovalGate": ("tikiagent.harness.permissions.approval", "ApprovalGate"),
    "ApprovalRequest": ("tikiagent.harness.permissions.models", "ApprovalRequest"),
    "CheckpointConflictError": (
        "tikiagent.harness.persistence.checkpoint",
        "CheckpointConflictError",
    ),
    "CheckpointIntegrityError": (
        "tikiagent.harness.persistence.checkpoint",
        "CheckpointIntegrityError",
    ),
    "CheckpointNotFoundError": (
        "tikiagent.harness.persistence.checkpoint",
        "CheckpointNotFoundError",
    ),
    "CommandResult": ("tikiagent.tools.models", "CommandResult"),
    "CoordinatedOutcome": ("tikiagent.harness.coordinator", "CoordinatedOutcome"),
    "Dispatcher": ("tikiagent.tools.dispatcher", "Dispatcher"),
    "ExecutionCheckpoint": (
        "tikiagent.harness.persistence.checkpoint",
        "ExecutionCheckpoint",
    ),
    "ExecutionContext": ("tikiagent.harness.models", "ExecutionContext"),
    "ExecutionCoordinator": ("tikiagent.harness.coordinator", "ExecutionCoordinator"),
    "ExecutionHarness": ("tikiagent.harness.execution", "ExecutionHarness"),
    "ExecutionIdentity": (
        "tikiagent.harness.persistence.checkpoint",
        "ExecutionIdentity",
    ),
    "ExecutionLifecycleEvent": (
        "tikiagent.harness.coordinator",
        "ExecutionLifecycleEvent",
    ),
    "ExecutionLifecycleObserver": (
        "tikiagent.harness.coordinator",
        "ExecutionLifecycleObserver",
    ),
    "ExecutionScope": ("tikiagent.harness.models", "ExecutionScope"),
    "FixedCommandPermissionPolicy": (
        "tikiagent.harness.permissions.policy",
        "FixedCommandPermissionPolicy",
    ),
    "HarnessOutcome": ("tikiagent.harness.models", "HarnessOutcome"),
    "HistoryResumeReference": (
        "tikiagent.harness.persistence.checkpoint",
        "HistoryResumeReference",
    ),
    "InMemoryApprovalLedger": (
        "tikiagent.harness.permissions.approval",
        "InMemoryApprovalLedger",
    ),
    "JsonCheckpointStore": (
        "tikiagent.harness.persistence.checkpoint",
        "JsonCheckpointStore",
    ),
    "JsonlTraceStore": ("tikiagent.harness.persistence.trace", "JsonlTraceStore"),
    "PendingModelToolCall": (
        "tikiagent.harness.persistence.checkpoint",
        "PendingModelToolCall",
    ),
    "PermissionDecision": (
        "tikiagent.harness.permissions.models",
        "PermissionDecision",
    ),
    "PermissionPolicy": ("tikiagent.harness.permissions.policy", "PermissionPolicy"),
    "ReActRunSnapshot": (
        "tikiagent.harness.persistence.checkpoint",
        "ReActRunSnapshot",
    ),
    "ReconcileResult": ("tikiagent.harness.persistence.recovery", "ReconcileResult"),
    "RecoveryDecision": ("tikiagent.harness.persistence.recovery", "RecoveryDecision"),
    "RegisteredTool": ("tikiagent.tools.registry", "RegisteredTool"),
    "RuleBasedPermissionPolicy": (
        "tikiagent.harness.permissions.policy",
        "RuleBasedPermissionPolicy",
    ),
    "SearchSettings": ("tikiagent.providers.search.config", "SearchSettings"),
    "TavilyProvider": ("tikiagent.providers.search.tavily", "TavilyProvider"),
    "ToolCall": ("tikiagent.tools.models", "ToolCall"),
    "ToolError": ("tikiagent.tools.models", "ToolError"),
    "ToolExecutionError": ("tikiagent.tools.models", "ToolExecutionError"),
    "ToolExposureGuard": ("tikiagent.harness.exposure", "ToolExposureGuard"),
    "ToolRegistry": ("tikiagent.tools.registry", "ToolRegistry"),
    "ToolResult": ("tikiagent.tools.models", "ToolResult"),
    "TraceEvent": ("tikiagent.harness.persistence.trace", "TraceEvent"),
    "ValidatedToolCall": ("tikiagent.tools.models", "ValidatedToolCall"),
    "WorkflowResumeSnapshot": (
        "tikiagent.harness.persistence.checkpoint",
        "WorkflowResumeSnapshot",
    ),
    "Workspace": ("tikiagent.harness.workspace", "Workspace"),
    "approval_fingerprint": (
        "tikiagent.harness.permissions.approval",
        "approval_fingerprint",
    ),
    "build_file_registry": ("tikiagent.tools.files", "build_file_registry"),
    "build_read_only_file_registry": (
        "tikiagent.tools.files",
        "build_read_only_file_registry",
    ),
    "build_web_registry": ("tikiagent.tools.web", "build_web_registry"),
    "register_command_tool": ("tikiagent.tools.commands", "register_command_tool"),
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    # 按需加载，避免正式启动带入基线实现。
    if name not in _EXPORTS:
        raise AttributeError(name)
    module, symbol = _EXPORTS[name]
    value = getattr(import_module(module), symbol)
    globals()[name] = value
    return value
