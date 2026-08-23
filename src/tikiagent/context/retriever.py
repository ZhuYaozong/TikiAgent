"""Context Engine v1 的确定性 Retriever。"""

from tikiagent.context.history import HistoryStore
from tikiagent.context.models import (
    ContextProfile,
    ContextRequest,
    HistoryRecord,
)


class Retriever:
    """默认 exact → keyword → recent，并允许 Profile 少量补充。"""

    def __init__(self, history: HistoryStore) -> None:
        self.history = history

    def retrieve(
        self,
        request: ContextRequest,
        profile: ContextProfile,
    ) -> list[HistoryRecord]:
        candidates: list[HistoryRecord] = []

        # 精确引用是当前控制流给出的权威关联，始终最优先。
        for record_id in request.context_refs:
            record = self.history.get_by_id(record_id)
            if self._allowed(record, request, profile):
                candidates.append(record)

        policy = profile.retrieval
        if not candidates or policy.supplement_keyword:
            candidates.extend(
                self.history.search_keyword(
                    task_id=request.task_id,
                    session_id=request.session_id,
                    keywords=request.keywords,
                    record_types=profile.allowed_record_types,
                    limit=policy.keyword_limit,
                )
            )

        if not candidates or policy.supplement_recent:
            candidates.extend(
                self.history.recent(
                    task_id=request.task_id,
                    session_id=request.session_id,
                    record_types=profile.allowed_record_types,
                    limit=policy.recent_limit,
                )
            )

        unique: dict[str, HistoryRecord] = {}
        for record in candidates:
            if self._allowed(record, request, profile):
                unique.setdefault(record.record_id, record)
        return list(unique.values())[: policy.max_records]

    @staticmethod
    def _allowed(
        record: HistoryRecord | None,
        request: ContextRequest,
        profile: ContextProfile,
    ) -> bool:
        return bool(
            record is not None
            and record.task_id == request.task_id
            and record.session_id == request.session_id
            and record.record_type in profile.allowed_record_types
        )
