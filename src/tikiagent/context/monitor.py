"""完整模型调用窗口的可替换 Token 估算与预算判断。"""

import json
from typing import Any, Protocol

from tikiagent.context.models import (
    CandidateModelCall,
    ContextBudget,
    ContextUsage,
)


class TokenEstimator(Protocol):
    """后续可替换为具体模型供应商的 Tokenizer。"""

    def estimate(self, value: Any) -> int: ...


class CharacterTokenEstimator:
    """确定性 v1 估算器：约四个字符计一个 Token。"""

    def estimate(self, value: Any) -> int:
        if value in (None, "", [], {}, ()):
            return 0
        text = (
            value
            if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False, default=str)
        )
        return max(1, (len(text) + 3) // 4)


class ContextMonitor:
    """分别统计来源，同时判断 Base、Local 和总输入预算。"""

    def __init__(self, estimator: TokenEstimator | None = None) -> None:
        self.estimator = estimator or CharacterTokenEstimator()

    def measure(
        self,
        candidate: CandidateModelCall,
        budget: ContextBudget,
    ) -> ContextUsage:
        memory = candidate.base_context.working_memory
        base_payload = {
            "agent": candidate.base_context.agent,
            "task": memory.task,
            "phase": memory.phase,
            "instruction": memory.instruction,
            "acceptance_criteria": memory.acceptance_criteria,
            "todos": [item.model_dump(mode="json") for item in memory.todos],
            "protected_refs": memory.protected_refs,
        }
        history_payload = {
            "summary": memory.history_summary,
            "records": [
                item.model_dump(mode="json")
                for item in memory.relevant_history
            ],
        }
        notepad_payload = [
            item.model_dump(mode="json") for item in memory.relevant_notepad
        ]
        prompt_tokens = self.estimator.estimate(candidate.prompt.render())
        base_tokens = self.estimator.estimate(base_payload)
        history_tokens = self.estimator.estimate(history_payload)
        notepad_tokens = self.estimator.estimate(notepad_payload)
        local_tokens = self.estimator.estimate(
            candidate.local_memory.model_dump(mode="json")
        )
        tool_schema_tokens = self.estimator.estimate(
            candidate.tool_view.schemas
        )
        response_schema_tokens = self.estimator.estimate(
            candidate.response_schema
        )
        total = sum(
            [
                prompt_tokens,
                base_tokens,
                history_tokens,
                notepad_tokens,
                local_tokens,
                tool_schema_tokens,
                response_schema_tokens,
            ]
        )
        base_total = base_tokens + history_tokens + notepad_tokens
        return ContextUsage(
            prompt_tokens=prompt_tokens,
            base_tokens=base_tokens,
            history_tokens=history_tokens,
            notepad_tokens=notepad_tokens,
            local_tokens=local_tokens,
            tool_schema_tokens=tool_schema_tokens,
            response_schema_tokens=response_schema_tokens,
            total_call_usage=total,
            available_input_budget=budget.available_input_budget,
            base_over_budget=base_total > budget.base_context_budget,
            local_over_budget=local_tokens > budget.local_messages_budget,
            total_over_budget=total > budget.available_input_budget,
        )
