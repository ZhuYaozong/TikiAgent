"""基于真实环境证据、无修改能力的 Verifier。"""

from dataclasses import dataclass
from typing import Any

from tikiagent.harness.dispatcher import Dispatcher
from tikiagent.harness.models import CommandResult
from tikiagent.orchestration.models import (
    ActorResult,
    CodeResult,
    Handoff,
    Plan,
    ResearchResult,
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

    def run_checks(self) -> list[VerificationCheck]:
        """运行应用固定的环境检查，供 Verification Gate 复用。"""

        return [
            self._run_check(index, check)
            for index, check in enumerate(self.checks, start=1)
        ]

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


class ResearchResultVerifier:
    """对 ResearchResult 进行不调用 LLM 的来源规则验证。"""

    def __init__(self, min_sources: int = 1) -> None:
        if min_sources < 1:
            raise ValueError("min_sources 必须大于 0")
        self.min_sources = min_sources

    def verify(
        self,
        *,
        handoff: Handoff,
        result: ResearchResult,
        specialist_results: dict[str, dict[str, Any]],
    ) -> VerificationReport:
        del specialist_results
        observation_urls = {
            (observation.observation_id, url)
            for observation in result.observations
            for url in observation.urls
        }
        checks = [
            VerificationCheck(
                name="result_handoff_link",
                passed=result.handoff_id == handoff.handoff_id,
                evidence=(
                    f"result.handoff_id={result.handoff_id}; "
                    f"handoff.handoff_id={handoff.handoff_id}"
                ),
            ),
            VerificationCheck(
                name="findings_present",
                passed=bool(result.findings),
                evidence=f"findings={len(result.findings)}",
            ),
            VerificationCheck(
                name="queries_present",
                passed=bool(result.queries),
                evidence=f"queries={result.queries}",
            ),
            VerificationCheck(
                name="minimum_sources",
                passed=len(result.sources) >= self.min_sources,
                evidence=(
                    f"sources={len(result.sources)}; "
                    f"required={self.min_sources}"
                ),
            ),
            VerificationCheck(
                name="source_observation_provenance",
                passed=bool(result.sources)
                and all(
                    (source.observation_id, source.url)
                    in observation_urls
                    for source in result.sources
                ),
                evidence=(
                    "source_pairs="
                    f"{[(item.observation_id, item.url) for item in result.sources]}"
                ),
            ),
        ]
        return _linked_report(
            handoff=handoff,
            result_id=result.result_id,
            subject_agent="research_agent",
            mode="rules",
            checks=checks,
        )


class CodeEnvironmentVerifier:
    """验证 CodeResult 身份、交付归属、环境检查和来源引用。"""

    def __init__(
        self,
        *,
        dispatcher: Dispatcher,
        checks: tuple[CommandCheck, ...],
        expected_files: tuple[str, ...],
    ) -> None:
        if not expected_files:
            raise ValueError("Code Verifier 至少需要一个目标文件")
        self.dispatcher = dispatcher
        self.environment = EnvironmentVerifier(dispatcher, checks)
        self.expected_files = expected_files

    def verify(
        self,
        *,
        handoff: Handoff,
        result: CodeResult,
        specialist_results: dict[str, dict[str, Any]],
    ) -> VerificationReport:
        checks = [
            VerificationCheck(
                name="result_handoff_link",
                passed=result.handoff_id == handoff.handoff_id,
                evidence=(
                    f"result.handoff_id={result.handoff_id}; "
                    f"handoff.handoff_id={handoff.handoff_id}"
                ),
            ),
            VerificationCheck(
                name="code_agent_completed",
                passed=result.completed,
                evidence=f"completed={result.completed}",
            ),
            VerificationCheck(
                name="current_result_owns_artifacts",
                passed=all(
                    path in result.changed_files for path in self.expected_files
                ),
                evidence=(
                    f"changed_files={result.changed_files}; "
                    f"expected={list(self.expected_files)}"
                ),
            ),
            *self.environment.run_checks(),
        ]
        raw_research = specialist_results.get("research_agent")
        if raw_research is not None:
            research = ResearchResult.model_validate(raw_research)
            checks.append(self._source_reference_check(research))
        return _linked_report(
            handoff=handoff,
            result_id=result.result_id,
            subject_agent="code_agent",
            mode="environment",
            checks=checks,
        )

    def _source_reference_check(
        self,
        research: ResearchResult,
    ) -> VerificationCheck:
        contents: list[str] = []
        read_errors: list[str] = []
        for index, path in enumerate(self.expected_files, start=1):
            result = self.dispatcher.dispatch(
                {
                    "tool_call_id": f"verify_source_{index}",
                    "name": "read_file",
                    "arguments": {"path": path},
                }
            )
            if result.ok and isinstance(result.output, dict):
                contents.append(str(result.output.get("content", "")))
            else:
                read_errors.append(str(result.error))
        urls = [source.url for source in research.sources]
        matched = [url for url in urls if any(url in text for text in contents)]
        return VerificationCheck(
            name="research_sources_referenced",
            passed=bool(urls) and bool(matched),
            evidence=f"matched={matched}; read_errors={read_errors}",
        )


def _linked_report(
    *,
    handoff: Handoff,
    result_id: str,
    subject_agent: str,
    mode: str,
    checks: list[VerificationCheck],
) -> VerificationReport:
    failures = [
        f"{check.name}: {check.evidence}"
        for check in checks
        if not check.passed
    ]
    passed = not failures
    return VerificationReport(
        result_id=result_id,
        handoff_id=handoff.handoff_id,
        subject_agent=subject_agent,
        mode=mode,
        passed=passed,
        checks=checks,
        failures=failures,
        evidence=[check.evidence for check in checks],
        recommendation=(
            "验证通过，建议 Supervisor 继续全局决策"
            if passed
            else "验证失败，建议 Supervisor 重新委派或停止"
        ),
    )
