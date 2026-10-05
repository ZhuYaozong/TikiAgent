"""共同收尾协议：只保留一次生成机会，不将总结成功当作验收成功。"""

from typing import Literal
import json

from pydantic import BaseModel, Field

from tikiagent.context.preparation import ContextRuntime
from tikiagent.tools.registry import RegisteredTool, ToolRegistry
from tikiagent.providers.llm.staged import at_stage


class DeliverySummary(BaseModel):
    summary: str = Field(min_length=1, max_length=6000)
    delivery_status: Literal["ready", "partial", "none"]


def final_context(runtime, **kwargs):
    """收尾使用规则压缩，不能暗中增加 LLM 摘要调用；硬条件超限仍拒绝。"""
    return ContextRuntime(budget=runtime.budget, profiles=runtime.prompt_assembler.profiles,
                          monitor=runtime.monitor, observer=runtime.observer,
                          budget_resolver=runtime.budget_resolver).prepare(**kwargs)


def complete_once(model, *, messages, tool_schemas):
    """生产适配器关闭网络和生成重试；离线测试模型遵守单次接口。"""
    return getattr(model, "complete_once", model.complete)(messages=messages, tool_schemas=tool_schemas)


def structured_once(model, *, messages, response_type):
    return getattr(model, "complete_structured_once", model.complete_structured)(
        messages=messages, response_type=response_type)


def summarize_run(model, runtime, context, local_memory, *, reason):
    """只接收 submit_result，不执行模型提出的任何工作工具。"""
    registry = ToolRegistry()
    registry.register(RegisteredTool("submit_result", "提交已有交付；ready 只是待验收声明，不代表 PASS",
                                     DeliverySummary, lambda **kwargs: kwargs))
    memory = context.working_memory.model_copy(update={
        "phase": "finalization",
        "runtime_budget": {**context.working_memory.runtime_budget, "finalization": True,
                           "remaining_rounds": 0, "remaining_tool_calls": 0},
        "instruction": context.working_memory.instruction + "\n执行已停止：" + reason
        + "。禁止继续工作，仅根据已观察证据调用 submit_result，说明已完成、未完成及阻塞；无证据不得声称完成。",
    })
    prepared = final_context(runtime, base_context=context.model_copy(update={"working_memory": memory}),
                             local_memory=local_memory, registry=registry)
    response = complete_once(at_stage(model, "code_final"), messages=prepared.messages, tool_schemas=prepared.tool_view.schemas)
    if len(response.tool_calls) == 1 and response.tool_calls[0].name == "submit_result":
        return DeliverySummary.model_validate(json.loads(response.tool_calls[0].arguments_json))
    # 普通文本不能提升 ready，仍可作为部分成果展示。
    if not response.tool_calls and response.final_text:
        return DeliverySummary(summary=response.final_text[:6000], delivery_status="partial")
    raise ValueError("收尾没有提交有效结果；未执行任何工作工具")
