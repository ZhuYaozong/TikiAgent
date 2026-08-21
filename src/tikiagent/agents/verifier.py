"""基于真实环境证据、无修改能力的 Verifier。"""

from dataclasses import dataclass
from typing import Any

from tikiagent.harness.dispatcher import Dispatcher
from tikiagent.harness.models import CommandResult
from tikiagent.orchestration.models import (
    ActorResult,
    Plan,
    VerificationCheck,
    VerificationReport,
)


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
    ) -> None:
        if not checks:
            raise ValueError("Verifier 至少需要一个环境检查")
        self.dispatcher = dispatcher
        self.checks = checks

    def verify(
        self,
        *,
        task: str,
        plan: Plan,
        actor_result: ActorResult,
    ) -> VerificationReport:
        del task, plan, actor_result
        verification_checks = [
            self._run_check(index, check)
            for index, check in enumerate(self.checks, start=1)
        ]
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

    def _run_check(
        self,
        index: int,
        check: CommandCheck,
    ) -> VerificationCheck:
        result = self.dispatcher.dispatch(
            {
                "tool_call_id": f"verify_{index}",
                "name": "run_command",
                "arguments": {
                    "command": list(check.command),
                    "cwd": check.cwd,
                    "timeout_seconds": check.timeout_seconds,
                },
            }
        )
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
