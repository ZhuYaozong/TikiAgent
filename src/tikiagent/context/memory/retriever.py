"""Context Engine v1 的确定性 Retriever。"""

from tikiagent.context.memory.history import HistoryStore
from tikiagent.context.memory.models import HistoryRecord
from tikiagent.context.models import ContextProfile, ContextRequest


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
            if self._allowed(record, request, profile, explicit_ref=True):
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
        explicit_ids = set(request.context_refs)
        for record in candidates:
            if self._allowed(
                record,
                request,
                profile,
                explicit_ref=record.record_id in explicit_ids,
            ):
                unique.setdefault(record.record_id, record)
        return list(unique.values())[: policy.max_records]

    @staticmethod
    def _allowed(
        record: HistoryRecord | None,
        request: ContextRequest,
        profile: ContextProfile,
        *,
        explicit_ref: bool = False,
    ) -> bool:
        """显式引用可跨 Task，但永远不能跨 Session 或越过 Profile。"""

        return bool(
            record is not None
            and record.session_id == request.session_id
            and record.record_type in profile.allowed_record_types
            and (explicit_ref or record.task_id == request.task_id)
        )
