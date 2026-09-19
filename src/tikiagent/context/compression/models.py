"""模型窗口预算与分项用量。"""

from __future__ import annotations

from pydantic import Field, model_validator

from tikiagent.context.schema import ContextModel


class ContextBudget(ContextModel):
    """模型窗口预算；输入必须为输出预留生成空间。"""

    model_context_limit: int = Field(default=32_000, gt=0)
    reserved_output_tokens: int = Field(default=2_000, ge=0)
    base_context_budget: int = Field(default=12_000, gt=0)
    local_messages_budget: int = Field(default=10_000, gt=0)
    recent_interaction_limit: int = Field(default=4, gt=0)

    @model_validator(mode="after")
    def validate_output_reservation(self) -> ContextBudget:
        if self.reserved_output_tokens >= self.model_context_limit:
            raise ValueError("reserved_output_tokens 必须小于模型窗口")
        return self

    @property
    def available_input_budget(self) -> int:
        return self.model_context_limit - self.reserved_output_tokens


class ContextUsage(ContextModel):
    """Candidate 或 Prepared Model Call 的完整输入估算。"""

    prompt_tokens: int
    base_tokens: int
    history_tokens: int
    notepad_tokens: int
    local_tokens: int
    tool_schema_tokens: int
    response_schema_tokens: int
    total_call_usage: int
    available_input_budget: int
    base_over_budget: bool
    local_over_budget: bool
    total_over_budget: bool
