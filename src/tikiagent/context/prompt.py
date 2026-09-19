"""根据 Agent Profile 和当前阶段动态组装行为 Prompt。"""

from collections.abc import Mapping

from tikiagent.context.models import ContextProfile, PromptBundle
from tikiagent.context.profiles import DEFAULT_CONTEXT_PROFILES
from tikiagent.context.schema import ContextAgentName


class PromptAssembler:
    """Prompt 只描述怎样工作，不重复 BaseContext 的任务数据。"""

    def __init__(
        self,
        profiles: Mapping[ContextAgentName, ContextProfile] | None = None,
    ) -> None:
        self.profiles = {
            **DEFAULT_CONTEXT_PROFILES,
            **dict(profiles or {}),
        }

    def assemble(self, agent: ContextAgentName, phase: str) -> PromptBundle:
        profile = self.profiles[agent]
        phase_rules = profile.phase_rules.get(
            phase,
            profile.phase_rules.get("default", []),
        )
        return PromptBundle(
            stable_system_rules=profile.system_rules,
            agent_role=profile.role,
            phase_rules=phase_rules,
        )
