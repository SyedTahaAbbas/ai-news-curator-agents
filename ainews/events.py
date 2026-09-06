#!/usr/bin/env python3
"""
The event vocabulary every stage speaks.

No stage calls another. A stage announces what it did by publishing an event;
whoever cares subscribes (see ainews/bus.py). The full cascade:

    RunRequested
      -> gatherer  -> StoriesGathered
      -> health    -> FeedHealthAssessed
      -> analyst   -> StoriesRanked
      -> writer    -> CommentaryWritten
      -> render    -> DigestRendered
                        |-> files  -> DigestFilesWritten  --.
                        |-> email  -> DigestEmailed        --+-> DigestDelivered
                        `-> print (dry run only)              |     -> ItemsMarkedSeen
                                                              `-> DeliveryIncomplete

DigestDelivered is the commit point, and the join in ainews/delivery/commit.py
is what decides when it fires. Marking stories as seen is the one irreversible
act in the run - it is what stops them ever being sent again - so it hangs off
delivery having actually succeeded, not off the digest merely existing.

Every event is JSON-serialisable in both directions so the run journal can
record a whole run and replay it later.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any

from ainews.models import Item


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Run configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunConfig:
    """The options for one run, carried along on every event.

    Handlers read the flags that concern them off this instead of being told
    what to do by the stage upstream - that's what keeps stages from having to
    know about each other. `--no-email`, for instance, is not the renderer's
    business: it is the email handler that declines to run.

    `run_at` is the run's single clock. Every stage that needs "now" - the
    recency curve, the digest filename, the seen-marks, the email subject -
    reads it from here, so a run that straddles midnight can't file half its
    output under one date and half under the next.
    """

    hours: int = 24
    max_items: int = 35
    include_seen: bool = False
    commentary: bool = True
    email: bool = True
    dry_run: bool = False
    run_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True)
class FeedOutcome:
    """What happened to one feed on this run. The unit of feed health."""

    feed_id: str
    name: str
    items: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def errors_of(outcomes: list[FeedOutcome]) -> dict[str, str]:
    """The {feed name: error} shape the digest renders."""
    return {o.name: o.error for o in outcomes if o.error}


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """Base class. An event is an immutable record of something that happened."""


@dataclass(frozen=True)
class RunRequested(Event):
    """Someone asked for a digest. The only event a caller publishes by hand."""

    config: RunConfig = field(default_factory=RunConfig)


@dataclass(frozen=True)
class StoriesGathered(Event):
    """Gatherer: every feed has been fetched. Unfiltered, unranked."""

    items: list[Item]
    outcomes: list[FeedOutcome] = field(default_factory=list)
    config: RunConfig = field(default_factory=RunConfig)

    @property
    def errors(self) -> dict[str, str]:
        return errors_of(self.outcomes)


@dataclass(frozen=True)
class FeedHealthAssessed(Event):
    """How much of the source list is actually working, and for how long it
    hasn't been. `fatal` means the run should not be treated as a success."""

    total: int
    unreachable: int
    dead_ratio: float
    warnings: list[str] = field(default_factory=list)
    fatal: bool = False
    reason: str = ""


@dataclass(frozen=True)
class StoriesRanked(Event):
    """Analyst: filtered, scored, deduped, checked against the seen store."""

    items: list[Item]
    outcomes: list[FeedOutcome] = field(default_factory=list)
    config: RunConfig = field(default_factory=RunConfig)

    @property
    def errors(self) -> dict[str, str]:
        return errors_of(self.outcomes)


@dataclass(frozen=True)
class CommentaryWritten(Event):
    """Writer: the two commentary levels, either of which may be None."""

    items: list[Item]
    outcomes: list[FeedOutcome] = field(default_factory=list)
    simple: str | None = None
    deep: str | None = None
    config: RunConfig = field(default_factory=RunConfig)

    @property
    def errors(self) -> dict[str, str]:
        return errors_of(self.outcomes)


@dataclass(frozen=True)
class DigestRendered(Event):
    """The digest exists as Markdown. Delivery subscribers take it from here."""

    items: list[Item]
    markdown: str
    outcomes: list[FeedOutcome] = field(default_factory=list)
    simple: str | None = None
    deep: str | None = None
    config: RunConfig = field(default_factory=RunConfig)

    @property
    def errors(self) -> dict[str, str]:
        return errors_of(self.outcomes)


@dataclass(frozen=True)
class DigestFilesWritten(Event):
    """`see news/<date>.md`, its JSON sibling, and latest.md are on disk."""

    markdown_path: str
    json_path: str


@dataclass(frozen=True)
class DigestEmailed(Event):
    """The email channel reported back.

    `skipped` distinguishes "no mailbox is configured, so email was never on
    the table" from "we tried to send and failed" - only the second blocks the
    commit, because only the second means a digest went missing.
    """

    sent: bool
    skipped: bool = False
    reason: str = ""


@dataclass(frozen=True)
class DigestDelivered(Event):
    """Every channel that was supposed to deliver, delivered. The commit point."""

    channels: list[str] = field(default_factory=list)
    item_count: int = 0
    config: RunConfig = field(default_factory=RunConfig)


@dataclass(frozen=True)
class DeliveryIncomplete(Event):
    """A channel that was supposed to deliver didn't. Nothing gets marked seen,
    so today's stories are still on the table tomorrow."""

    failed: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass(frozen=True)
class ItemsMarkedSeen(Event):
    """The seen store now knows about these items. Irreversible for 30 days."""

    count: int


@dataclass(frozen=True)
class FeedCheckRequested(Event):
    """`ai-news check-feeds`: health-check sources instead of running a digest."""


@dataclass(frozen=True)
class FeedCheckCompleted(Event):
    healthy: int
    unreachable: int


@dataclass(frozen=True)
class StageFailed(Event):
    """A handler raised. Published instead of propagating, so one dead stage
    leaves the rest of the run - and the other subscribers of the same event -
    running. The failed event travels with it, so a retry or dead-letter
    handler has the payload it would need to act on."""

    handler: str
    event: Event
    error: str

    @property
    def event_name(self) -> str:
        return type(self.event).__name__


# ---------------------------------------------------------------------------
# JSON codec (used by the run journal, and by replay)
# ---------------------------------------------------------------------------

EVENT_TYPES: dict[str, type[Event]] = {
    cls.__name__: cls
    for cls in (
        RunRequested,
        StoriesGathered,
        FeedHealthAssessed,
        StoriesRanked,
        CommentaryWritten,
        DigestRendered,
        DigestFilesWritten,
        DigestEmailed,
        DigestDelivered,
        DeliveryIncomplete,
        ItemsMarkedSeen,
        FeedCheckRequested,
        FeedCheckCompleted,
        StageFailed,
    )
}


def _encode_value(name: str, value: Any) -> Any:
    if name == "items":
        return [item.to_json() for item in value]
    if name == "outcomes":
        return [asdict(outcome) for outcome in value]
    if name == "config":
        data = asdict(value)
        data["run_at"] = value.run_at.isoformat()
        return data
    if name == "event":  # StageFailed's nested event
        return event_to_payload(value)
    return value


def _decode_value(name: str, value: Any) -> Any:
    if name == "items":
        return [Item.from_dict(d) for d in value]
    if name == "outcomes":
        return [FeedOutcome(**d) for d in value]
    if name == "config":
        data = dict(value)
        data["run_at"] = datetime.fromisoformat(data["run_at"])
        known = {f.name for f in fields(RunConfig)}
        return RunConfig(**{k: v for k, v in data.items() if k in known})
    if name == "event":
        return event_from_payload(value)
    return value


def event_to_payload(event: Event) -> dict[str, Any]:
    """One event as a JSON-safe dict, tagged with its type."""
    payload = {
        f.name: _encode_value(f.name, getattr(event, f.name)) for f in fields(event)
    }
    return {"type": type(event).__name__, "data": payload}


def event_from_payload(payload: dict[str, Any]) -> Event:
    """Rebuild an event recorded by event_to_payload()."""
    name = payload.get("type", "")
    cls = EVENT_TYPES.get(name)
    if cls is None:
        raise ValueError(f"unknown event type {name!r}")
    known = {f.name for f in fields(cls)}
    data = {
        key: _decode_value(key, value)
        for key, value in payload.get("data", {}).items()
        if key in known
    }
    return cls(**data)
