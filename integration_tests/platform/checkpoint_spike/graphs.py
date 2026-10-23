"""不调用模型的真实 LangGraph，计数用于观察恢复是否重跑节点。"""
from typing import Annotated, TypedDict
import operator
import asyncio
from threading import Lock

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt


class State(TypedDict):
    values: Annotated[list[str], operator.add]


def build(saver, mode, trace, fail=False):
    trace_lock = Lock()
    good_persisted = asyncio.Event()

    def writes_committed(writes):
        if any(channel == "values" and value == ["good"] for channel, value in writes):
            good_persisted.set()

    saver.writes_committed = writes_committed

    def record(name):
        # 同一图并行节点在工作线程执行，Windows 文件追加需显式串行化。
        with trace_lock:
            with trace.open("a", encoding="utf-8") as handle:
                handle.write(name + "\n")

    def waiting(state):
        record("waiting")
        answer = interrupt("确认继续")
        return {"values": [answer]}

    def good(state):
        record("good")
        return {"values": ["good"]}

    async def flaky(state):
        record("flaky")
        if fail:
            # 等兄弟节点写入真正提交后再注入错误，不依赖调度时间碰运气。
            await asyncio.wait_for(good_persisted.wait(), timeout=10)
            raise RuntimeError("injected-node-failure")
        return {"values": ["flaky"]}

    graph = StateGraph(State)
    if mode == "interrupt":
        graph.add_node("waiting", waiting)
        graph.add_edge(START, "waiting")
        graph.add_edge("waiting", END)
    else:
        graph.add_node("good", good)
        graph.add_node("flaky", flaky)
        for name in ("good", "flaky"):
            graph.add_edge(START, name)
            graph.add_edge(name, END)
    return graph.compile(checkpointer=saver)
