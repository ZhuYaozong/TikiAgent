"""把验证通过的 Specialist Result 转换为用户可读最终回答。"""

from __future__ import annotations

from typing import TypeVar

from tikiagent.orchestration.models import CodeResult, ResearchResult, SpecialistName
from tikiagent.orchestration.state import TikiState


_ResultT = TypeVar("_ResultT", ResearchResult, CodeResult)
MAX_FINAL_ANSWER_CHARS = 7600
_RESEARCH_SECTION_LIMIT = 5400
_CODE_SECTION_LIMIT = 1800


def compose_final_answer(state: TikiState) -> str:
    """只展示最新且具有匹配 PASS 的结果，不把内部身份 ID 暴露给用户。"""

    sections: list[str] = ["# 任务完成"]
    research = _verified_result(state, "research_agent", ResearchResult)
    code = _verified_result(state, "code_agent", CodeResult)
    if research is not None:
        sections.append(
            _bound_markdown(
                "\n\n".join(_research_sections(research)),
                _RESEARCH_SECTION_LIMIT,
            )
        )
    if code is not None:
        sections.append(
            _bound_markdown(
                "\n\n".join(_code_sections(code)),
                _CODE_SECTION_LIMIT,
            )
        )
    if research is None and code is None:
        raise ValueError("没有可用于最终回答的已验证 Specialist Result")
    return _bound_markdown("\n\n".join(sections), MAX_FINAL_ANSWER_CHARS)


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
    sections = ["## 调研总结", _shorten(result.summary.strip(), 1600)]
    if result.findings:
        sections.extend(
            [
                "## 关键发现",
                "\n".join(
                    f"- {_shorten(item, 360)}" for item in result.findings[:8]
                ),
            ]
        )
    if result.sources:
        source_lines: list[str] = []
        seen_urls: set[str] = set()
        for source in result.sources[:10]:
            if source.url in seen_urls:
                continue
            seen_urls.add(source.url)
            # 完整网页摘录属于 Research Evidence，不复制到最终回答与 Final History。
            source_lines.append(
                f"- **{_shorten(source.title, 140)}** — "
                f"<{_shorten(source.url, 500)}>"
            )
        sections.extend(["## 来源", "\n".join(source_lines)])
    if result.unresolved_questions:
        sections.extend(
            [
                "## 尚未解决的问题",
                "\n".join(
                    f"- {_shorten(item, 320)}"
                    for item in result.unresolved_questions[:4]
                ),
            ]
        )
    return sections


def _code_sections(result: CodeResult) -> list[str]:
    sections = ["## 执行结果", _shorten(result.summary.strip(), 1200)]
    if result.changed_files:
        sections.extend(
            [
                "## 交付文件",
                "\n".join(
                    f"- `{_shorten(path, 240)}`" for path in result.changed_files[:30]
                ),
            ]
        )
    if result.tests_run:
        sections.extend(
            [
                "## 验证",
                "\n".join(
                    f"- `{_shorten(test, 300)}`" for test in result.tests_run[:12]
                ),
            ]
        )
    return sections


def _shorten(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _bound_markdown(value: str, limit: int) -> str:
    """在完整行边界截断，确保 Final History 永远满足 8,000 字符契约。"""

    if len(value) <= limit:
        return value
    marker = "\n\n> 部分内容因最终回答长度限制已省略；完整证据仍保存在任务 History。"
    prefix = value[: limit - len(marker)]
    line_boundary = prefix.rfind("\n")
    if line_boundary > 0:
        prefix = prefix[:line_boundary]
    return prefix.rstrip() + marker
