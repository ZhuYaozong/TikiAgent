"""统一研究证据目录：真实搜索、正文提取和显式授权的历史来源。"""

import hashlib
import re
from dataclasses import dataclass, field

from tikiagent.context.memory.models import HistoryRecord
from tikiagent.orchestration.contracts import ResearchObservation, ResearchResult, ResearchSource


def source_id(url: str) -> str:
    return "s-" + hashlib.sha256(url.encode()).hexdigest()[:12]


def provenance_valid(result: ResearchResult) -> bool:
    """旧数据只检查已有事实；新引用必须能对应到实际来源和结论。"""
    pairs = {(o.observation_id, url) for o in result.observations for url in o.urls}
    kinds = {(o.observation_id, url): o.kind for o in result.observations for url in o.urls}
    ids = {s.source_id for s in result.sources if s.source_id}
    indices = [c.finding_index for c in result.finding_citations]
    return all((s.observation_id, s.url) in pairs and (
        s.source_id is None or s.source_id == source_id(s.url)) and
        s.evidence_kind == kinds[(s.observation_id, s.url)] for s in result.sources) and (
        not indices or len(set(indices)) == len(indices) and set(indices) == set(range(len(result.findings)))
    ) and all(
        c.finding_index < len(result.findings) and set(c.source_ids) <= ids
        for c in result.finding_citations
    )


def authorized_history(records, *, handoff, session_id):
    """只接受显式引用的同 Session 原始 Result；软检索摘要不能升级为证据。"""
    selected = []
    for record in records:
        if (record.record_id not in handoff.context_refs or record.session_id != session_id
                or record.record_type != "result" or record.producer != "research_agent"):
            continue
        try:
            result = ResearchResult.model_validate(record.payload)
        except ValueError:
            continue
        if (result.result_id != record.record_id or result.handoff_id not in record.refs
                or not provenance_valid(result)):
            continue
        selected.append((record, result))
    return selected


def relevant_excerpt(content: str, instruction: str, limit: int = 1400) -> str:
    """保持原文片段，优先保留日期及任务关键词附近内容，不编造摘要或日期。"""
    if len(content) <= limit:
        return content
    terms = set(re.findall(r"[A-Za-z0-9_-]{3,}|[\u4e00-\u9fff]{2,6}", instruction.casefold()))
    windows = [(0, 220)]
    # 日期窗口优先，避免页头/菜单占满摘录后丢失正文发布日期。
    for match in list(re.finditer(r"\b20\d{2}[-/年.]\d{1,2}[-/月.]\d{1,2}|published|发布时间|发布日期", content, re.I))[:4]:
        windows.append((max(0, match.start() - 100), min(len(content), match.end() + 200)))
    blocks = [(m.start(), m.end(), m.group()) for m in re.finditer(r"[^\n]+", content)]
    ranked = sorted(blocks, key=lambda b: sum(t in b[2].casefold() for t in terms), reverse=True)
    for start, end, text in ranked[:4]:
        if any(t in text.casefold() for t in terms):
            windows.append((start, min(end, start + 420)))
    windows.append((max(0, len(content) - 300), len(content)))
    parts, used, remaining = [], [], limit
    for start, end in windows:
        if remaining < 30 or any(start >= a and end <= b for a, b in used):
            continue
        part = content[start:end][:remaining - 8]
        parts.append(part)
        used.append((start, start + len(part)))
        remaining -= len(part) + 8
    return "\n[…]\n".join(parts)[:limit]


@dataclass
class ResearchEvidence:
    """模型只能引用目录 ID；URL、日期与来源身份均由程序回填。"""

    observations: list[ResearchObservation] = field(default_factory=list)
    sources: dict[str, ResearchSource] = field(default_factory=dict)
    contents: dict[str, str] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)

    def import_history(self, records: list[HistoryRecord], *, handoff, session_id):
        for record, result in authorized_history(records, handoff=handoff, session_id=session_id):
            for source in result.sources:
                oid = f"history:{record.record_id}:{source.observation_id}"
                if not any(o.observation_id == oid for o in self.observations):
                    self.observations.append(ResearchObservation(observation_id=oid, query="显式复用历史来源",
                        urls=[s.url for s in result.sources if s.observation_id == source.observation_id],
                        kind="history", history_record_id=record.record_id, original_observation_id=source.observation_id))
                self.sources[source.url] = source.model_copy(update={
                    "source_id": source_id(source.url), "observation_id": oid, "evidence_kind": "history"})

    def observe(self, result) -> bool | None:
        """返回内容是否有增量；None 表示不是可识别的联网响应。"""
        if not result.ok:
            if result.tool_name == "web_extract":
                self.issues.append(f"正文提取未成功：{result.error.code if result.error else 'unknown'}")
            return False
        output = result.output
        if not isinstance(output, dict):
            return None
        if result.tool_name == "web_search" and isinstance(output.get("query"), str) and isinstance(output.get("results"), list):
            changed, urls = False, []
            for raw in output["results"]:
                if not isinstance(raw, dict) or not isinstance(raw.get("url"), str) or not raw["url"].strip():
                    continue
                url = raw["url"]
                urls.append(url)
                old = self.sources.get(url)
                date = raw.get("published_date") if isinstance(raw.get("published_date"), str) else None
                title, snippet = str(raw.get("title") or url), str(raw.get("snippet") or "")
                changed |= old is None or (date is not None and date != old.published_date) or bool(snippet and snippet != old.snippet and old.evidence_kind == "search")
                if old is None or old.evidence_kind == "search":
                    self.sources[url] = ResearchSource(observation_id=result.tool_call_id, title=title, url=url,
                        snippet=snippet, source_id=source_id(url), published_date=date or (old.published_date if old else None))
                elif date and not old.published_date:
                    self.sources[url] = old.model_copy(update={"published_date": date})
            self.observations.append(ResearchObservation(observation_id=result.tool_call_id, query=output["query"], urls=urls))
            return changed
        if result.tool_name == "web_extract" and isinstance(output.get("url"), str) and isinstance(output.get("content"), str) and output["content"].strip():
            url, content = output["url"], output["content"]
            old = self.sources.get(url)
            changed = self.contents.get(url) != content
            self.contents[url] = content
            date = output.get("published_date") if isinstance(output.get("published_date"), str) else None
            self.sources[url] = ResearchSource(observation_id=result.tool_call_id, url=url,
                title=str(output.get("title") or (old.title if old else url)), snippet=content,
                source_id=source_id(url), evidence_kind="extract", published_date=date or (old.published_date if old else None))
            self.observations.append(ResearchObservation(observation_id=result.tool_call_id, query="直接网页提取", urls=[url], kind="extract"))
            if output.get("truncated"):
                self.issues.append(f"网页正文已截断：{url[:160]}")
            return changed
        return None

    def catalog(self, instruction: str, limit: int = 12):
        # 正文证据优先，其次显式历史；其他搜索项按任务相关性排序而非前 N 个。
        terms = set(re.findall(r"[A-Za-z0-9_-]{3,}|[\u4e00-\u9fff]{2,6}", instruction.casefold()))
        def rank(source):
            text = f"{source.title} {source.snippet}".casefold()
            return (source.evidence_kind == "extract", source.evidence_kind == "history",
                    sum(term in text for term in terms))
        selected = sorted(self.sources.values(), key=rank, reverse=True)[:limit]
        return {s.source_id: s.model_copy(update={"snippet": relevant_excerpt(s.snippet, instruction)}) for s in selected}

    @staticmethod
    def view(catalog):
        return [{"source_id": sid, "url": s.url, "title": s.title[:200],
                 "published_date": s.published_date, "evidence_kind": s.evidence_kind,
                 "excerpt": s.snippet} for sid, s in catalog.items()]
