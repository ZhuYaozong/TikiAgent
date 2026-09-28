"""按实际工具调用消耗预算，并阻止相同参数的失败持续重试。"""

from typing import Any
import hashlib
import json

from tikiagent.runtime.models import AgentRunResult, MaxStepsExceeded
from tikiagent.tools.models import ToolResult


class AgentLoopStopped(MaxStepsExceeded):
    """保留停止原因及实际观察，交给 Supervisor 做最终停止决策。"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.run_result: AgentRunResult | None = None


class ToolLoopGuard:
    def __init__(
        self, *, max_calls: int, repeat_limit: int,
        snapshot: dict[str, Any] | None = None, legacy_calls: int = 0,
    ) -> None:
        data = snapshot or {}
        # 恢复时采用更严格的配置，不能通过重启扩大已经冻结的预算。
        self.max_calls = min(max_calls, data.get("max_calls", max_calls))
        self.repeat_limit = min(repeat_limit, data.get("repeat_limit", repeat_limit))
        self.calls_used = max(data.get("calls_used", legacy_calls), legacy_calls)
        self.failures: dict[str, int] = dict(data.get("failures", {}))

    @staticmethod
    def fingerprint(name: str, arguments_json: str) -> str:
        try:
            arguments = json.loads(arguments_json)
        except ValueError:
            arguments = arguments_json
        value = json.dumps([name, arguments], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(value.encode()).hexdigest()

    def check(self, name: str, arguments_json: str) -> None:
        if self.calls_used >= self.max_calls:
            raise AgentLoopStopped(
                "tool_budget_exhausted",
                f"实际工具调用已达到预算 {self.max_calls}，停止继续执行",
            )
        key = self.fingerprint(name, arguments_json)
        if self.failures.get(key, 0) >= self.repeat_limit:
            raise AgentLoopStopped(
                "repeated_tool_failure",
                f"工具 {name} 的相同参数已失败 {self.repeat_limit} 次，停止无进展重试",
            )

    def record(self, name: str, arguments_json: str, result: ToolResult) -> None:
        self.calls_used += 1
        failed = not result.ok
        if name == "run_command" and isinstance(result.output, dict):
            failed = failed or bool(result.output.get("timed_out")) or result.output.get("exit_code") != 0
        key = self.fingerprint(name, arguments_json)
        if failed:
            self.failures[key] = self.failures.get(key, 0) + 1
        else:
            self.failures.pop(key, None)

    def snapshot(self) -> dict[str, Any]:
        return {
            "max_calls": self.max_calls,
            "repeat_limit": self.repeat_limit,
            "calls_used": self.calls_used,
            "failures": dict(self.failures),
        }
