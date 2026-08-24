"""把验证通过的 Specialist Result 转换为用户可读最终回答。"""

from __future__ import annotations

from typing import TypeVar

from tikiagent.orchestration.models import CodeResult, ResearchResult, SpecialistName
from tikiagent.orchestration.state import TikiState


_ResultT = TypeVar("_ResultT", ResearchResult, CodeResult)


def compose_final_answer(state: TikiState) -> str:
    """只展示最新且具有匹配 PASS 的结果，不把内部身份 ID 暴露给用户。"""

    sections: list[str] = ["# 任务完成"]
    research = _verified_result(state, "research_agent", ResearchResult)
    code = _verified_result(state, "code_agent", CodeResult)
    if research is not None:
        sections.extend(_research_sections(research))
    if code is not None:
        sections.extend(_code_sections(code))
    if research is None and code is None:
        raise ValueError("没有可用于最终回答的已验证 Specialist Result")
    return "\n\n".join(sections)


def _verified_result(
    state: TikiState,
    specialist: SpecialistName,
    result_type: type[_ResultT],
) -> _ResultT | None:
    """在展示边界再次检查 Result/Verification 身份链。"""

    raw_result = state["specialist_results"].get(specialist)
    report = state["specialist_verifications"].get(specialist)
    if raw_result is None or report is None or not report.passed:
        return None
    if (
        report.subject_agent != specialist
        or report.result_id != raw_result.get("result_id")
        or report.handoff_id != raw_result.get("handoff_id")
    ):
        return None
    return result_type.model_validate(raw_result)


def _research_sections(result: ResearchResult) -> list[str]:
    sections = ["## 调研总结", result.summary.strip()]
    if result.findings:
        sections.extend(
            ["## 关键发现", "\n".join(f"- {item}" for item in result.findings)]
        )
    if result.sources:
        source_lines: list[str] = []
        seen_urls: set[str] = set()
        for source in result.sources:
            if source.url in seen_urls:
                continue
            seen_urls.add(source.url)
            source_lines.append(f"- **{source.title}** — <{source.url}>")
            if source.snippet.strip():
                source_lines.append(f"  {source.snippet.strip()}")
        sections.extend(["## 来源", "\n".join(source_lines)])
    if result.unresolved_questions:
        sections.extend(
            [
                "## 尚未解决的问题",
                "\n".join(f"- {item}" for item in result.unresolved_questions),
            ]
        )
    return sections


def _code_sections(result: CodeResult) -> list[str]:
    sections = ["## 执行结果", result.summary.strip()]
    if result.changed_files:
        sections.extend(
            ["## 交付文件", "\n".join(f"- `{path}`" for path in result.changed_files)]
        )
    if result.tests_run:
        sections.extend(
            ["## 验证", "\n".join(f"- `{test}`" for test in result.tests_run)]
        )
    return sections
