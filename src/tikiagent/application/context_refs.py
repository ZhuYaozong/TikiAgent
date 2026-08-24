"""应用层跨 Turn Context 引用选择。"""

from pathlib import Path

from tikiagent.context import JsonlHistoryStore


class SessionContextReferenceProvider:
    """只选择同 Session 已 Finalize 的结果，不复制 Specialist messages。"""

    def __init__(self, history_root: str | Path, *, limit: int = 4) -> None:
        self.history_root = Path(history_root).resolve()
        self.limit = limit

    def select(self, session_id: str) -> list[str]:
        path = self.history_root / f"{session_id}.jsonl"
        history = JsonlHistoryStore(path)
        records = [
            record
            for record in history.list_records(session_id=session_id)
            if record.record_type == "result"
            and record.producer == "supervisor"
            and record.record_id.startswith("final:")
        ]
        return [record.record_id for record in records[-self.limit :]]
