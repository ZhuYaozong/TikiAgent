"""构建绑定 Result 和 Handoff 的验证报告。"""

from __future__ import annotations

from tikiagent.orchestration.contracts import (
    Handoff,
    VerificationCheck,
    VerificationReport,
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
