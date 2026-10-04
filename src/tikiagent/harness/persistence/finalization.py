"""收尾请求的持久化消费凭据，不伪造工具 Checkpoint，也不自动重发未知请求。"""

from pathlib import Path
from uuid import uuid4
import hashlib
import json
import os


class FinalizationLedger:
    """claim 先于模型调用持久化；进程重启后同一身份仍最多消费一次。"""

    def __init__(self, root=None):
        self.root = Path(root) if root is not None else None
        self.claimed = set()
        if self.root is not None:
            self.root.mkdir(parents=True, exist_ok=True)

    def claim(self, identity: str, reason: str) -> bool:
        key = hashlib.sha256(identity.encode()).hexdigest()
        if self.root is None:
            if key in self.claimed:
                return False
            self.claimed.add(key)
            return True
        path = self.root / f"{key}.json"
        try:
            # 排他创建避免两个 Runtime 同时消费；不完整文件也按已消费保守处理。
            with path.open("x", encoding="utf-8") as stream:
                json.dump({"identity": identity, "stage": "claimed", "reason": reason}, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            return False
        return True

    def finish(self, identity: str, status: str, diagnostics=None) -> None:
        if self.root is None:
            return
        key = hashlib.sha256(identity.encode()).hexdigest()
        path = self.root / f"{key}.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value.update(stage="finished", outcome=status)
        value["diagnostics"] = diagnostics or {}
        temporary = path.with_suffix(f".{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
