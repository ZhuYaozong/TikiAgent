"""应用层跨 Turn Context 引用选择。"""

from pathlib import Path

from tikiagent.context.memory.history import JsonlHistoryStore


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
        # 仅扩展已授权最终结果的显式来源链；同 Session、有限深度/数量，禁止全库放开。
        selected = records[-self.limit :] if self.limit > 0 else []
        refs = [record.record_id for record in selected]
        for _ in range(2):
            previous = list(refs)
            for ref in previous:
                record = history.get_by_id(ref)
                if record is None:
                    continue
                for linked in record.refs:
                    source = history.get_by_id(linked)
                    if (source is not None and source.session_id == session_id and linked not in refs
                            and source.record_type in {"handoff", "result", "verification", "review"} and len(refs) < 24):
                        refs.append(linked)
        return refs
