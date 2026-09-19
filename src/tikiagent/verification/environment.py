"""固定命令与只读环境验证的共享实现。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tikiagent.harness.execution import ExecutionHarness
from tikiagent.harness.scope import ExecutionContext
from tikiagent.orchestration.contracts import (
    ActorResult,
    Plan,
    VerificationCheck,
    VerificationReport,
)
from tikiagent.tools.dispatcher import Dispatcher
from tikiagent.tools.models import CommandResult, ToolResult


@dataclass(frozen=True, slots=True)
class CommandCheck:
    """由应用代码预先配置的一条验证命令。"""

    name: str
    command: tuple[str, ...]
    cwd: str = "."
    timeout_seconds: float = 30.0


class EnvironmentVerifier:
    """执行固定检查并生成报告，不调用写工具或决定路由。"""

    def __init__(
        self,
        dispatcher: Dispatcher,
        checks: tuple[CommandCheck, ...],
        execution_harness: ExecutionHarness | None = None,
    ) -> None:
        if not checks:
            raise ValueError("Verifier 至少需要一个环境检查")
        self.dispatcher = dispatcher
        self.checks = checks
        self.execution_harness = execution_harness
        self.supports_harness = execution_harness is not None

    def verify(
        self,
        *,
        task: str,
        plan: Plan,
        actor_result: ActorResult,
    ) -> VerificationReport:
        del task, plan, actor_result
        verification_checks = self.run_checks()
        failures = [
            f"{check.name}: {check.evidence}"
            for check in verification_checks
            if not check.passed
        ]
        passed = not failures
        return VerificationReport(
            passed=passed,
            checks=verification_checks,
            failures=failures,
            evidence=[check.evidence for check in verification_checks],
            recommendation=(
                "环境检查通过，建议 Planner 结束任务"
                if passed
                else "环境检查失败，建议 Planner 根据证据重新规划"
            ),
        )

    def run_checks(
        self,
        execution_context: ExecutionContext | None = None,
    ) -> list[VerificationCheck]:
        """运行应用固定的环境检查，供 Verification Gate 复用。"""

        return [
            self._run_check(index, check, execution_context)
            for index, check in enumerate(self.checks, start=1)
        ]

    def _run_check(
        self,
        index: int,
        check: CommandCheck,
        execution_context: ExecutionContext | None,
    ) -> VerificationCheck:
        raw_call = {
                "tool_call_id": f"verify_{index}",
                "name": "run_command",
                "arguments": {
                    "command": list(check.command),
                    "cwd": check.cwd,
                    "timeout_seconds": check.timeout_seconds,
                },
            }
        result = self._execute(raw_call, execution_context, {"run_command"})
        if not result.ok:
            return VerificationCheck(
                name=check.name,
                passed=False,
                evidence=f"Harness error: {result.error}",
            )

        try:
            command_result = CommandResult.model_validate(result.output)
        except ValueError as error:
            return VerificationCheck(
                name=check.name,
                passed=False,
                evidence=f"CommandResult 结构不合法：{error}",
            )

        passed = (
            not command_result.timed_out
            and command_result.exit_code == 0
        )
        return VerificationCheck(
            name=check.name,
            passed=passed,
            evidence=self._format_evidence(command_result),
        )

    def _execute(
        self,
        raw_call: dict[str, Any],
        execution_context: ExecutionContext | None,
        exposed_tools: set[str],
    ) -> ToolResult:
        if self.execution_harness is None:
            return self.dispatcher.dispatch(raw_call)
        if execution_context is None:
            raise ValueError("正式 Verifier 需要 ExecutionContext")
        outcome = self.execution_harness.handle(
            raw_call,
            context=execution_context.model_copy(
                update={"exposed_tools": exposed_tools}
            ),
        )
        if outcome.status == "awaiting_approval":
            raise RuntimeError("Verifier 只允许无需审批的只读检查")
        assert outcome.tool_result is not None
        return outcome.tool_result

    @staticmethod
    def _format_evidence(result: CommandResult) -> str:
        output: dict[str, Any] = {
            "command": result.command,
            "cwd": result.cwd,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        return str(output)
