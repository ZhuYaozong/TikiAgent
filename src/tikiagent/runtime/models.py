"""Agent 运行结果、暂停状态与预算异常。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from tikiagent.context.compression.models import ContextUsage
from tikiagent.harness.permissions.models import ApprovalRequest
from tikiagent.tools.models import ToolResult


class MaxStepsExceeded(RuntimeError):
    """Agent 在限制内未产生最终回答。"""


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    final_text: str
    steps: int
    tool_results: tuple[ToolResult, ...]
    messages: tuple[dict[str, Any], ...]
    context_usages: tuple[ContextUsage, ...] = ()
    phases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentRunPause:
    """ASK 或未知副作用恢复时交给外层 Graph 的暂停事实。"""

    status: Literal[
        "awaiting_approval",
        "recovery_required",
        "awaiting_reconcile",
    ]
    checkpoint_id: str
    revision: int
    approval_request: ApprovalRequest | None = None


AgentRunOutcome = AgentRunResult | AgentRunPause
