"""FINISH Guard 之后的幂等任务收尾。"""

from typing import Any

from tikiagent.context.memory.history import HistoryConflictError, HistoryStore
from tikiagent.context.memory.models import HistoryRecord, NotepadEntry
from tikiagent.context.memory.notepad import NotepadStore
from tikiagent.context.models import FinalizationReport


class FinalizationService:
    """持久化最终事实；不删除 History、Result、TaskBoard 或 Artifact。"""

    def __init__(
        self,
        *,
        history_store: HistoryStore,
        notepad_store: NotepadStore,
    ) -> None:
        self.history_store = history_store
        self.notepad_store = notepad_store

    def finalize(
        self,
        *,
        task_id: str,
        session_id: str,
        final_result_id: str,
        final_result: str,
        refs: list[str],
        approved_notepad: list[NotepadEntry] | None = None,
        ephemeral_runtime: dict[str, Any] | None = None,
    ) -> FinalizationReport:
        existing = self.history_store.get_by_id(final_result_id)
        already_finalized = existing is not None
        expected = HistoryRecord(
            record_id=final_result_id,
            task_id=task_id,
            session_id=session_id,
            record_type="result",
            producer="supervisor",
            summary=final_result,
            payload={
                "final_result_id": final_result_id,
                "status": "completed",
            },
            refs=list(dict.fromkeys(refs)),
        )
        if existing is None:
            self.history_store.append(expected)
        else:
            normalized = expected.model_copy(update={"sequence": existing.sequence})
            if normalized != existing:
                raise HistoryConflictError(
                    f"final_result_id 冲突：{final_result_id}"
                )

        for entry in approved_notepad or []:
            self.notepad_store.append(entry)

        cleared = list(ephemeral_runtime or {})
        if ephemeral_runtime is not None:
            # 只清调用方明确交给 Finalization 的临时运行对象。
            ephemeral_runtime.clear()
        return FinalizationReport(
            task_id=task_id,
            final_result_id=final_result_id,
            history_record_id=final_result_id,
            already_finalized=already_finalized,
            cleared_runtime_fields=cleared,
        )
