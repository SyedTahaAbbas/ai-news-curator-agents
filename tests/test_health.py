"""
Feed health: catching the failure nobody notices.

A crash is loud. Eight of twenty-two sources quietly rotting is not, and the
digest still looks plausible while it happens.
"""

from dataclasses import replace
from datetime import timedelta

from ainews.agents.gatherer import register_health
from ainews.config import Health
from ainews.events import FeedHealthAssessed, FeedOutcome, StoriesGathered
from ainews.state import FeedHealthStore, InMemoryFeedHealthStore
from tests.conftest import make_item


def gathered(items, outcomes, run_config):
    return StoriesGathered(items=items, outcomes=outcomes, config=run_config)


def test_a_healthy_run_is_not_fatal(bus, cfg, health_store, items, outcomes, run_config):
    register_health(bus, cfg, health_store)
    bus.publish(gathered(items, outcomes, run_config))

    assessment = bus.last(FeedHealthAssessed)
    assert assessment.total == 4 and assessment.unreachable == 1
    assert assessment.dead_ratio == 0.25
    assert not assessment.fatal


def test_no_items_at_all_fails_the_run(bus, cfg, health_store, outcomes, run_config):
    register_health(bus, cfg, health_store)
    bus.publish(gathered([], outcomes, run_config))

    assessment = bus.last(FeedHealthAssessed)
    assert assessment.fatal
    assert "no items" in assessment.reason


def test_majority_of_feeds_dead_fails_the_run(bus, cfg, health_store, items, run_config):
    dead = [
        FeedOutcome(feed_id=f"f{n}", name=f"Feed {n}", error="HTTP 500") for n in range(3)
    ] + [FeedOutcome(feed_id="ok", name="Working", items=5)]
    register_health(bus, cfg, health_store)
    bus.publish(gathered(items, dead, run_config))

    assessment = bus.last(FeedHealthAssessed)
    assert assessment.fatal
    assert "3/4 feeds unreachable" in assessment.reason


def test_a_single_flaky_feed_does_not_fail_the_run(bus, cfg, health_store, items, outcomes, run_config):
    """Reddit rate-limits most mornings. That must not turn CI red."""
    register_health(bus, cfg, health_store)
    bus.publish(gathered(items, outcomes, run_config))
    assert not bus.last(FeedHealthAssessed).fatal


def test_thresholds_come_from_config(bus, cfg, health_store, items, outcomes, run_config):
    strict = replace(cfg, health=Health(fail_above_dead_ratio=0.1))
    register_health(bus, strict, health_store)
    bus.publish(gathered(items, outcomes, run_config))
    assert bus.last(FeedHealthAssessed).fatal


def test_consecutive_failures_accumulate_and_reset(now):
    store = InMemoryFeedHealthStore()
    failing = [FeedOutcome(feed_id="hn", name="Hacker News", error="HTTP 500")]

    for day in range(4):
        store.record(failing, now + timedelta(days=day))

    record = store.load()["hn"]
    assert record.consecutive_failures == 4
    assert record.dead_days(now + timedelta(days=4)) == 4

    store.record([FeedOutcome(feed_id="hn", name="Hacker News", items=3)], now + timedelta(days=5))
    assert store.load()["hn"].consecutive_failures == 0
    assert store.load()["hn"].failing_since is None


def test_a_long_dead_feed_is_warned_about(bus, cfg, items, run_config, now):
    store = InMemoryFeedHealthStore()
    for day in range(5):
        store.record(
            [FeedOutcome(feed_id="hn", name="Hacker News", error="HTTP 404")],
            now - timedelta(days=5 - day),
        )

    register_health(bus, cfg, store)
    bus.publish(gathered(items, [FeedOutcome(feed_id="hn", name="Hacker News", error="HTTP 404")], run_config))

    warnings = bus.last(FeedHealthAssessed).warnings
    assert any("Hacker News" in w and "5 days" in w for w in warnings)


def test_dry_run_records_nothing(bus, cfg, items, outcomes, run_config):
    store = InMemoryFeedHealthStore()
    register_health(bus, cfg, store)
    bus.publish(gathered(items, outcomes, replace(run_config, dry_run=True)))
    assert store.load() == {}


def test_health_state_round_trips_through_its_file(tmp_path, now):
    path = tmp_path / ".feed-health.json"
    FeedHealthStore(path).record(
        [FeedOutcome(feed_id="hn", name="Hacker News", error="HTTP 500")], now
    )
    reloaded = FeedHealthStore(path).load()
    assert reloaded["hn"].consecutive_failures == 1
    assert reloaded["hn"].last_error == "HTTP 500"


def test_a_corrupt_health_file_is_ignored_not_fatal(tmp_path):
    path = tmp_path / ".feed-health.json"
    path.write_text("{not json", encoding="utf-8")
    assert FeedHealthStore(path).load() == {}
