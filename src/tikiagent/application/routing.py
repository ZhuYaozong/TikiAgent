"""CHAT / WORKFLOW 入口路由。"""

from __future__ import annotations

from typing import Protocol

from tikiagent.application.models import IntentDecision
from tikiagent.providers.llm.models import StructuredModelClient


class IntentRouter(Protocol):
    def route(self, user_input: str, *, has_history: bool) -> IntentDecision: ...


class RuleBasedIntentRouter:
    """确定性离线 Router，同时作为模型 Router 的安全 fallback。"""

    WORKFLOW_SIGNALS = (
        "搜索",
        "查询今天",
        "最新",
        "调研",
        "创建",
        "生成",
        "修改",
        "修复",
        "运行",
        "执行",
        "测试",
        "写入",
        "根据刚才",
    )
    CONCEPT_SIGNALS = ("什么是", "为什么", "有什么区别", "如何理解")
    EXECUTION_SIGNALS = ("请", "帮我", "替我", "开始", "然后")

    def route(self, user_input: str, *, has_history: bool) -> IntentDecision:
        normalized = user_input.casefold().strip()
        if not normalized:
            raise ValueError("user_input 不能为空")
        concept = any(item in normalized for item in self.CONCEPT_SIGNALS)
        execution = any(item in normalized for item in self.EXECUTION_SIGNALS)
        if concept and not execution:
            return IntentDecision(
                intent="CHAT",
                reason="识别为不要求实际执行工具的概念问答",
            )
        matches = [item for item in self.WORKFLOW_SIGNALS if item in normalized]
        if matches:
            suffix = "，可检索当前 Session 历史" if has_history else ""
            return IntentDecision(
                intent="WORKFLOW",
                reason=f"检测到需要工具或多步骤执行的动作{suffix}",
            )
        return IntentDecision(
            intent="CHAT",
            reason="未检测到需要进入任务执行系统的明确动作",
        )


class StructuredIntentRouter:
    """使用 OpenAI-compatible Structured Output 判断二元入口。"""

    def __init__(
        self,
        model: StructuredModelClient,
        *,
        fallback: IntentRouter | None = None,
    ) -> None:
        self.model = model
        self.fallback = fallback

    def route(self, user_input: str, *, has_history: bool) -> IntentDecision:
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 TikiAgent 的入口路由器，只判断 CHAT 或 WORKFLOW。"
                    "需要搜索、文件、代码、Shell、多步骤执行或引用先前结果继续操作时"
                    "选择 WORKFLOW；只需文本解释或普通对话时选择 CHAT。"
                    "不要规划任务，不要选择 Agent 或 Tool。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"has_session_history={has_history}\n"
                    f"user_input={user_input}"
                ),
            },
        ]
        try:
            return self.model.complete_structured(messages, IntentDecision)
        except (RuntimeError, ValueError):
            if self.fallback is None:
                raise
            return self.fallback.route(user_input, has_history=has_history)
