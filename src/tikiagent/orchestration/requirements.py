"""委派前冻结的验收契约；模型不能通过遗漏检查取得通过。"""

from typing import Literal
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, model_validator

Capability = Literal["web_research", "workspace_read", "workspace_write", "python_environment", "command_execution"]
DeliveryMode = Literal["artifact", "inspection", "environment"]
VerificationLevel = Literal["basic", "independent"]
ReviewAction = Literal["accept", "accept_with_limitations", "request_changes", "stop"]


class ResultReview(BaseModel):
    """Supervisor 的验收事实，不能由 Verifier 的 passed 自动生成。"""

    model_config = ConfigDict(extra="forbid")
    review_id: str = Field(default_factory=lambda: str(uuid4()))
    todo_id: str
    result_id: str
    handoff_id: str
    verification_id: str
    action: ReviewAction
    reason: str = Field(min_length=1, max_length=600)
    limitations: list[str] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def validate_decision(self):
        if not self.reason.strip() or any(not s.strip() or len(s) > 500 for s in self.limitations):
            raise ValueError("验收理由和限制必须为有界的非空文本")
        if (self.action == "accept_with_limitations") != bool(self.limitations):
            raise ValueError("只有带限制接受必须且可以包含 limitations")
        return self


def assessment_structure_valid(criteria, assessments, evidence):
    """检查报告覆盖和引用真实性，不把内容缺陷当作结构错误。"""
    ids = [item.criterion_id for item in criteria]
    checked = [item.criterion_id for item in assessments]
    if not ids or len(set(ids)) != len(ids) or len(set(checked)) != len(checked) or set(ids) != set(checked):
        return False
    return all(
        not any(ref not in evidence for ref in check.evidence_refs)
        and (check.status != "passed" or bool(check.evidence_refs) and all(
            evidence[ref].get("usable") is True for ref in check.evidence_refs
        )) for check in assessments
    )


def review_blockers(todo, result, report):
    """不可覆盖的身份/证据边界；质量评价仍由 Supervisor 取舍。"""
    blockers = list(report.get("hard_blockers", []))
    if result.get("delivery_status") == "none":
        blockers.append("没有实际交付，不能接受为已完成")
    if (not todo.result_id or not todo.handoff_id or not todo.verification_id
            or result.get("result_id") != todo.result_id or result.get("handoff_id") != todo.handoff_id
            or report.get("verification_id") != todo.verification_id
            or report.get("result_id") != todo.result_id or report.get("handoff_id") != todo.handoff_id
            or report.get("subject_agent") != todo.owner):
        blockers.append("最新 Result/Handoff/Verification 身份不匹配")
    if report.get("failure_category") == "identity":
        blockers.append("身份或证据完整性检查未通过")
    if report.get("todo_id") != todo.todo_id:
        blockers.append("检查报告未关联当前 Todo")
    if report.get("verification_status") == "checks_only" and todo.verification_level == "independent":
        blockers.append("该 Todo 已冻结独立审核要求，不能用基础检查静默降级")
    if report.get("failure_category") == "permission" and report.get("verification_status") == "not_performed":
        blockers.append("权限阻塞且未形成可审核交付，不能声称已执行")
    if todo.acceptance_criteria and report.get("verification_status", "assessed") == "assessed":
        try:
            assessments = [CriterionAssessment.model_validate(a) for a in report.get("assessments", [])]
            if report.get("todo_id") != todo.todo_id or not assessment_structure_valid(todo.acceptance_criteria, assessments, report.get("evidence_records", {})):
                blockers.append("报告覆盖或证据引用不完整")
        except (ValueError, TypeError, AttributeError):
            blockers.append("报告结构不合法")
    return blockers


def review_limitations(todo, report):
    """保留未满足条件，不允许接受决定或最终摘要掩盖这些缺口。"""
    if report.get("verification_status") == "not_performed":
        return list(dict.fromkeys([*report.get("limitations", []), "审核未完成：" + str(report.get("blocking_reason") or "未取得完整报告")[:400]]))
    if report.get("verification_status") == "checks_only":
        # 未调用 LLM 不等于审核失败；仅机械检查的边界必须如实展示。
        return list(report.get("limitations", []))
    checks = {a.get("criterion_id"): a for a in report.get("assessments", [])}
    missing = [(f"未确认验收项 {c.criterion_id}：{c.description[:200]}；{str(checks.get(c.criterion_id, {}).get('reason', '证据不足'))[:200]}")[:500]
               for c in todo.acceptance_criteria if checks.get(c.criterion_id, {}).get("status") != "passed"]
    if not report.get("passed") and not missing:
        missing = ["审核保留意见：" + str(report.get("blocking_reason") or report.get("failures") or "未全部满足")[:400]]
    return list(dict.fromkeys([*report.get("limitations", []), *missing]))


def accepted_review_valid(todo, result, report):
    """FINISH 与展示共用同一验收边界，绝不回退到 passed 自动接受。"""
    review = todo.review
    if review is None or todo.status != "completed" or review.action not in {"accept", "accept_with_limitations"}:
        return False
    if (review.todo_id != todo.todo_id or review.result_id != todo.result_id
            or review.handoff_id != todo.handoff_id or review.verification_id != todo.verification_id
            or review_blockers(todo, result, report)):
        return False
    limitations = review_limitations(todo, report)
    return (not limitations if review.action == "accept" else bool(review.limitations)
            and set(limitations) <= set(review.limitations))


class AcceptanceCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    criterion_id: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=1500)
    required: bool = True


class CriterionAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    criterion_id: str = Field(min_length=1)
    status: Literal["passed", "failed", "insufficient_evidence"]
    evidence_refs: list[str] = Field(default_factory=list, max_length=20,
        description="passed 必须引用已读取且可用的真实 evidence_id；在 reason 里写来源 ID 不能代替本字段。failed/insufficient_evidence 可为空")
    reason: str = Field(min_length=1, max_length=1500)


def assessments_valid(criteria, assessments, evidence):
    """结构覆盖与真实证据引用的硬检查；不声称能证明模型语义判断正确。"""
    ids = [item.criterion_id for item in criteria]
    checked = [item.criterion_id for item in assessments]
    if not ids or not any(item.required for item in criteria) or len(set(ids)) != len(ids):
        return False
    if len(set(checked)) != len(checked) or set(ids) != set(checked):
        return False
    by_id = {item.criterion_id: item for item in assessments}
    for criterion in criteria:
        check = by_id[criterion.criterion_id]
        if any(ref not in evidence for ref in check.evidence_refs):
            return False
        if check.status == "passed" and (not check.evidence_refs or not all(
            evidence[ref].get("usable") is True for ref in check.evidence_refs
        )):
            return False
        if criterion.required and check.status != "passed":
            return False
    return True


def report_satisfies(todo, report):
    """FINISH 再检查持久化报告，防止仅凭 passed 布尔值越过契约。"""
    if not todo.acceptance_criteria:
        return True  # 旧规则工作流兼容；正式 Agent Gate 拒绝缺失新契约。
    try:
        checks = [CriterionAssessment.model_validate(item) for item in report.get("assessments", [])]
        return report.get("todo_id") == todo.todo_id and assessments_valid(todo.acceptance_criteria, checks, report.get("evidence_records", {}))
    except (ValueError, TypeError, AttributeError):
        return False
