"""动态 Tool View 与本轮工具暴露保护。"""

from collections.abc import Mapping

from tikiagent.context.models import (
    ContextAgentName,
    ContextProfile,
    ToolView,
)
from tikiagent.context.profiles import DEFAULT_CONTEXT_PROFILES
from tikiagent.harness.guards import ToolExposureGuard as HarnessToolExposureGuard
from tikiagent.harness.registry import ToolRegistry


class ToolSelector:
    """只决定哪些 Schema 给模型看，不负责 Permission。"""

    def __init__(
        self,
        profiles: Mapping[ContextAgentName, ContextProfile] | None = None,
    ) -> None:
        self.profiles = {
            **DEFAULT_CONTEXT_PROFILES,
            **dict(profiles or {}),
        }

    def select(
        self,
        *,
        agent: ContextAgentName,
        phase: str,
        registry: ToolRegistry,
    ) -> ToolView:
        profile = self.profiles[agent]
        configured = profile.tool_names_by_phase.get(
            phase,
            profile.tool_names_by_phase.get("default", set()),
        )
        # Profile 可以声明未来工具，但本轮只能暴露 Registry 真实存在的交集。
        exposed = set(configured) & registry.names()
        return ToolView(
            exposed_names=exposed,
            schemas=registry.schemas(exposed),
        )


class ToolExposureGuard:
    """Context 兼容入口；实际判断委托给 Harness Plane。"""

    @staticmethod
    def allows(tool_name: str, tool_view: ToolView) -> bool:
        return HarnessToolExposureGuard.allows(
            tool_name,
            tool_view.exposed_names,
        )
