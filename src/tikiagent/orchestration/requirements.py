"""委派前冻结的验收契约；模型不能通过遗漏检查取得通过。"""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

Capability = Literal["web_research", "workspace_read", "workspace_write", "python_environment", "command_execution"]
DeliveryMode = Literal["artifact", "inspection", "environment"]


class AcceptanceCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    criterion_id: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=1500)
    required: bool = True


class CriterionAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    criterion_id: str = Field(min_length=1)
    status: Literal["passed", "failed", "insufficient_evidence"]
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)
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
