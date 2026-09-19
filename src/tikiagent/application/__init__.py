"""包级兼容导出；内部使用明确模块路径。"""

from importlib import import_module

_EXPORTS = {
    "ApplicationController": (
        "tikiagent.application.controller",
        "ApplicationController",
    ),
    "ApplicationError": ("tikiagent.application.controller", "ApplicationError"),
    "ApplicationEvent": ("tikiagent.application.models", "ApplicationEvent"),
    "ApplicationOutcome": ("tikiagent.application.models", "ApplicationOutcome"),
    "ChatService": ("tikiagent.application.chat", "ChatService"),
    "CliEventSink": ("tikiagent.application.events", "CliEventSink"),
    "CollectingEventSink": ("tikiagent.application.events", "CollectingEventSink"),
    "EventBus": ("tikiagent.application.events", "EventBus"),
    "EventScope": ("tikiagent.application.models", "EventScope"),
    "EventSink": ("tikiagent.application.events", "EventSink"),
    "FilteringEventSink": ("tikiagent.application.events", "FilteringEventSink"),
    "HarnessEventForwarder": (
        "tikiagent.application.harness_events",
        "HarnessEventForwarder",
    ),
    "IntentDecision": ("tikiagent.application.models", "IntentDecision"),
    "IntentRouter": ("tikiagent.application.routing", "IntentRouter"),
    "JsonSessionStore": ("tikiagent.application.session", "JsonSessionStore"),
    "JsonlTurnStore": ("tikiagent.application.session", "JsonlTurnStore"),
    "ModelChatService": ("tikiagent.application.chat", "ModelChatService"),
    "ReconcileSubmission": ("tikiagent.application.models", "ReconcileSubmission"),
    "ResponseRecord": ("tikiagent.application.models", "ResponseRecord"),
    "RuleBasedIntentRouter": ("tikiagent.application.routing", "RuleBasedIntentRouter"),
    "SessionConflictError": ("tikiagent.application.session", "SessionConflictError"),
    "SessionContextReferenceProvider": (
        "tikiagent.application.context_refs",
        "SessionContextReferenceProvider",
    ),
    "SessionNotFoundError": ("tikiagent.application.session", "SessionNotFoundError"),
    "SessionRecord": ("tikiagent.application.models", "SessionRecord"),
    "SessionService": ("tikiagent.application.session", "SessionService"),
    "StructuredIntentRouter": (
        "tikiagent.application.routing",
        "StructuredIntentRouter",
    ),
    "TikiWorkflowAdapter": (
        "tikiagent.application.workflow_adapter",
        "TikiWorkflowAdapter",
    ),
    "TurnRecord": ("tikiagent.application.models", "TurnRecord"),
    "WorkflowOutcome": ("tikiagent.application.models", "WorkflowOutcome"),
    "WorkflowPort": ("tikiagent.application.workflow_adapter", "WorkflowPort"),
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
