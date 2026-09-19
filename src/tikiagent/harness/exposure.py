"""Execution Harness 的执行侧能力保护。"""

from collections.abc import Collection


class ToolExposureGuard:
    """拒绝本轮未向模型暴露的工具；必须在 Permission 之前运行。"""

    @staticmethod
    def allows(tool_name: str, exposed_tools: Collection[str]) -> bool:
        return tool_name in exposed_tools
