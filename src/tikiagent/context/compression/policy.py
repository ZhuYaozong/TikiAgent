"""根据上下文预算选择压缩范围。"""

from __future__ import annotations

from tikiagent.context.compression.models import ContextUsage


class CompressionPolicy:
    """根据分项和总预算决定哪类 Context 需要尝试压缩。"""

    @staticmethod
    def should_compress_base(usage: ContextUsage) -> bool:
        return usage.base_over_budget or usage.total_over_budget

    @staticmethod
    def should_compress_local(usage: ContextUsage) -> bool:
        return usage.local_over_budget or usage.total_over_budget
