"""包级兼容导出；内部使用明确模块路径。"""

from importlib import import_module

_EXPORTS = {
    "BaseCompressor": ("tikiagent.context.compression.compressors", "BaseCompressor"),
    "BaseContext": ("tikiagent.context.models", "BaseContext"),
    "CallContextAssembler": ("tikiagent.context.call_context", "CallContextAssembler"),
    "CandidateModelCall": ("tikiagent.context.models", "CandidateModelCall"),
    "CharacterTokenEstimator": (
        "tikiagent.context.compression.monitor",
        "CharacterTokenEstimator",
    ),
    "CompressionPolicy": ("tikiagent.context.compression.policy", "CompressionPolicy"),
    "ContextAgentName": ("tikiagent.context.schema", "ContextAgentName"),
    "ContextBudget": ("tikiagent.context.compression.models", "ContextBudget"),
    "ContextBudgetExceeded": ("tikiagent.context.preparation", "ContextBudgetExceeded"),
    "ContextBuilder": ("tikiagent.context.builder", "ContextBuilder"),
    "ContextMonitor": ("tikiagent.context.compression.monitor", "ContextMonitor"),
    "ContextProfile": ("tikiagent.context.models", "ContextProfile"),
    "ContextRequest": ("tikiagent.context.models", "ContextRequest"),
    "ContextRuntime": ("tikiagent.context.preparation", "ContextRuntime"),
    "ContextUsage": ("tikiagent.context.compression.models", "ContextUsage"),
    "DEFAULT_CONTEXT_PROFILES": (
        "tikiagent.context.profiles",
        "DEFAULT_CONTEXT_PROFILES",
    ),
    "FinalizationReport": ("tikiagent.context.models", "FinalizationReport"),
    "FinalizationService": ("tikiagent.context.finalization", "FinalizationService"),
    "HistoryConflictError": (
        "tikiagent.context.memory.history",
        "HistoryConflictError",
    ),
    "HistoryRecord": ("tikiagent.context.memory.models", "HistoryRecord"),
    "HistoryRecordType": ("tikiagent.context.schema", "HistoryRecordType"),
    "HistoryStore": ("tikiagent.context.memory.history", "HistoryStore"),
    "InMemoryHistoryStore": (
        "tikiagent.context.memory.history",
        "InMemoryHistoryStore",
    ),
    "InMemoryNotepadStore": (
        "tikiagent.context.memory.notepad",
        "InMemoryNotepadStore",
    ),
    "JsonlHistoryStore": ("tikiagent.context.memory.history", "JsonlHistoryStore"),
    "LocalCompressor": ("tikiagent.context.compression.compressors", "LocalCompressor"),
    "LocalMemory": ("tikiagent.context.memory.models", "LocalMemory"),
    "LocalMemoryManager": ("tikiagent.context.memory.local", "LocalMemoryManager"),
    "MarkdownNotepadStore": (
        "tikiagent.context.memory.notepad",
        "MarkdownNotepadStore",
    ),
    "NotepadConflictError": (
        "tikiagent.context.memory.notepad",
        "NotepadConflictError",
    ),
    "NotepadEntry": ("tikiagent.context.memory.models", "NotepadEntry"),
    "NotepadScope": ("tikiagent.context.schema", "NotepadScope"),
    "NotepadStore": ("tikiagent.context.memory.notepad", "NotepadStore"),
    "PreparedModelCall": ("tikiagent.context.models", "PreparedModelCall"),
    "PromptAssembler": ("tikiagent.context.prompt", "PromptAssembler"),
    "PromptBundle": ("tikiagent.context.models", "PromptBundle"),
    "ReActInteraction": ("tikiagent.context.memory.models", "ReActInteraction"),
    "RetrievalPolicy": ("tikiagent.context.memory.models", "RetrievalPolicy"),
    "Retriever": ("tikiagent.context.memory.retriever", "Retriever"),
    "RuleBasedBaseCompressor": (
        "tikiagent.context.compression.compressors",
        "RuleBasedBaseCompressor",
    ),
    "RuleBasedLocalCompressor": (
        "tikiagent.context.compression.compressors",
        "RuleBasedLocalCompressor",
    ),
    "TaskBoard": ("tikiagent.context.models", "TaskBoard"),
    "TaskBoardTransitionError": (
        "tikiagent.context.task_board",
        "TaskBoardTransitionError",
    ),
    "TodoItem": ("tikiagent.context.models", "TodoItem"),
    "TodoStatus": ("tikiagent.context.schema", "TodoStatus"),
    "TokenEstimator": ("tikiagent.context.compression.monitor", "TokenEstimator"),
    "ToolExposureGuard": ("tikiagent.context.tool_selection", "ToolExposureGuard"),
    "ToolSelector": ("tikiagent.context.tool_selection", "ToolSelector"),
    "ToolView": ("tikiagent.context.models", "ToolView"),
    "WorkingMemory": ("tikiagent.context.models", "WorkingMemory"),
    "add_todo": ("tikiagent.context.task_board", "add_todo"),
    "create_task_board": ("tikiagent.context.task_board", "create_task_board"),
    "next_actionable_todo": ("tikiagent.context.task_board", "next_actionable_todo"),
    "record_result": ("tikiagent.context.task_board", "record_result"),
    "record_verification": ("tikiagent.context.task_board", "record_verification"),
    "start_todo": ("tikiagent.context.task_board", "start_todo"),
    "todos_for_owner": ("tikiagent.context.task_board", "todos_for_owner"),
    "todos_for_refs": ("tikiagent.context.task_board", "todos_for_refs"),
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
