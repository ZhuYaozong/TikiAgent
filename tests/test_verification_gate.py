"""Verification Gate 的策略选择与身份关联测试。"""

import sys
from pathlib import Path

from tikiagent.agents.verifier import (
    CodeEnvironmentVerifier,
    CommandCheck,
    ResearchResultVerifier,
)
from tikiagent.harness.command_tools import register_command_tool
from tikiagent.harness.dispatcher import Dispatcher
from tikiagent.harness.file_tools import build_read_only_file_registry
from tikiagent.harness.workspace import Workspace
from tikiagent.orchestration.models import (
    CodeResult,
    Handoff,
    ResearchObservation,
    ResearchResult,
    ResearchSource,
)
from tikiagent.orchestration.verification_gate import VerificationGate


def completed_handoff(agent: str, result_id: str) -> Handoff:
    return Handoff.model_validate(
        {
            "handoff_id": f"handoff-{agent}",
            "from_agent": "supervisor",
            "to_agent": agent,
            "instruction": "execute",
            "result_id": result_id,
            "status": "completed",
        }
    )


def research_result() -> ResearchResult:
    return ResearchResult(
        result_id="research-result-1",
        handoff_id="handoff-research_agent",
        summary="summary",
        findings=["finding"],
        sources=[
            ResearchSource(
                observation_id="search-1",
                title="source",
                url="https://example.com/source",
                snippet="snippet",
            )
        ],
        observations=[
            ResearchObservation(
                observation_id="search-1",
                query="latest agent",
                urls=["https://example.com/source"],
            )
        ],
        queries=["latest agent"],
    )


def code_verifier(tmp_path: Path) -> CodeEnvironmentVerifier:
    workspace = Workspace(tmp_path)
    registry = build_read_only_file_registry(workspace)
    register_command_tool(registry, workspace)
    return CodeEnvironmentVerifier(
        dispatcher=Dispatcher(registry),
        checks=(
            CommandCheck(
                name="python_ok",
                command=(sys.executable, "-B", "-c", "print('ok')"),
            ),
        ),
        expected_files=("comparison.html",),
    )


def gate(tmp_path: Path) -> VerificationGate:
    return VerificationGate(
        research_verifier=ResearchResultVerifier(),
        code_verifier=code_verifier(tmp_path),
    )


def test_research_result_passes_rule_verification(tmp_path: Path) -> None:
    result = research_result()
    report = gate(tmp_path).verify(
        handoff=completed_handoff("research_agent", result.result_id),
        raw_result=result.model_dump(mode="json"),
        specialist_results={"research_agent": result.model_dump(mode="json")},
    )

    assert report.passed is True
    assert report.result_id == result.result_id
    assert report.handoff_id == result.handoff_id
    assert report.subject_agent == "research_agent"
    assert report.mode == "rules"


def test_tampered_research_source_fails_provenance(tmp_path: Path) -> None:
    result = research_result().model_copy(
        update={
            "sources": [
                ResearchSource(
                    observation_id="search-1",
                    title="invented",
                    url="https://invented.example/source",
                    snippet="",
                )
            ]
        }
    )
    report = gate(tmp_path).verify(
        handoff=completed_handoff("research_agent", result.result_id),
        raw_result=result.model_dump(mode="json"),
        specialist_results={"research_agent": result.model_dump(mode="json")},
    )

    assert report.passed is False
    assert any("source_observation_provenance" in item for item in report.failures)


def test_mismatched_result_id_is_rejected_before_strategy(tmp_path: Path) -> None:
    result = research_result()
    report = gate(tmp_path).verify(
        handoff=completed_handoff("research_agent", "older-result"),
        raw_result=result.model_dump(mode="json"),
        specialist_results={},
    )

    assert report.passed is False
    assert report.result_id == result.result_id
    assert "不匹配" in report.evidence[0]


def test_code_result_requires_current_artifact_and_source(tmp_path: Path) -> None:
    source = research_result()
    (tmp_path / "comparison.html").write_text(
        "<html><body>https://example.com/source</body></html>",
        encoding="utf-8",
    )
    result = CodeResult(
        result_id="code-result-1",
        handoff_id="handoff-code_agent",
        summary="done",
        completed=True,
        steps=2,
        changed_files=["comparison.html"],
        context_refs_used=["research-result-1"],
    )
    report = gate(tmp_path).verify(
        handoff=completed_handoff("code_agent", result.result_id),
        raw_result=result.model_dump(mode="json"),
        specialist_results={
            "research_agent": source.model_dump(mode="json"),
            "code_agent": result.model_dump(mode="json"),
        },
    )

    assert report.passed is True
    assert report.result_id == "code-result-1"
    assert report.subject_agent == "code_agent"


def test_stale_workspace_file_cannot_replace_current_artifact_ownership(
    tmp_path: Path,
) -> None:
    (tmp_path / "comparison.html").write_text(
        "<html><body>old</body></html>",
        encoding="utf-8",
    )
    result = CodeResult(
        result_id="code-result-2",
        handoff_id="handoff-code_agent",
        summary="no changes",
        completed=True,
        steps=1,
        changed_files=[],
    )
    report = gate(tmp_path).verify(
        handoff=completed_handoff("code_agent", result.result_id),
        raw_result=result.model_dump(mode="json"),
        specialist_results={"code_agent": result.model_dump(mode="json")},
    )

    assert report.passed is False
    assert any("current_result_owns_artifacts" in item for item in report.failures)
