"""面向 CLI/UI/API 的同步 Application Event Stream。"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from tikiagent.application.models import (
    ApplicationEvent,
    ApplicationEventType,
    EventScope,
)


_REDACTED = "[REDACTED]"
_SECRET_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
)


class EventSink(Protocol):
    def handle(self, event: ApplicationEvent) -> None: ...


@dataclass(frozen=True, slots=True)
class EventDeliveryFailure:
    sink_name: str
    event_id: str
    message: str


class EventBus:
    """唯一事件工厂：stream 内编号、脱敏、截断和隔离分发。"""

    def __init__(
        self,
        *,
        stream_id: str | None = None,
        max_text_length: int = 1000,
    ) -> None:
        if max_text_length < 32:
            raise ValueError("max_text_length 至少为 32")
        self.stream_id = stream_id or str(uuid4())
        self.max_text_length = max_text_length
        self._sequence = 0
        self._sinks: list[EventSink] = []
        self.delivery_failures: list[EventDeliveryFailure] = []

    def subscribe(self, sink: EventSink) -> None:
        if sink not in self._sinks:
            self._sinks.append(sink)

    def unsubscribe(self, sink: EventSink) -> None:
        if sink in self._sinks:
            self._sinks.remove(sink)

    def emit(
        self,
        event_type: ApplicationEventType,
        *,
        scope: EventScope,
        source: str,
        correlation_id: str,
        message: str,
        causation_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> ApplicationEvent:
        self._sequence += 1
        event = ApplicationEvent(
            stream_id=self.stream_id,
            sequence=self._sequence,
            event_type=event_type,
            scope=scope,
            source=source,
            correlation_id=correlation_id,
            causation_id=causation_id,
            message=_bound(message, self.max_text_length),
            data=_sanitize(data or {}, self.max_text_length),
        )
        for sink in tuple(self._sinks):
            try:
                sink.handle(event.model_copy(deep=True))
            except Exception as error:  # noqa: BLE001 - 展示层故障必须隔离
                self.delivery_failures.append(
                    EventDeliveryFailure(
                        sink_name=type(sink).__name__,
                        event_id=event.event_id,
                        message=str(error),
                    )
                )
        return event


class CollectingEventSink:
    def __init__(self) -> None:
        self.events: list[ApplicationEvent] = []

    def handle(self, event: ApplicationEvent) -> None:
        self.events.append(event)


class FilteringEventSink:
    def __init__(
        self,
        downstream: EventSink,
        *,
        event_types: set[ApplicationEventType],
    ) -> None:
        self.downstream = downstream
        self.event_types = event_types

    def handle(self, event: ApplicationEvent) -> None:
        if event.event_type in self.event_types:
            self.downstream.handle(event)


class CliEventSink:
    def __init__(self, writer: Callable[[str], None] = print) -> None:
        self.writer = writer

    def handle(self, event: ApplicationEvent) -> None:
        source = event.source.replace("_", " ").title()
        self.writer(
            f"[{event.sequence:02d}] [{source}] "
            f"{event.event_type}: {event.message}"
        )


def _sanitize(value: Any, limit: int, key: str | None = None) -> Any:
    if key is not None and _is_secret(key):
        return _REDACTED
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize(item, limit, str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, limit) for item in value]
    if isinstance(value, str):
        return _bound(value, limit)
    return deepcopy(value)


def _is_secret(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return any(marker in normalized for marker in _SECRET_MARKERS)


def _bound(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = "...[truncated]"
    return value[: limit - len(marker)] + marker
