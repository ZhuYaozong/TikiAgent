"""统一的应用级执行预算；与供应商最大窗口区分，配置不得隐式扩容。"""
import os
from pydantic import BaseModel, Field


class AgentPolicy(BaseModel):
    supervisor_steps: int = Field(default=12, ge=1)
    supervisor_tools: int = Field(default=20, ge=1)
    research_steps: int = Field(default=4, ge=1)
    research_searches: int = Field(default=2, ge=1)
    research_extracts: int = Field(default=2, ge=1)
    code_steps: int = Field(default=12, ge=1)
    code_tools: int = Field(default=24, ge=1)
    verifier_steps: int = Field(default=4, ge=1)
    verifier_tools: int = Field(default=6, ge=1)
    delegations: int = Field(default=5, ge=1)
    task_code_tools: int = Field(default=48, ge=1)
    task_web_tools: int = Field(default=6, ge=1)
    model_requests: int = Field(default=64, ge=16)
    repeat_limit: int = Field(default=2, ge=1)

    @classmethod
    def from_env(cls):
        return cls(**{key: int(os.environ[f"TIKI_BUDGET_{key.upper()}"])
                      for key in cls.model_fields if f"TIKI_BUDGET_{key.upper()}" in os.environ})


OUTPUT_LIMITS = {"router": 512, "chat": 2048, "supervisor": 3072,
    "code_agent": 8192, "code_final": 3072, "research_agent": 2048,
    "research_final": 4096, "verifier": 2048, "verifier_final": 4096, "summary": 2048}


def output_limit(stage):
    """阶段覆盖优先于旧全局配置；旧全局配置只作为未分阶段客户端的默认值。"""
    value = int(os.getenv(f"TIKI_OUTPUT_{stage.upper()}", OUTPUT_LIMITS[stage]))
    if value < 1:
        raise ValueError("阶段输出预算必须大于零")
    return value


def stage_for(agent, phase):
    if agent == "research_agent" and phase == "research_synthesis":
        return "research_final"
    if agent == "code_agent" and phase == "finalization":
        return "code_final"
    if agent == "verifier" and phase == "verification_finalization":
        return "verifier_final"
    return agent
