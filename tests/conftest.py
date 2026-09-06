"""
Shared fixtures.

Everything here is in-memory or under tmp_path. No test reads the developer's
real .seen.json, writes to `see news/`, or touches the network - which is what
the SeenStore/FeedHealthStore interfaces and the injected run clock bought us.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ainews.bus import EventBus
from ainews.config import Config, Feed, Health, Scoring
from ainews.events import FeedOutcome, RunConfig
from ainews.models import Item
from ainews.state import InMemoryFeedHealthStore, InMemorySeenStore

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def now() -> datetime:
    """A frozen run clock. Every stage reads `run_at`, so nothing drifts."""
    return NOW


@pytest.fixture
def cfg() -> Config:
    """A small config built in code - independent of the real sources.yaml, so
    editing the feed list can't break unrelated tests."""
    return Config(
        feeds=[
            Feed(id="openai", name="OpenAI", url="https://openai.com/rss",
                 weight=2.0, category="Labs & Releases", always=True),
            Feed(id="nvidia", name="NVIDIA", url="https://nvidia.com/rss",
                 weight=1.5, category="Labs & Releases", always=False),
            Feed(id="techcrunch", name="TechCrunch AI", url="https://tc.com/rss",
                 weight=1.0, category="Industry & Press", always=False),
            Feed(id="hn", name="Hacker News", url="https://hn.com/rss",
                 weight=1.0, category="Community", always=False),
        ],
        keywords=["ai", "llm", "inference", "gpu cluster", "claude"],
        boost_terms=["release*", "announce*"],
        mute_terms=["sponsored", "webinar replay"],
        scoring=Scoring(),
        health=Health(),
    )


@pytest.fixture
def seen_store() -> InMemorySeenStore:
    return InMemorySeenStore()


@pytest.fixture
def health_store() -> InMemoryFeedHealthStore:
    return InMemoryFeedHealthStore()


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def run_config(now) -> RunConfig:
    """Defaults suited to tests: no LLM call, no email, real (tmp) files."""
    return RunConfig(
        hours=24, max_items=50, include_seen=True, commentary=False,
        email=False, dry_run=False, run_at=now,
    )


def make_item(
    title: str,
    feed_id: str = "techcrunch",
    source: str = "TechCrunch AI",
    category: str = "Industry & Press",
    hours_ago: float = 1,
    url: str | None = None,
    summary: str = "",
) -> Item:
    return Item(
        title=title,
        url=url or f"https://example.com/{abs(hash(title))}",
        source=source,
        feed_id=feed_id,
        category=category,
        published=NOW - timedelta(hours=hours_ago),
        summary=summary,
    )


@pytest.fixture
def items() -> list[Item]:
    return [
        make_item("Anthropic releases Claude update", "openai", "OpenAI", "Labs & Releases", 2),
        make_item("OpenAI announces new model", "openai", "OpenAI", "Labs & Releases", 1),
        make_item("Best sandwich recipes of 2026", "nvidia", "NVIDIA", "Labs & Releases", 1),
        make_item("New GPU cluster for LLM inference", "nvidia", "NVIDIA", "Labs & Releases", 3),
        make_item("Stale AI story from last week", "techcrunch", "TechCrunch AI", "Industry & Press", 200),
        make_item("Sponsored webinar replay on AI", "hn", "Hacker News", "Community", 1),
    ]


@pytest.fixture
def outcomes() -> list[FeedOutcome]:
    return [
        FeedOutcome(feed_id="openai", name="OpenAI", items=2),
        FeedOutcome(feed_id="nvidia", name="NVIDIA", items=2),
        FeedOutcome(feed_id="techcrunch", name="TechCrunch AI", items=1),
        FeedOutcome(feed_id="hn", name="Hacker News", error="HTTP 404"),
    ]
