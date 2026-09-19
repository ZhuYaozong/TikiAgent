from pathlib import Path
import sys

from tikiagent.application.bootstrap import ApplicationRuntimeFactory
from tikiagent.harness.execution import ExecutionHarness
from tikiagent.harness.permissions.policy import FixedCommandPermissionPolicy
from tikiagent.harness.scope import ExecutionContext, ExecutionScope
from tikiagent.harness.workspace import Workspace
from tikiagent.orchestration.contracts import (
    CodeResult,
    Handoff,
    ResearchObservation,
    ResearchResult,
    ResearchSource,
)
from tikiagent.tools.commands import register_command_tool
from tikiagent.tools.dispatcher import Dispatcher
from tikiagent.tools.files import build_read_only_file_registry
from tikiagent.verification.artifacts import ArtifactAwareCodeVerifier
from tikiagent.verification.environment import CommandCheck


def build_verifier(tmp_path: Path):
    workspace = Workspace(tmp_path)
    registry = build_read_only_file_registry(workspace)
    register_command_tool(registry, workspace)
    dispatcher = Dispatcher(registry)
    command = (
        sys.executable,
        "-B",
        "-m",
        "unittest",
        "discover",
        "-s",
        ".",
        "-p",
        "test_*.py",
        "-v",
    )
    verifier = ArtifactAwareCodeVerifier(
        dispatcher=dispatcher,
        python_test_check=CommandCheck(name="python-unittest", command=command),
        execution_harness=ExecutionHarness(
            dispatcher,
            permission_policy=FixedCommandPermissionPolicy(
                allowed_commands={command}
            ),
        ),
    )
    context = ExecutionContext(
        scope=ExecutionScope(
            task_id="task-1",
            session_id="session-1",
            workspace_id="workspace-1",
        ),
        agent="verifier",
        exposed_tools={"read_file", "run_command"},
    )
    return verifier, dispatcher, context


def handoff(result_id: str = "code-result-1") -> Handoff:
    return Handoff(
        handoff_id="handoff-1",
        from_agent="supervisor",
        to_agent="code_agent",
        instruction="完成当前代码交付并验证",
        result_id=result_id,
        status="completed",
    )


def code_result(files: list[str], *, completed: bool = True) -> CodeResult:
    return CodeResult(
        result_id="code-result-1",
        handoff_id="handoff-1",
        summary="done",
        completed=completed,
        steps=3,
        changed_files=files,
    )


def verify(tmp_path: Path, result: CodeResult, specialists=None):
    verifier, dispatcher, context = build_verifier(tmp_path)
    report = verifier.verify(
        handoff=handoff(result.result_id),
        result=result,
        specialist_results=specialists or {"code_agent": result.model_dump(mode="json")},
        execution_context=context,
    )
    return report, dispatcher


def research_result() -> ResearchResult:
    return ResearchResult(
        result_id="research-1",
        handoff_id="research-handoff-1",
        summary="research",
        findings=["finding"],
        queries=["agent frameworks"],
        sources=[
            ResearchSource(
                observation_id="observation-1",
                title="source",
                url="https://example.com/source",
                snippet="snippet",
            )
        ],
        observations=[
            ResearchObservation(
                observation_id="observation-1",
                query="agent frameworks",
                urls=["https://example.com/source"],
            )
        ],
    )


def test_python_artifacts_run_real_unittest_through_harness(tmp_path: Path) -> None:
    (tmp_path / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "test_calculator.py").write_text(
        "import unittest\nfrom calculator import add\n\n"
        "class TestAdd(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )

    report, dispatcher = verify(
        tmp_path,
        code_result(["calculator.py", "test_calculator.py"]),
    )

    assert report.passed is True
    assert any(check.name == "python-unittest" for check in report.checks)
    assert dispatcher.registry.get("write_file") is None
    assert dispatcher.registry.get("edit_file") is None


def test_failing_python_tests_fail_verification(tmp_path: Path) -> None:
    (tmp_path / "calculator.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (tmp_path / "test_calculator.py").write_text(
        "import unittest\nfrom calculator import add\n\n"
        "class TestAdd(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )

    report, _ = verify(tmp_path, code_result(["calculator.py", "test_calculator.py"]))

    assert report.passed is False
    assert any("python-unittest" in failure for failure in report.failures)


def test_python_delivery_requires_test_file(tmp_path: Path) -> None:
    (tmp_path / "calculator.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")

    report, _ = verify(tmp_path, code_result(["calculator.py"]))

    assert report.passed is False
    assert any("python_tests_present" in failure for failure in report.failures)


def test_html_artifact_checks_structure_and_research_source(tmp_path: Path) -> None:
    (tmp_path / "comparison.html").write_text(
        "<html><head><title>Compare</title></head><body>"
        "<a href='https://example.com/source'>Source</a></body></html>",
        encoding="utf-8",
    )
    code = code_result(["comparison.html"])
    research = research_result()

    report, _ = verify(
        tmp_path,
        code,
        {
            "research_agent": research.model_dump(mode="json"),
            "code_agent": code.model_dump(mode="json"),
        },
    )

    assert report.passed is True
    assert any(check.name == "html_structure:comparison.html" for check in report.checks)
    assert any(check.name == "research_sources_referenced" for check in report.checks)


def test_invalid_html_and_missing_source_fail(tmp_path: Path) -> None:
    (tmp_path / "comparison.html").write_text("<html><body>no source</body></html>", encoding="utf-8")
    code = code_result(["comparison.html"])
    research = research_result()

    report, _ = verify(
        tmp_path,
        code,
        {"research_agent": research.model_dump(mode="json")},
    )

    assert report.passed is False
    assert any("html_structure" in failure for failure in report.failures)
    assert any("research_sources_referenced" in failure for failure in report.failures)


def test_missing_or_empty_declared_artifact_fails(tmp_path: Path) -> None:
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")

    report, _ = verify(tmp_path, code_result(["empty.txt", "missing.txt"]))

    assert report.passed is False
    assert sum("artifact_readable_nonempty" in failure for failure in report.failures) == 2


def test_formal_runtime_uses_artifact_aware_verifier(tmp_path: Path) -> None:
    factory = ApplicationRuntimeFactory(
        tmp_path / ".tiki",
        env_file=tmp_path / "missing.env",
    )

    workflow = factory._build_workflow("session-1", "workspace-1")

    assert isinstance(
        workflow.verification_gate.verifiers["code_agent"],
        ArtifactAwareCodeVerifier,
    )
