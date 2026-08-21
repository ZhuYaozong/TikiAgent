"""Control Plane 节点之间传递的结构化模型。"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ControlModel(BaseModel):
    """拒绝模型生成未声明字段，防止静默污染状态。"""

    model_config = ConfigDict(extra="forbid")


class Plan(ControlModel):
    """Planner 生成的目标、步骤与环境验收标准。"""

    goal: str = Field(min_length=1)
    steps: list[str] = Field(min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1)


class PlannerDecision(ControlModel):
    """Planner 对下一条控制边的结构化决定。"""

    action: Literal["execute", "retry", "finish", "stop"]
    instruction: str
    reason: str = Field(min_length=1)


class PlanningResult(ControlModel):
    """首次规划同时返回 Plan 与执行决定。"""

    plan: Plan
    decision: PlannerDecision


class ActorResult(ControlModel):
    """Actor 隔离内部消息后交给 Verifier 的 Handoff。"""

    completed: bool
    summary: str = Field(min_length=1)
    steps: int = Field(ge=0)
    tool_results: tuple[dict[str, Any], ...] = ()


class VerificationCheck(ControlModel):
    """Verifier 执行的一项客观环境检查。"""

    name: str = Field(min_length=1)
    passed: bool
    evidence: str


class VerificationReport(ControlModel):
    """Verifier 的报告；不包含路由或修复权限。"""

    passed: bool
    checks: list[VerificationCheck]
    failures: list[str]
    evidence: list[str]
    recommendation: str
