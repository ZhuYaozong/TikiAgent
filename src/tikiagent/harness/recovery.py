"""未知副作用执行的人工恢复决定。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tikiagent.harness.models import ToolResult


class RecoveryDecision(BaseModel):
    """人工确认 handler 在崩溃前是否产生过副作用。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["confirmed_not_executed", "confirmed_executed"]
    decided_by: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ReconcileResult(BaseModel):
    """confirmed_executed 后由人工提供的真实结果与证据。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    result: ToolResult
    reconciled_by: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_evidence(self) -> "ReconcileResult":
        if not all(item.strip() for item in self.evidence):
            raise ValueError("reconcile evidence 不能为空")
        return self
