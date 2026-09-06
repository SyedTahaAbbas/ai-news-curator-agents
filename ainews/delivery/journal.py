#!/usr/bin/env python3
"""
The run journal: every event of a run, appended to a JSONL file.

The bus already holds the whole run in memory and then throws it away. Writing
it down costs one subscriber and buys two things:

* a complete audit trail - exactly what the gatherer saw, what the analyst
  kept, what the writer produced, in order, with timestamps;
* replay. `ai-news replay runs/<file>.jsonl` re-publishes a recorded event into
  a fresh bus, so delivery can be re-run against yesterday's material without
  refetching 2,000 items or paying for another model call.

This is a subscriber, not a bus feature. The bus stays a dispatcher.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from ainews.bus import EventBus
from ainews.events import Event, event_from_payload, event_to_payload
from ainews.paths import project_root


class RunJournal:
    """Appends every event to one file per run."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.seq = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def for_run(cls, run_at: datetime, directory: Path | None = None) -> "RunJournal":
        directory = directory or project_root() / "runs"
        return cls(directory / f"{run_at:%Y-%m-%dT%H%M%SZ}.jsonl")

    def record(self, event: Event) -> None:
        self.seq += 1
        line = {
            "seq": self.seq,
            "at": datetime.now().astimezone().isoformat(),
            **event_to_payload(event),
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")

    def read(self) -> list[Event]:
        """Every event in the file, in order. Unknown types are skipped so an
        old journal stays readable after the vocabulary changes."""
        events: list[Event] = []
        for n, raw in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            try:
                events.append(event_from_payload(json.loads(raw)))
            except (json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
                print(f"[journal] {self.path.name}:{n} skipped: {exc}", file=sys.stderr)
        return events

    def last_of(self, type_name: str) -> Event | None:
        for event in reversed(self.read()):
            if type(event).__name__ == type_name:
                return event
        return None


def register_journal(bus: EventBus, journal: RunJournal) -> None:
    """Record everything. Subscribing to Event itself matches every event."""

    def on_any(event: Event, bus: EventBus) -> None:
        journal.record(event)

    bus.subscribe(Event, on_any)
