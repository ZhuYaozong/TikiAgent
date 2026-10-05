"""把验证通过的 Specialist Result 转换为用户可读最终回答。"""

from __future__ import annotations

from typing import TypeVar

from tikiagent.orchestration.contracts import CodeResult, ResearchResult, SpecialistName
from tikiagent.orchestration.state import TikiState
from tikiagent.orchestration.requirements import accepted_review_valid


_ResultT = TypeVar("_ResultT", ResearchResult, CodeResult)
MAX_FINAL_ANSWER_CHARS = 7600
_RESEARCH_SECTION_LIMIT = 5400
_CODE_SECTION_LIMIT = 1800


def compose_final_answer(state: TikiState) -> str:
    """只展示最新且具有匹配 PASS 的结果，不把内部身份 ID 暴露给用户。"""

    sections: list[str] = ["# 任务完成"]
    todos = list(state["task_board"].items.values())
    if state.get("requires_supervisor_review", False):
        return _reviewed_answer(state, todos)
    if state.get("results_by_id") and len(todos) > len({t.owner for t in todos}):
        # 多 Todo 逐项展示，不能只返回每个 Agent 最后一次交付。
        share = max(200, 6800 // max(1, len(todos)))
        for todo in todos:
            raw = state["results_by_id"].get(todo.result_id, {})
            report = state.get("verifications_by_id", {}).get(todo.verification_id, {})
            if (todo.status != "completed" or not report.get("passed")
                    or report.get("result_id") != todo.result_id or report.get("handoff_id") != todo.handoff_id
                    or report.get("subject_agent") != todo.owner or raw.get("handoff_id") != todo.handoff_id):
                raise ValueError("Todo 缺少匹配的已验证结果")
            result = ResearchResult.model_validate(raw) if todo.owner == "research_agent" else CodeResult.model_validate(raw)
            content = _research_sections(result) if isinstance(result, ResearchResult) else _code_sections(result)
            sections.append(_bound_markdown("\n\n".join([f"## {todo.description}", *content]), share))
        return _bound_markdown("\n\n".join(sections), MAX_FINAL_ANSWER_CHARS)
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


def _reviewed_answer(state, todos):
    """质量取舍不抹除事实：先披露限制，再展示已接受交付。"""
    if not todos:
        raise ValueError("没有经过 Supervisor 验收的交付")
    for todo in todos:
        raw = state.get("results_by_id", {}).get(todo.result_id, {})
        report = state.get("verifications_by_id", {}).get(todo.verification_id, {})
        if not accepted_review_valid(todo, raw, report):
            raise ValueError("最新结果缺少匹配的 Supervisor 接受决定")
    limited = [t for t in todos if t.review.action == "accept_with_limitations"]
    sections = ["# 任务交付（含限制）" if limited else "# 任务完成"]
    basic_count = sum(state["verifications_by_id"][t.verification_id].get("verification_status") == "checks_only" for t in todos)
    if basic_count:
        sections.append(f"验收方式：由 Supervisor 验收；{basic_count} 项仅完成基础检查，未进行独立 LLM 审核。")
    if limited:
        sections.append("## 验收限制\n以下交付由 Supervisor 带限制接受，不代表原始验收条件全部满足。")
        share = max(120, 3400 // len(limited))
        for todo in limited:
            # 限制区优先保留；长列表显式给出数量与原文引用，不被成果摘要挤掉。
            text = f"### {todo.description[:120]}\n理由：{todo.review.reason}\n" + "\n".join(f"- {s}" for s in todo.review.limitations)
            sections.append(_bound_markdown(text, share))
            if len(text) > share:
                sections.append(f"限制共 {len(todo.review.limitations)} 项；完整验收记录：`{todo.review.review_id}`（History）。")
    available = MAX_FINAL_ANSWER_CHARS - len("\n\n".join(sections)) - 200
    share = max(100, available // len(todos) - 4)
    for todo in todos:
        raw = state["results_by_id"][todo.result_id]
        result = ResearchResult.model_validate(raw) if todo.owner == "research_agent" else CodeResult.model_validate(raw)
        content = _research_sections(result) if isinstance(result, ResearchResult) else _code_sections(result)
        sections.append(_bound_markdown("\n\n".join([f"## {todo.description[:120]}", *content]), share))
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
