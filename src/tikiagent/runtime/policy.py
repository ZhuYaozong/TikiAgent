"""统一的应用级执行预算；与供应商最大窗口区分，配置不得隐式扩容。"""
import os
from pydantic import BaseModel, Field


class AgentPolicy(BaseModel):
    supervisor_steps: int = Field(default=20, ge=1)
    supervisor_tools: int = Field(default=32, ge=1)
    research_steps: int = Field(default=10, ge=1)
    research_searches: int = Field(default=4, ge=1)
    research_extracts: int = Field(default=6, ge=1)
    code_steps: int = Field(default=16, ge=1)
    code_tools: int = Field(default=32, ge=1)
    verifier_steps: int = Field(default=6, ge=1)
    verifier_tools: int = Field(default=10, ge=1)
    delegations: int = Field(default=5, ge=1)
    task_code_tools: int = Field(default=64, ge=1)
    task_web_tools: int = Field(default=20, ge=1)
    model_requests: int = Field(default=128, ge=16)
    repeat_limit: int = Field(default=2, ge=1)

    @classmethod
    def from_env(cls):
        return cls(**{key: int(os.environ[f"TIKI_BUDGET_{key.upper()}"])
                      for key in cls.model_fields if f"TIKI_BUDGET_{key.upper()}" in os.environ})


OUTPUT_LIMITS = {"router": 2048, "chat": 8192, "supervisor": 16384,
    "code_agent": 32768, "code_final": 16384, "research_agent": 16384,
    "research_final": 16384, "verifier": 16384, "verifier_final": 16384, "summary": 8192}

# 仅在 DeepSeek 适配下发送；通用兼容接口不会被强加供应商参数。
THINKING_EFFORTS = {"router": "none", "chat": "low", "supervisor": "high",
    "code_agent": "high", "code_final": "low", "research_agent": "low",
    "research_final": "low", "verifier": "high", "verifier_final": "low", "summary": "low"}


def thinking_effort(stage):
    """阶段推理强度可显式覆盖；none 表示禁用思考而非发送未知 effort。"""
    value = os.getenv(f"TIKI_THINKING_{stage.upper()}", THINKING_EFFORTS[stage])
    if value not in {"none", "low", "high", "max"}:
        raise ValueError(f"TIKI_THINKING_{stage.upper()} 必须为 none/low/high/max")
    return value


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
