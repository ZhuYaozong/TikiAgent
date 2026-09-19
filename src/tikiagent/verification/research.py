"""ResearchResult 的来源与 Observation 对应关系验证。"""

from __future__ import annotations

from typing import Any

from tikiagent.orchestration.contracts import (
    Handoff,
    ResearchResult,
    VerificationCheck,
    VerificationReport,
)
from tikiagent.verification.reports import _linked_report


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
