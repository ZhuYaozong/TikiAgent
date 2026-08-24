"""Demo Validation 的稳定输入与输出模型。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


ScenarioName = Literal["research", "coding", "hybrid"]


class DemoModel(BaseModel):
    """Demo 模型统一拒绝未知字段，避免汇总格式静默漂移。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class DemoScenario(DemoModel):
    name: ScenarioName
    title: str = Field(min_length=1)
    task: str = Field(min_length=1)
    expected_agents: list[str]
    acceptance_criteria: list[str]
    expected_artifacts: list[str]


class ApplicationDigest(DemoModel):
    event_count: int = Field(ge=0)
    event_types: dict[str, int]
    agents_observed: list[str]
    tool_calls: int = Field(ge=0)
    approvals: int = Field(ge=0)
    verification_passed: int = Field(ge=0)
    verification_failed: int = Field(ge=0)


class TraceDigest(DemoModel):
    event_count: int = Field(ge=0)
    event_types: dict[str, int]
    trace_ref: str | None = None


class DemoRunSummary(DemoModel):
    demo_run_id: str = Field(min_length=1)
    scenario: ScenarioName
    task: str = Field(min_length=1)
    status: str = Field(min_length=1)
    started_at: datetime
    finished_at: datetime
    elapsed_seconds: float = Field(ge=0)
    session_id: str = Field(min_length=1)
    task_id: str | None = None
    run_id: str | None = None
    application: ApplicationDigest
    trace: TraceDigest
    artifacts: list[str]
    final_message: str = Field(min_length=1)
    next_action: str | None = None


class DemoRunResult(DemoModel):
    summary: DemoRunSummary
    output_dir: Path
