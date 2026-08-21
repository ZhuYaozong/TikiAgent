"""使用结构化输出进行规划和重试决策的 Planner。"""

from tikiagent.llm.models import StructuredModelClient
from tikiagent.orchestration.models import (
    Plan,
    PlannerDecision,
    PlanningResult,
    VerificationReport,
)


class PlannerAgent:
    """创建计划，并拥有 RETRY / FINISH / STOP 决策权。"""

    def __init__(self, model: StructuredModelClient) -> None:
        self.model = model

    def plan(self, task: str) -> PlanningResult:
        result = self.model.complete_structured(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是任务 Planner。生成可执行步骤和能够由环境"
                        "验证的验收标准，不要调用工具。首次 action 必须"
                        "是 execute。"
                    ),
                },
                {"role": "user", "content": task},
            ],
            response_type=PlanningResult,
        )
        if result.decision.action == "execute":
            return result

        # 模型输出仍受程序契约约束，首次规划不能跳过执行。
        return PlanningResult(
            plan=result.plan,
            decision=PlannerDecision(
                action="execute",
                instruction=(
                    result.decision.instruction
                    or self._default_instruction(result.plan)
                ),
                reason=(
                    "首次规划必须进入执行阶段。"
                    f"模型原理由：{result.decision.reason}"
                ),
            ),
        )

    def decide(
        self,
        *,
        task: str,
        plan: Plan,
        report: VerificationReport,
        attempts: int,
        max_attempts: int,
    ) -> PlannerDecision:
        if report.passed:
            return PlannerDecision(
                action="finish",
                instruction="",
                reason="Verifier 的全部环境检查通过",
            )

        if attempts >= max_attempts:
            return PlannerDecision(
                action="stop",
                instruction="",
                reason="验证失败且达到最大执行尝试次数",
            )

        decision = self.model.complete_structured(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是任务 Planner。Verifier 已报告失败。你不能"
                        "修改文件，只能依据证据生成下一轮 Actor 指令。"
                        "action 必须是 retry。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"任务：{task}\n"
                        f"计划：{plan.model_dump_json()}\n"
                        "验证报告："
                        f"{report.model_dump_json()}"
                    ),
                },
            ],
            response_type=PlannerDecision,
        )
        if decision.action == "retry":
            return decision

        # FAIL 报告不能被模型直接改写成 FINISH。
        return PlannerDecision(
            action="retry",
            instruction=(
                decision.instruction
                or "根据 VerificationReport 修复失败项并重新验证"
            ),
            reason=(
                "Verifier 未通过，系统把决策约束为 retry。"
                f"模型原理由：{decision.reason}"
            ),
        )

    @staticmethod
    def _default_instruction(plan: Plan) -> str:
        return "按照计划执行并验证：" + "；".join(plan.steps)
