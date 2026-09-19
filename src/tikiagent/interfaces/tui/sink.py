"""Application EventBus 到 Textual Message Queue 的线程安全桥。"""

from collections.abc import Callable
import threading

from textual.message import Message

from tikiagent.application.models import ApplicationEvent
from tikiagent.interfaces.tui.messages import TuiEventReceived


class TextualEventSink:
    def __init__(self, post_message: Callable[[Message], bool]) -> None:
        self._post_message = post_message

    def handle(self, event: ApplicationEvent) -> None:
        # post_message 是 Worker 唯一允许调用的 Textual API。
        self._post_message(TuiEventReceived(event, producer_thread_id=threading.get_ident()))
