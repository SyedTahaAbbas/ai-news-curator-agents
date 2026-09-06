#!/usr/bin/env python3
"""
Stage 1: Gatherer.

Fetches every feed in sources.yaml in parallel and emits raw Items - no
filtering, scoring, or ranking. It does not know the analyst exists: it
subscribes to RunRequested and publishes StoriesGathered.

It also publishes a FeedOutcome per feed, which is what the health assessment
downstream is built on. A run where 8 of 22 sources quietly stopped working
still produces a plausible-looking digest, and that is the failure this data
exists to catch.
"""

from __future__ import annotations

import concurrent.futures
import socket
import sys
from typing import Any

import feedparser

from ainews.bus import EventBus
from ainews.config import Config, Feed
from ainews.events import (
    FeedCheckCompleted,
    FeedCheckRequested,
    FeedHealthAssessed,
    FeedOutcome,
    RunRequested,
    StoriesGathered,
    utcnow,
)
from ainews.http import with_retries
from ainews.models import Item, clean_text, parse_date
from ainews.state import FeedHealthStore

USER_AGENT = (
    "Mozilla/5.0 (compatible; AINewsUpdate/1.0; "
    "+https://github.com/) python-feedparser"
)
FETCH_TIMEOUT = 25
MAX_WORKERS = 12
FETCH_ATTEMPTS = 2  # feeds are cheap to lose and there are 22 of them


class FeedFetchError(Exception):
    """A feed did not yield anything usable. Retryable at the fetch layer."""


def _configure_socket_timeout(timeout: float = FETCH_TIMEOUT) -> None:
    """feedparser takes no timeout argument, so a hung connection would block
    its worker thread forever - and a stuck feed also stalls the executor's
    in-order iteration. The process-wide socket default is the standard fix.

    This used to run at import time, which meant importing the gatherer
    silently changed the timeout of every other socket in the process,
    including smtplib's in the email handler. Now the caller opts in.
    """
    socket.setdefaulttimeout(timeout)


def fetch_feed(feed: Feed) -> tuple[Feed, list[Item], str | None]:
    """Fetch one feed. Never raises - a dead source must not kill the run."""

    def once() -> Any:
        parsed = feedparser.parse(feed.url, agent=USER_AGENT)
        status = getattr(parsed, "status", None)
        if status and status >= 400:
            raise FeedFetchError(f"HTTP {status}")
        if not parsed.entries:
            bozo = getattr(parsed, "bozo_exception", None)
            raise FeedFetchError(f"no entries ({bozo})" if bozo else "no entries")
        return parsed

    try:
        # feedparser swallows network errors into bozo_exception rather than
        # raising, so retry on anything that produced no entries - that is the
        # only signal available for "the fetch didn't work".
        parsed = with_retries(
            once,
            attempts=FETCH_ATTEMPTS,
            base_delay=2.0,
            label=f"feed {feed.name}",
            retry_on=lambda exc: isinstance(exc, FeedFetchError),
        )
    except FeedFetchError as exc:
        return feed, [], str(exc)
    except Exception as exc:  # network, DNS, malformed XML, anything
        return feed, [], f"{type(exc).__name__}: {exc}"

    items: list[Item] = []
    for entry in parsed.entries:
        link = entry.get("link") or ""
        title = clean_text(entry.get("title", ""), limit=300)
        if not title or not link:
            continue
        items.append(
            Item(
                title=title,
                url=link,
                source=feed.name,
                feed_id=feed.id,
                category=feed.category,
                published=parse_date(entry) or utcnow(),
                summary=clean_text(
                    entry.get("summary") or entry.get("description") or "", limit=400
                ),
            )
        )
    return feed, items, None


def collect(cfg: Config) -> tuple[list[Item], list[FeedOutcome]]:
    """Fetch all feeds in parallel. Returns (items, one outcome per feed)."""
    _configure_socket_timeout()
    items: list[Item] = []
    outcomes: list[FeedOutcome] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for feed, got, err in pool.map(fetch_feed, cfg.feeds):
            outcomes.append(
                FeedOutcome(feed_id=feed.id, name=feed.name, items=len(got), error=err)
            )
            items.extend(got)
    return items, outcomes


def check_feeds(cfg: Config) -> tuple[int, int]:
    """Health-check every source. Returns (healthy, unreachable)."""
    print(f"Checking {len(cfg.feeds)} feeds\n")
    _, outcomes = collect(cfg)
    for outcome in outcomes:
        if outcome.ok:
            print(f"  OK    {outcome.name:<30} {outcome.items:>3} items")
        else:
            print(f"  DEAD  {outcome.name:<30} {outcome.error}")
    healthy = sum(1 for o in outcomes if o.ok)
    print(f"\n{healthy} healthy, {len(outcomes) - healthy} unreachable")
    return healthy, len(outcomes) - healthy


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def register(bus: EventBus, cfg: Config, health: FeedHealthStore | None = None) -> None:
    """Subscribe this agent to the bus. Nothing below names another stage."""

    def on_run_requested(event: RunRequested, bus: EventBus) -> None:
        items, outcomes = collect(cfg)
        unreachable = sum(1 for o in outcomes if not o.ok)
        print(f"Fetched {len(items)} raw items; {unreachable} feed(s) unreachable.")
        bus.publish(StoriesGathered(items=items, outcomes=outcomes, config=event.config))

    def on_feed_check_requested(event: FeedCheckRequested, bus: EventBus) -> None:
        healthy, unreachable = check_feeds(cfg)
        bus.publish(FeedCheckCompleted(healthy=healthy, unreachable=unreachable))

    bus.subscribe(RunRequested, on_run_requested)
    bus.subscribe(FeedCheckRequested, on_feed_check_requested)

    if health is not None:
        register_health(bus, cfg, health)


def register_health(bus: EventBus, cfg: Config, health: FeedHealthStore) -> None:
    """Assess how much of the source list is actually working.

    Separate from the fetch on purpose: it is a different concern with a
    different failure mode, and keeping it its own subscriber means the
    thresholds can change without touching the fetching code.
    """

    def on_stories_gathered(event: StoriesGathered, bus: EventBus) -> None:
        outcomes = event.outcomes
        total = len(outcomes)
        unreachable = sum(1 for o in outcomes if not o.ok)
        ratio = unreachable / total if total else 0.0

        if not event.config.dry_run:
            health.record(outcomes, event.config.run_at)

        warnings = [
            f"{name} has been unreachable for {days} days"
            for name, days in health.dead_for_days(
                cfg.health.warn_after_dead_days, event.config.run_at
            )
        ]
        for warning in warnings:
            print(f"[health] {warning}", file=sys.stderr)

        fatal, reason = False, ""
        if cfg.health.fail_if_no_items and not event.items:
            fatal, reason = True, "no items gathered from any source"
        elif total and ratio > cfg.health.fail_above_dead_ratio:
            fatal = True
            reason = (
                f"{unreachable}/{total} feeds unreachable "
                f"(over the {cfg.health.fail_above_dead_ratio:.0%} threshold)"
            )
        if fatal:
            print(f"[health] FAIL: {reason}", file=sys.stderr)

        bus.publish(
            FeedHealthAssessed(
                total=total,
                unreachable=unreachable,
                dead_ratio=round(ratio, 3),
                warnings=warnings,
                fatal=fatal,
                reason=reason,
            )
        )

    bus.subscribe(StoriesGathered, on_stories_gathered)
