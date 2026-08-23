"""所有 Specialist Result 必经的通用 Verification Gate。"""

from typing import Any, Protocol

from tikiagent.orchestration.models import (
    CodeResult,
    Handoff,
    ResearchResult,
    VerificationCheck,
    VerificationReport,
)


class SpecialistVerifier(Protocol):
    def verify(
        self,
        *,
        handoff: Handoff,
        result: Any,
        specialist_results: dict[str, dict[str, Any]],
    ) -> VerificationReport: ...


class VerificationGate:
    """选择验证策略，并强制 Result/Handoff/Report 三者关联一致。"""

    def __init__(
        self,
        *,
        research_verifier: SpecialistVerifier,
        code_verifier: SpecialistVerifier,
    ) -> None:
        self.verifiers = {
            "research_agent": research_verifier,
            "code_agent": code_verifier,
        }

    def verify(
        self,
        *,
        handoff: Handoff,
        raw_result: dict[str, Any],
        specialist_results: dict[str, dict[str, Any]],
    ) -> VerificationReport:
        result_type = (
            ResearchResult
            if handoff.to_agent == "research_agent"
            else CodeResult
        )
        try:
            result = result_type.model_validate(raw_result)
        except ValueError as error:
            return self._identity_failure(
                handoff,
                str(raw_result.get("result_id", "invalid-result")),
                f"Result 结构不合法：{error}",
            )

        if handoff.status != "completed":
            return self._identity_failure(
                handoff,
                result.result_id,
                f"Handoff 尚未完成：status={handoff.status}",
            )
        if handoff.result_id != result.result_id:
            return self._identity_failure(
                handoff,
                result.result_id,
                (
                    f"Handoff.result_id={handoff.result_id} 与 "
                    f"Result.result_id={result.result_id} 不匹配"
                ),
            )
        if result.handoff_id != handoff.handoff_id:
            return self._identity_failure(
                handoff,
                result.result_id,
                (
                    f"Result.handoff_id={result.handoff_id} 与 "
                    f"Handoff.handoff_id={handoff.handoff_id} 不匹配"
                ),
            )

        report = self.verifiers[handoff.to_agent].verify(
            handoff=handoff,
            result=result,
            specialist_results=specialist_results,
        )
        if (
            report.result_id != result.result_id
            or report.handoff_id != handoff.handoff_id
            or report.subject_agent != handoff.to_agent
        ):
            return self._identity_failure(
                handoff,
                result.result_id,
                "Verifier 返回了指向其他 Result/Handoff 的报告",
            )
        return report

    @staticmethod
    def _identity_failure(
        handoff: Handoff,
        result_id: str,
        evidence: str,
    ) -> VerificationReport:
        check = VerificationCheck(
            name="result_verification_identity",
            passed=False,
            evidence=evidence,
        )
        return VerificationReport(
            result_id=result_id,
            handoff_id=handoff.handoff_id,
            subject_agent=handoff.to_agent,
            mode=(
                "rules"
                if handoff.to_agent == "research_agent"
                else "environment"
            ),
            passed=False,
            checks=[check],
            failures=[f"{check.name}: {check.evidence}"],
            evidence=[check.evidence],
            recommendation="身份关联失败，禁止 Supervisor FINISH",
        )
