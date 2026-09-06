#!/usr/bin/env python3
"""
The event bus.

Dispatch is synchronous and single-threaded: publish() appends to a FIFO queue
and drains it, so ordering is deterministic and there is nothing to await.
Events published from inside a handler queue behind the current one rather
than recursing, which is what keeps a cascade readable in a stack trace.

This is deliberately the smallest thing that works. There is no broker, no
persistence in the bus itself, no threads: a daily batch job that finishes in
minutes does not need any of it, and every one of them would add operating
surface with nothing in return. The one durability feature - the run journal -
is a subscriber (ainews/delivery/journal.py), not a bus concern.
"""

from __future__ import annotations

import sys
from collections import deque
from typing import Callable, TypeVar

from ainews.events import Event, StageFailed

E = TypeVar("E", bound=Event)
Handler = Callable[[E, "EventBus"], None]


class EventBus:
    """Synchronous publish/subscribe.

    Handlers are `(event, bus) -> None`. They receive the bus so they can
    publish follow-on events without importing anything about who else is
    listening. Subscribing to `Event` itself matches everything, which is how
    the journal records a whole run.
    """

    def __init__(self, trace: bool = False) -> None:
        self._handlers: dict[type[Event], list[Handler]] = {}
        self._queue: deque[Event] = deque()
        self._draining = False
        self.history: list[Event] = []
        self.failures: list[StageFailed] = []
        self.trace = trace

    def subscribe(self, event_type: type[E], handler: Handler) -> None:
        """Register a handler. Subclasses of `event_type` match too."""
        self._handlers.setdefault(event_type, []).append(handler)

    def publish(self, event: Event) -> None:
        """Queue an event and, unless we're already inside dispatch, drain."""
        self._queue.append(event)
        if not self._draining:
            self._drain()

    def _drain(self) -> None:
        self._draining = True
        try:
            while self._queue:
                self._dispatch(self._queue.popleft())
        finally:
            self._draining = False

    def _dispatch(self, event: Event) -> None:
        self.history.append(event)
        if isinstance(event, StageFailed):
            self.failures.append(event)

        handlers = [
            handler
            for event_type, registered in self._handlers.items()
            if isinstance(event, event_type)
            for handler in registered
        ]
        if self.trace:
            print(
                f"[bus] {type(event).__name__} -> {len(handlers)} handler(s)",
                file=sys.stderr,
            )

        for handler in handlers:
            name = getattr(handler, "__qualname__", repr(handler))
            try:
                handler(event, self)
            except Exception as exc:  # a stage must not kill the process
                detail = f"{type(exc).__name__}: {exc}"
                print(f"[bus] {name} failed on {type(event).__name__}: {detail}", file=sys.stderr)
                if isinstance(event, StageFailed):
                    continue  # never let failure handling fail recursively
                # The failed event travels with the failure, so a retry or
                # dead-letter handler has the payload it would need to act.
                self.publish(StageFailed(handler=name, event=event, error=detail))

    # -- reading back what happened ----------------------------------------

    def all_of(self, event_type: type[E]) -> list[E]:
        return [e for e in self.history if isinstance(e, event_type)]

    def last(self, event_type: type[E]) -> E | None:
        for event in reversed(self.history):
            if isinstance(event, event_type):
                return event
        return None
