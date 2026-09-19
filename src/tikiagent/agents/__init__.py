"""包级兼容导出；内部使用明确模块路径。"""

from importlib import import_module

_EXPORTS = {
    "AgentRunPause": ("tikiagent.runtime.models", "AgentRunPause"),
    "AgentRunResult": ("tikiagent.runtime.models", "AgentRunResult"),
    "ArtifactAwareCodeVerifier": (
        "tikiagent.verification.artifacts",
        "ArtifactAwareCodeVerifier",
    ),
    "CodeEnvironmentVerifier": (
        "tikiagent.baselines.fixed_file_verifier",
        "CodeEnvironmentVerifier",
    ),
    "CommandCheck": ("tikiagent.verification.environment", "CommandCheck"),
    "EnvironmentVerifier": (
        "tikiagent.verification.environment",
        "EnvironmentVerifier",
    ),
    "MaxStepsExceeded": ("tikiagent.runtime.models", "MaxStepsExceeded"),
    "MultiAgentCodeAgent": ("tikiagent.agents.code", "MultiAgentCodeAgent"),
    "PlannerAgent": ("tikiagent.baselines.planner", "PlannerAgent"),
    "ReActAgent": ("tikiagent.runtime.react", "ReActAgent"),
    "ReActCodeActor": ("tikiagent.baselines.code_actor", "ReActCodeActor"),
    "ResearchAgent": ("tikiagent.agents.research", "ResearchAgent"),
    "ResearchResultVerifier": (
        "tikiagent.verification.research",
        "ResearchResultVerifier",
    ),
    "ResumableReActAgent": ("tikiagent.runtime.resumable", "ResumableReActAgent"),
    "SupervisorAgent": ("tikiagent.agents.supervisor", "SupervisorAgent"),
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
