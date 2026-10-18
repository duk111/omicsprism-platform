from __future__ import annotations

from contextvars import ContextVar
from threading import Event


_cancel_event: ContextVar[Event | None] = ContextVar("agent_cancel_event", default=None)


def bind_cancel_event(event: Event):
    return _cancel_event.set(event)


def reset_cancel_event(token) -> None:
    _cancel_event.reset(token)


def cancellation_requested() -> bool:
    event = _cancel_event.get()
    return event is not None and event.is_set()


def raise_if_cancelled() -> None:
    if cancellation_requested():
        raise TimeoutError("Agent turn cancellation requested")
