"""执行与审批共享的身份作用域；不依赖执行结果模型。"""

from pydantic import BaseModel, ConfigDict, Field


class ExecutionScope(BaseModel):
    """工具执行和审批所属的任务、会话与工作区身份。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    task_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)


class ExecutionContext(BaseModel):
    """Harness 本轮执行上下文；不包含模型 Messages。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    scope: ExecutionScope
    agent: str = Field(min_length=1)
    exposed_tools: set[str] = Field(default_factory=set)
