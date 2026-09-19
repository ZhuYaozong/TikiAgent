"""Retriever 默认优先级与 Profile 补充策略测试。"""

from tikiagent.context.memory.history import InMemoryHistoryStore
from tikiagent.context.memory.models import HistoryRecord
from tikiagent.context.memory.retriever import Retriever
from tikiagent.context.models import ContextProfile, ContextRequest


def request(*, refs=None, keywords=None) -> ContextRequest:
    return ContextRequest(
        agent="code_agent",
        task_id="task-1",
        session_id="session-1",
        phase="repair",
        instruction="repair page",
        context_refs=refs or [],
        keywords=keywords or [],
    )


def profile(*, supplement_keyword=False, supplement_recent=False):
    return ContextProfile(
        agent="code_agent",
        role="code",
        system_rules=["rule"],
        allowed_record_types={"result", "verification"},
        retrieval={
            "supplement_keyword": supplement_keyword,
            "supplement_recent": supplement_recent,
            "max_records": 5,
        },
    )


def add_record(
    store: InMemoryHistoryStore,
    record_id: str,
    record_type: str,
    summary: str,
) -> None:
    store.append(
        HistoryRecord.model_validate(
            {
                "record_id": record_id,
                "task_id": "task-1",
                "session_id": "session-1",
                "record_type": record_type,
                "producer": "verifier",
                "summary": summary,
            }
        )
    )


def test_exact_refs_do_not_mix_old_keyword_match_by_default() -> None:
    store = InMemoryHistoryStore()
    add_record(store, "old-pass", "verification", "PASS comparison.html")
    add_record(store, "new-fail", "verification", "FAIL comparison.html")

    records = Retriever(store).retrieve(
        request(refs=["new-fail"], keywords=["comparison.html"]),
        profile(),
    )

    assert [item.record_id for item in records] == ["new-fail"]


def test_profile_can_supplement_exact_results_with_keyword_matches() -> None:
    store = InMemoryHistoryStore()
    add_record(store, "research-result", "result", "agent framework")
    add_record(store, "prior-result", "result", "previous framework source")

    records = Retriever(store).retrieve(
        request(refs=["research-result"], keywords=["framework"]),
        profile(supplement_keyword=True),
    )

    assert [item.record_id for item in records] == [
        "research-result",
        "prior-result",
    ]


def test_recent_is_only_fallback_unless_profile_requests_supplement() -> None:
    store = InMemoryHistoryStore()
    add_record(store, "result-1", "result", "first")
    add_record(store, "result-2", "result", "second")

    fallback = Retriever(store).retrieve(request(), profile())
    supplemented = Retriever(store).retrieve(
        request(refs=["result-1"]),
        profile(supplement_recent=True),
    )

    assert [item.record_id for item in fallback] == ["result-1", "result-2"]
    assert [item.record_id for item in supplemented] == [
        "result-1",
        "result-2",
    ]


def test_disallowed_record_type_is_not_returned_by_exact_ref() -> None:
    store = InMemoryHistoryStore()
    add_record(store, "verification-1", "verification", "failed")
    store.append(
        HistoryRecord(
            record_id="handoff-1",
            task_id="task-1",
            session_id="session-1",
            record_type="handoff",
            producer="supervisor",
            summary="delegate",
        )
    )

    records = Retriever(store).retrieve(
        request(refs=["handoff-1", "verification-1"]),
        profile(),
    )

    assert [item.record_id for item in records] == ["verification-1"]


def test_explicit_ref_can_reuse_previous_task_in_same_session() -> None:
    store = InMemoryHistoryStore()
    store.append(
        HistoryRecord(
            record_id="final:previous",
            task_id="task-previous",
            session_id="session-1",
            record_type="result",
            producer="supervisor",
            summary="上一轮调研结果",
        )
    )

    records = Retriever(store).retrieve(
        request(refs=["final:previous"]),
        profile(),
    )

    assert [item.record_id for item in records] == ["final:previous"]


def test_explicit_ref_cannot_cross_session() -> None:
    store = InMemoryHistoryStore()
    store.append(
        HistoryRecord(
            record_id="final:other-session",
            task_id="task-previous",
            session_id="session-other",
            record_type="result",
            producer="supervisor",
            summary="其他会话的私有结果",
        )
    )

    records = Retriever(store).retrieve(
        request(refs=["final:other-session"]),
        profile(),
    )

    assert records == []
