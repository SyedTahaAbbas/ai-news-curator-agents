#!/usr/bin/env python3
"""
Durable state: what has already been sent, and which feeds are rotting.

Both used to be module-level file paths poked at by free functions from two
different modules - the analyst read .seen.json, delivery wrote it. That is
one mutable file with two owners and no seam for tests, which is why the old
test suite had to read the developer's real .seen.json to run.

Now both are objects with an interface, handed to the stages that need them at
registration time. The JSON implementations are the ones the app uses; the
in-memory ones are what make the delivery tests possible at all.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Protocol

from ainews.events import FeedOutcome
from ainews.paths import project_root

SEEN_RETENTION_DAYS = 30

# Distinguishes "you didn't say" from "you said None", which for the health
# store means "keep it in memory, write nothing".
_DEFAULT = object()


def seen_file() -> Path:
    return project_root() / ".seen.json"


def health_file() -> Path:
    return project_root() / ".feed-health.json"


# ---------------------------------------------------------------------------
# Seen store
# ---------------------------------------------------------------------------


class SeenStore(Protocol):
    """What has already gone out, so it never goes out twice."""

    def load(self) -> dict[str, str]:
        ...

    def mark(self, uids: Iterable[str], when: datetime) -> None:
        ...


class JsonSeenStore:
    """The real one: a uid -> ISO timestamp map, pruned to 30 days on write.

    The on-disk format is unchanged from previous versions, so an existing
    .seen.json keeps working untouched.
    """

    def __init__(self, path: Path | None = None, retention_days: int = SEEN_RETENTION_DAYS) -> None:
        self.path = path or seen_file()
        self.retention_days = retention_days

    def load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def mark(self, uids: Iterable[str], when: datetime) -> None:
        seen = self.load()
        stamp = when.isoformat()
        seen.update({uid: stamp for uid in uids})
        self.path.write_text(json.dumps(self._pruned(seen, when), indent=0), encoding="utf-8")

    def _pruned(self, seen: dict[str, str], now: datetime) -> dict[str, str]:
        cutoff = now - timedelta(days=self.retention_days)
        pruned = {}
        for uid, iso in seen.items():
            try:
                if datetime.fromisoformat(iso) >= cutoff:
                    pruned[uid] = iso
            except (ValueError, TypeError):
                continue
        return pruned


class InMemorySeenStore:
    """For tests, and for anything that must not touch the developer's state."""

    def __init__(self, seen: dict[str, str] | None = None) -> None:
        self._seen = dict(seen or {})

    def load(self) -> dict[str, str]:
        return dict(self._seen)

    def mark(self, uids: Iterable[str], when: datetime) -> None:
        self._seen.update({uid: when.isoformat() for uid in uids})


# ---------------------------------------------------------------------------
# Feed health
# ---------------------------------------------------------------------------


@dataclass
class FeedHealth:
    name: str = ""
    consecutive_failures: int = 0
    failing_since: str | None = None
    last_ok: str | None = None
    last_error: str | None = None

    def dead_days(self, now: datetime) -> int:
        if not self.failing_since:
            return 0
        try:
            return max((now - datetime.fromisoformat(self.failing_since)).days, 0)
        except (ValueError, TypeError):
            return 0


class FeedHealthStore:
    """A rolling record of which feeds are working.

    A single failed fetch is noise - Reddit rate-limits, arXiv times out - and
    the digest already footnotes it. What this catches is the failure nobody
    notices: a feed whose URL rotted months ago, quietly contributing nothing
    while the digest still looks plausible.
    """

    def __init__(self, path: Path | None = _DEFAULT) -> None:  # type: ignore[assignment]
        self.path = health_file() if path is _DEFAULT else path
        self._records: dict[str, FeedHealth] | None = None

    def load(self) -> dict[str, FeedHealth]:
        if self._records is not None:
            return self._records
        records: dict[str, FeedHealth] = {}
        if self.path and self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                raw = {}
            known = {f.name for f in fields(FeedHealth)}
            for feed_id, entry in (raw or {}).items():
                if isinstance(entry, dict):
                    records[feed_id] = FeedHealth(
                        **{k: v for k, v in entry.items() if k in known}
                    )
        self._records = records
        return records

    def record(self, outcomes: Iterable[FeedOutcome], now: datetime) -> None:
        records = self.load()
        stamp = now.isoformat()
        for outcome in outcomes:
            entry = records.setdefault(outcome.feed_id, FeedHealth())
            entry.name = outcome.name
            if outcome.ok:
                entry.consecutive_failures = 0
                entry.failing_since = None
                entry.last_error = None
                entry.last_ok = stamp
            else:
                entry.consecutive_failures += 1
                entry.failing_since = entry.failing_since or stamp
                entry.last_error = outcome.error
        self._save()

    def dead_for_days(self, min_days: int, now: datetime) -> list[tuple[str, int]]:
        """Feeds failing for at least `min_days` running, worst first."""
        stale = [
            (entry.name or feed_id, entry.dead_days(now))
            for feed_id, entry in self.load().items()
            if entry.consecutive_failures > 0 and entry.dead_days(now) >= min_days
        ]
        return sorted(stale, key=lambda pair: -pair[1])

    def _save(self) -> None:
        if not self.path:
            return
        payload = {
            feed_id: asdict(entry) for feed_id, entry in (self._records or {}).items()
        }
        self.path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")


class InMemoryFeedHealthStore(FeedHealthStore):
    """FeedHealthStore with nothing behind it. Used by tests and by --dry-run."""

    def __init__(self, records: dict[str, FeedHealth] | None = None) -> None:
        super().__init__(path=None)
        self._records = dict(records or {})
