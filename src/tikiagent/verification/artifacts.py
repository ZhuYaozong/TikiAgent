"""根据交付文件类型验证 Python、HTML 和文本产物。"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import PurePosixPath
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


class ArtifactAwareCodeVerifier:
    """根据当前 Result 的真实 Artifact 类型选择确定性环境验证。"""

    def __init__(
        self,
        *,
        dispatcher: Dispatcher,
        python_test_check: CommandCheck,
        execution_harness: ExecutionHarness | None = None,
    ) -> None:
        self.dispatcher = dispatcher
        self.environment = EnvironmentVerifier(
            dispatcher,
            (python_test_check,),
            execution_harness=execution_harness,
        )
        self.python_test_check = python_test_check
        self.supports_harness = execution_harness is not None

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
                evidence=f"result={result.handoff_id}; handoff={handoff.handoff_id}",
            ),
            VerificationCheck(
                name="code_agent_completed",
                passed=result.completed,
                evidence=f"completed={result.completed}",
            ),
            VerificationCheck(
                name="artifacts_declared",
                passed=bool(result.changed_files),
                evidence=f"changed_files={result.changed_files}",
            ),
        ]
        contents: dict[str, str] = {}
        for index, path in enumerate(dict.fromkeys(result.changed_files), start=1):
            read = self.environment._execute(
                {
                    "tool_call_id": f"verify_artifact_{index}",
                    "name": "read_file",
                    "arguments": {"path": path},
                },
                execution_context,
                {"read_file"},
            )
            content = (
                str(read.output.get("content", ""))
                if read.ok and isinstance(read.output, dict)
                else ""
            )
            if read.ok:
                contents[path] = content
            checks.append(
                VerificationCheck(
                    name=f"artifact_readable_nonempty:{path}",
                    passed=read.ok and bool(content.strip()),
                    evidence=(
                        f"path={path}; characters={len(content)}"
                        if read.ok
                        else f"path={path}; error={read.error}"
                    ),
                )
            )

        python_files = [path for path in contents if PurePosixPath(path).suffix == ".py"]
        if python_files:
            test_files = [
                path for path in python_files
                if PurePosixPath(path).name.startswith("test_")
                or "tests" in PurePosixPath(path).parts
            ]
            checks.append(
                VerificationCheck(
                    name="python_tests_present",
                    passed=bool(test_files),
                    evidence=f"test_files={test_files}",
                )
            )
            if test_files:
                checks.append(
                    self.environment._run_check(
                        1,
                        self.python_test_check,
                        execution_context,
                    )
                )

        for path, content in contents.items():
            if PurePosixPath(path).suffix.casefold() != ".html":
                continue
            parser = _RequiredHtmlParser()
            try:
                parser.feed(content)
                missing = sorted(_RequiredHtmlParser.REQUIRED - parser.tags)
                error = None
            except Exception as parse_error:  # noqa: BLE001 - 形成结构化失败证据
                missing = sorted(_RequiredHtmlParser.REQUIRED)
                error = str(parse_error)
            checks.append(
                VerificationCheck(
                    name=f"html_structure:{path}",
                    passed=not missing and error is None,
                    evidence=f"path={path}; missing={missing}; error={error}",
                )
            )

        raw_research = specialist_results.get("research_agent")
        if raw_research is not None:
            research = ResearchResult.model_validate(raw_research)
            urls = [source.url for source in research.sources]
            matched = [url for url in urls if any(url in text for text in contents.values())]
            checks.append(
                VerificationCheck(
                    name="research_sources_referenced",
                    passed=bool(urls) and bool(matched),
                    evidence=f"matched={matched}; sources={len(urls)}",
                )
            )
        return _linked_report(
            handoff=handoff,
            result_id=result.result_id,
            subject_agent="code_agent",
            mode="environment",
            checks=checks,
        )


class _RequiredHtmlParser(HTMLParser):
    REQUIRED = {"html", "head", "title", "body"}

    def __init__(self) -> None:
        super().__init__()
        self.tags: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        self.tags.add(tag.casefold())
