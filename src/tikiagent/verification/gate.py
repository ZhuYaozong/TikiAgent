"""所有 Specialist Result 必经的通用 Verification Gate。"""

from typing import Any, Protocol

from tikiagent.harness.scope import ExecutionContext
from tikiagent.orchestration.requirements import assessments_valid, assessment_structure_valid
from tikiagent.verification.failures import execution_failure, failure_report
from tikiagent.providers.llm.openai_compatible import ModelOutputError
from tikiagent.context.preparation import ContextBudgetExceeded
from tikiagent.verification.research import ResearchResultVerifier
from tikiagent.orchestration.contracts import (
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
    supports_related_results = True

    def __init__(
        self,
        *,
        research_verifier: SpecialistVerifier,
        code_verifier: SpecialistVerifier,
        basic_verifier: SpecialistVerifier | None = None,
    ) -> None:
        self.verifiers = {
            "research_agent": research_verifier,
            "code_agent": code_verifier,
        }
        # 未提供基础策略的历史示例保持原行为；正式入口启用条件独立审核。
        self.basic_verifier = basic_verifier
        self.supports_harness = any(
            getattr(verifier, "supports_harness", False)
            for verifier in self.verifiers.values()
        )
        self.supports_context = any(getattr(v, "supports_context", False) for v in self.verifiers.values())

    def needs_context(self, handoff: Handoff) -> bool:
        """基础检查不创建 Verifier Prompt，更不触发其上下文压缩请求。"""
        return (self.basic_verifier is None or handoff.verification_level == "independent") and bool(
            getattr(self.verifiers[handoff.to_agent], "supports_context", False))

    def verify(
        self,
        *,
        handoff: Handoff,
        raw_result: dict[str, Any],
        specialist_results: dict[str, dict[str, Any]],
        execution_context: ExecutionContext | None = None,
        research_results: list[dict[str, Any]] | None = None,
        base_context=None,
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

        verifier = self.verifiers[handoff.to_agent]
        # 即使结果只交付了一部分，也不允许虚构来源在提前返回路径绕过身份边界。
        if isinstance(result, ResearchResult):
            observation_urls = {(o.observation_id, url) for o in result.observations for url in o.urls}
            if any((s.observation_id, s.url) not in observation_urls for s in result.sources):
                return self._identity_failure(handoff, result.result_id, "source_observation_provenance: 来源不能追溯到真实搜索 Observation")
        basic = None
        if self.basic_verifier is not None:
            if not handoff.todo_id or not handoff.acceptance_criteria:
                return self._identity_failure(handoff, result.result_id, "缺少冻结的 Todo/验收契约，不能通过基础检查")
            basic = self.basic_verifier.verify(handoff=handoff, result=result, specialist_results=specialist_results)
            if basic.hard_blockers:
                return basic
            # 结果整理错误仍属于执行端，不能因跳过 LLM 审核而刷新研究工具/收尾预算。
            if result.finalization_status in {"failed", "already_consumed"}:
                return self._with_basic(execution_failure(handoff, result), basic)
            if handoff.verification_level == "basic":
                return basic
        delivery = getattr(result, "delivery_status", None)
        # 部分研究交付不等于验收失败：有真实证据且已完成收尾时交给 Verifier 判断。
        reviewable_partial = (
            isinstance(result, ResearchResult)
            and delivery == "partial"
            and result.finalization_status == "completed"
            and bool(result.findings and result.sources)
        )
        if (delivery in {"none", "partial"} and not reviewable_partial) or (delivery is None and isinstance(result, CodeResult) and not result.completed):
            report = execution_failure(handoff, result)
            return self._with_basic(report, basic)
        related_kwargs = {"research_results": research_results} if getattr(verifier, "supports_related_results", False) else {}
        if getattr(verifier, "supports_context", False):
            related_kwargs["base_context"] = base_context
            if not handoff.acceptance_criteria:
                return self._identity_failure(handoff, result.result_id, "缺少冻结验收契约，请重新规划；旧快照不能自动通过")
        if getattr(verifier, "supports_harness", False):
            related_kwargs["execution_context"] = execution_context
        try:
            report = verifier.verify(handoff=handoff, result=result, specialist_results=specialist_results, **related_kwargs)
        except (ModelOutputError, ContextBudgetExceeded) as error:
            report = failure_report(handoff, result, "model_response" if isinstance(error, ModelOutputError) else "budget",
                                    f"独立审核未完成：{type(error).__name__}；不能因此重新执行 Specialist")
            return self._with_basic(report.model_copy(update={"allowed_actions": ["stop"]}), basic)
        except Exception as error:
            if basic is None:
                raise  # 历史验证器保持原异常语义。
            report = failure_report(handoff, result, "model_response", f"独立审核请求失败：{type(error).__name__}；不代表 Specialist 交付失败")
            return self._with_basic(report.model_copy(update={"allowed_actions": ["stop"]}), basic)
        if (
            report.result_id != result.result_id
            or report.handoff_id != handoff.handoff_id
            or report.subject_agent != handoff.to_agent
            or report.todo_id is not None and report.todo_id != handoff.todo_id
        ):
            return self._identity_failure(
                handoff,
                result.result_id,
                "Verifier 返回了指向其他 Result/Handoff 的报告",
            )
        # 审核未完成时交由 Supervisor 决定是否披露限制；完整报告仍必须覆盖契约。
        if handoff.acceptance_criteria and report.verification_status == "assessed":
            if report.todo_id != handoff.todo_id or not assessment_structure_valid(
                    handoff.acceptance_criteria, report.assessments, report.evidence_records):
                if basic is not None:
                    report = failure_report(handoff, result, "model_response", "独立审核报告覆盖或引用不完整；不是 Specialist 交付失败，禁止因此重新委派")
                    return self._with_basic(report.model_copy(update={"allowed_actions": ["stop"]}), basic)
                return self._identity_failure(handoff, result.result_id, "验收覆盖或证据引用不完整")
        if not report.passed:
            category, retryable = report.failure_category or "validation", report.retryable
            if report.failure_category is None:
                retryable = True
            if isinstance(result, CodeResult) and result.tool_results and report.verification_status == "assessed":
                last = result.tool_results[-1]
                error = last.get("error") or {}
                if error.get("code") in {"permission_denied", "approval_rejected", "tool_not_exposed", "workspace_escape"}:
                    category, retryable = "permission", False
                elif last.get("ok") is False and report.failure_category is None:
                    category, retryable = "unknown", None
            report = report.model_copy(update={"failure_category": category, "retryable": retryable,
                "blocking_reason": report.blocking_reason or "; ".join(report.failures)})
        if isinstance(result, CodeResult) and delivery is None and (result.stop_reason or not result.completed):
            reason = result.stop_reason or "CodeAgent 未完成当前委派"
            last_error = (result.tool_results[-1].get("error") or {}) if result.tool_results else {}
            category = "permission" if last_error.get("code") in {"permission_denied", "approval_rejected", "tool_not_exposed", "workspace_escape"} else "budget" if result.stop_reason else "validation"
            report = report.model_copy(update={"passed": False, "failure_category": category,
                "retryable": category == "validation", "blocking_reason": reason, "failures": [*report.failures, reason]})
        if report.passed and handoff.acceptance_criteria:
            if report.todo_id != handoff.todo_id or not assessments_valid(handoff.acceptance_criteria, report.assessments, report.evidence_records):
                return self._identity_failure(handoff, result.result_id, "验收覆盖、证据引用或 Todo 关联不完整")
        if report.passed and isinstance(result, ResearchResult):
            provenance = ResearchResultVerifier().verify(handoff=handoff, result=result, specialist_results={})
            if not provenance.passed:
                report = report.model_copy(update={"passed": False, "failure_category": "insufficient_evidence",
                    "retryable": False, "blocking_reason": "来源不能追溯到真实搜索 Observation",
                    "failures": provenance.failures})
        return self._with_basic(report, basic)

    @staticmethod
    def _with_basic(report, basic):
        if basic is None:
            return report
        return report.model_copy(update={"checks": [*basic.checks, *report.checks],
            "limitations": list(dict.fromkeys([*basic.limitations, *report.limitations]))[:24],
            "verification_reason": basic.verification_reason})

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
            failure_category="identity",
            retryable=False,
            todo_id=handoff.todo_id,
            hard_blockers=[evidence],
        )
