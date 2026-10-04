"""任务资源消费凭据：先落盘再发送请求；不承担 Workflow 节点恢复。"""
from contextvars import ContextVar
from pathlib import Path
from contextlib import contextmanager
from uuid import uuid4
import hashlib
import json
import os


class RequestBudgetExceeded(RuntimeError):
    """资源已消费或身份锁冲突时保守停止，不进行隐式重试。"""


class RequestBudget:
    def __init__(self, root, *, model_limit=64, web_limit=6):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.model_limit, self.web_limit = model_limit, web_limit
        self.scope = ContextVar(f"request-budget-{id(self)}", default=None)

    def bind(self, session_id, task_id):
        self.scope.set((session_id, task_id))

    @contextmanager
    def _state(self):
        scope = self.scope.get()
        if scope is None:
            yield None
            return
        key = hashlib.sha256(json.dumps(scope).encode()).hexdigest()
        path = self.root / f"{key}.json"
        lock = path.with_suffix(".lock")
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            raise RequestBudgetExceeded("资源账本正被占用或存在未清理锁；禁止猜测重发") from error
        temporary = path.with_suffix(f".{uuid4().hex}.tmp")
        try:
            os.close(fd)
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
                "scope": scope, "model": 0, "work": 0, "web": 0,
                "model_limit": self.model_limit, "web_limit": self.web_limit, "web_keys": []}
            data["model_limit"] = min(data["model_limit"], self.model_limit)
            data["web_limit"] = min(data["web_limit"], self.web_limit)
            yield data
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(data, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
            lock.unlink()

    def model_request(self, *, final=False):
        with self._state() as state:
            if state is None:
                return
            # 独立保留16次收尾请求；网络/校验重试也必须逐次扣减。
            if state["model"] >= state["model_limit"] or (not final and state["work"] >= state["model_limit"] - 16):
                raise RequestBudgetExceeded("任务模型请求额度耗尽；工作请求不得占用收尾保留额度")
            state["model"] += 1
            state["work"] += int(not final)

    def web_request(self, key):
        with self._state() as state:
            if state is None:
                return
            if key in state["web_keys"]:
                raise RequestBudgetExceeded("本任务已请求相同联网参数，请复用已有证据，禁止重复付费检索")
            if state["web"] >= state["web_limit"]:
                raise RequestBudgetExceeded("任务联网工具额度耗尽，重新委派不能刷新")
            state["web"] += 1
            state["web_keys"].append(key)
