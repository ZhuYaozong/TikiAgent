"""早期工作流使用的固定文件验证器。"""

from __future__ import annotations

from typing import Any

from tikiagent.harness.execution import ExecutionHarness
from tikiagent.harness.scope import ExecutionContext
from tikiagent.orchestration.contracts import (
    CodeResult,
    Handoff,
    ResearchResult,
    VerificationCheck,
    VerificationReport,
)
from tikiagent.tools.dispatcher import Dispatcher
from tikiagent.verification.environment import CommandCheck, EnvironmentVerifier
from tikiagent.verification.reports import _linked_report


class CodeEnvironmentVerifier:
    """验证 CodeResult 身份、交付归属、环境检查和来源引用。"""

    def __init__(
        self,
        *,
        dispatcher: Dispatcher,
        checks: tuple[CommandCheck, ...],
        expected_files: tuple[str, ...],
        execution_harness: ExecutionHarness | None = None,
    ) -> None:
        if not expected_files:
            raise ValueError("Code Verifier 至少需要一个目标文件")
        self.dispatcher = dispatcher
        self.environment = EnvironmentVerifier(
            dispatcher,
            checks,
            execution_harness=execution_harness,
        )
        self.supports_harness = execution_harness is not None
        self.expected_files = expected_files

    def verify(
        self,
        *,
        handoff: Handoff,
        result: CodeResult,
        specialist_results: dict[str, dict[str, Any]],
        execution_context: ExecutionContext | None = None,
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
            *self.environment.run_checks(execution_context),
        ]
        raw_research = specialist_results.get("research_agent")
        if raw_research is not None:
            research = ResearchResult.model_validate(raw_research)
            checks.append(
                self._source_reference_check(research, execution_context)
            )
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
        execution_context: ExecutionContext | None,
    ) -> VerificationCheck:
        contents: list[str] = []
        read_errors: list[str] = []
        for index, path in enumerate(self.expected_files, start=1):
            result = self.environment._execute(
                {
                    "tool_call_id": f"verify_source_{index}",
                    "name": "read_file",
                    "arguments": {"path": path},
                },
                execution_context,
                {"read_file"},
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
