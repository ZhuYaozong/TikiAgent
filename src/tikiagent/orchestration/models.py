"""Control Plane 节点之间传递的结构化模型。"""

from typing import Any, Literal
from uuid import uuid4

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
    """Verifier 的报告；v0.4 工作流会填写 Result/Handoff 关联。"""

    verification_id: str = Field(default_factory=lambda: str(uuid4()))
    result_id: str | None = None
    handoff_id: str | None = None
    subject_agent: Literal["research_agent", "code_agent"] | None = None
    mode: Literal["rules", "environment"] = "environment"
    passed: bool
    checks: list[VerificationCheck]
    failures: list[str]
    evidence: list[str]
    recommendation: str


SpecialistName = Literal["research_agent", "code_agent"]
AgentName = Literal["supervisor", "research_agent", "code_agent"]


class SupervisorPlan(ControlModel):
    """Supervisor 首次理解任务后生成的路由计划。"""

    goal: str = Field(min_length=1)
    required_specialists: list[SpecialistName] = Field(min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1)


class SupervisorDecision(ControlModel):
    """Supervisor 对下一条 Graph 路由边的结构化决定。"""

    action: Literal["delegate", "finish", "stop"]
    target_agent: SpecialistName | None
    instruction: str
    reason: str = Field(min_length=1)
    context_refs: list[str] = Field(default_factory=list)


class ResearchSource(ControlModel):
    """能够追溯到一次真实 Web Observation 的来源。"""

    observation_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: str = Field(min_length=1)
    snippet: str


class ResearchObservation(ControlModel):
    """ResearchAgent 对外保留的最小搜索证据，不包含内部消息。"""

    observation_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    urls: list[str]


class ResearchResult(ControlModel):
    """ResearchAgent 对外提供的结构化 Result。"""

    result_id: str = Field(default_factory=lambda: str(uuid4()))
    handoff_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    findings: list[str] = Field(default_factory=list)
    sources: list[ResearchSource] = Field(default_factory=list)
    observations: list[ResearchObservation] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)


class CodeResult(ControlModel):
    """CodeAgent 对外提供的结构化 Result。"""

    result_id: str = Field(default_factory=lambda: str(uuid4()))
    handoff_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    completed: bool
    steps: int = Field(ge=0)
    changed_files: list[str] = Field(default_factory=list)
    tests_run: list[str] = Field(default_factory=list)
    context_refs_used: list[str] = Field(default_factory=list)
    tool_results: tuple[dict[str, Any], ...] = ()


class Handoff(ControlModel):
    """一次显式委派；完成后记录对应的最新 result_id。"""

    handoff_id: str = Field(default_factory=lambda: str(uuid4()))
    from_agent: AgentName
    to_agent: SpecialistName
    instruction: str = Field(min_length=1)
    context_refs: list[str] = Field(default_factory=list)
    result_id: str | None = None
    status: Literal["pending", "completed", "failed"] = "pending"
