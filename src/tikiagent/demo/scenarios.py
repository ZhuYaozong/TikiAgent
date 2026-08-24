"""v1 三类主场景；任务文字同时约束范围和可验证产物。"""

from __future__ import annotations

from datetime import date

from tikiagent.demo.models import DemoScenario, ScenarioName


def get_scenario(name: ScenarioName, *, today: date | None = None) -> DemoScenario:
    current = today or date.today()
    stamp = current.isoformat()
    scenarios = {
        "research": DemoScenario(
            name="research",
            title="Research：近期 AI Agent 变化",
            task=(
                f"今天是 {stamp}。仅进行 Web Research，不修改任何文件。"
                "调研近期 AI Agent 领域的重要变化，给出简洁总结；"
                "每条关键结论必须包含可追溯的来源 URL，并区分事实与推断。"
            ),
            expected_agents=["research_agent"],
            acceptance_criteria=[
                "至少包含一个来源 URL",
                "关键结论具有来源归属",
                "不创建或修改 Workspace 文件",
            ],
            expected_artifacts=[],
        ),
        "coding": DemoScenario(
            name="coding",
            title="Coding：可测试的计算器",
            task=(
                "在 Workspace 根目录创建 calculator.py 和 test_calculator.py。"
                "calculator.py 实现 add、subtract、multiply、divide；"
                "divide 遇到除数为 0 时抛出 ValueError。"
                "测试只使用 Python 标准库 unittest，并实际运行测试直到通过。"
            ),
            expected_agents=["code_agent"],
            acceptance_criteria=[
                "calculator.py 位于 Workspace 根目录",
                "test_calculator.py 位于 Workspace 根目录",
                "标准库 unittest 测试通过",
            ],
            expected_artifacts=["calculator.py", "test_calculator.py"],
        ),
        "hybrid": DemoScenario(
            name="hybrid",
            title="Hybrid：调研驱动的框架对比网页",
            task=(
                f"今天是 {stamp}。先调研近期主流 Agent Framework 的重要变化，"
                "再根据调研结果在 Workspace 根目录生成 comparison.html。"
                "网页必须包含清晰的框架对比、关键结论和对应来源 URL；"
                "ResearchAgent 的内部 messages 不得直接传给 CodeAgent。"
            ),
            expected_agents=["research_agent", "code_agent"],
            acceptance_criteria=[
                "先形成带来源的 ResearchResult",
                "comparison.html 位于 Workspace 根目录",
                "网页包含可见对比内容和来源链接",
            ],
            expected_artifacts=["comparison.html"],
        ),
    }
    return scenarios[name]
