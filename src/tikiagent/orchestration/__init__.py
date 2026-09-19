"""包级兼容导出；内部使用明确模块路径。"""

from importlib import import_module

_EXPORTS = {
    "ActorResult": ("tikiagent.orchestration.contracts", "ActorResult"),
    "CodeResult": ("tikiagent.orchestration.contracts", "CodeResult"),
    "Handoff": ("tikiagent.orchestration.contracts", "Handoff"),
    "MultiAgentWorkflow": ("tikiagent.orchestration.workflow", "MultiAgentWorkflow"),
    "PendingToolCall": ("tikiagent.orchestration.state", "PendingToolCall"),
    "Plan": ("tikiagent.orchestration.contracts", "Plan"),
    "PlanVerifyWorkflow": ("tikiagent.baselines.plan_verify", "PlanVerifyWorkflow"),
    "PlannerDecision": ("tikiagent.orchestration.contracts", "PlannerDecision"),
    "PlanningResult": ("tikiagent.orchestration.contracts", "PlanningResult"),
    "ReActWorkflow": ("tikiagent.baselines.react_graph", "ReActWorkflow"),
    "ResearchObservation": ("tikiagent.orchestration.contracts", "ResearchObservation"),
    "ResearchResult": ("tikiagent.orchestration.contracts", "ResearchResult"),
    "ResearchSource": ("tikiagent.orchestration.contracts", "ResearchSource"),
    "SupervisorDecision": ("tikiagent.orchestration.contracts", "SupervisorDecision"),
    "SupervisorPlan": ("tikiagent.orchestration.contracts", "SupervisorPlan"),
    "TikiState": ("tikiagent.orchestration.state", "TikiState"),
    "VerificationCheck": ("tikiagent.orchestration.contracts", "VerificationCheck"),
    "VerificationGate": ("tikiagent.verification.gate", "VerificationGate"),
    "VerificationReport": ("tikiagent.orchestration.contracts", "VerificationReport"),
    "WorkflowStatus": ("tikiagent.orchestration.state", "WorkflowStatus"),
    "compose_final_answer": (
        "tikiagent.orchestration.completion",
        "compose_final_answer",
    ),
    "create_initial_state": ("tikiagent.orchestration.state", "create_initial_state"),
    "create_multi_agent_state": (
        "tikiagent.orchestration.state",
        "create_multi_agent_state",
    ),
    "create_plan_verify_state": (
        "tikiagent.orchestration.state",
        "create_plan_verify_state",
    ),
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
