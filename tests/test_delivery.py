"""
The commit point, which is where the data-loss bug lived.

Marking stories seen is irreversible: it is what stops them ever being sent
again. These tests pin down exactly when it happens.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from ainews.bus import EventBus
from ainews.delivery.commit import register_commit
from ainews.delivery.files import register_files
from ainews.delivery.render import register_render
from ainews.delivery.seen import register_seen
from ainews.events import (
    CommentaryWritten,
    DeliveryIncomplete,
    DigestDelivered,
    DigestEmailed,
    DigestFilesWritten,
    DigestRendered,
    ItemsMarkedSeen,
)


@pytest.fixture
def wired(bus, cfg, seen_store, health_store, tmp_path):
    """Render + files + commit + seen, with a stub email channel the test drives."""
    finalize = register_commit(bus)
    register_render(bus, cfg, health_store)
    register_files(bus, digest_dir=tmp_path / "see news")
    register_seen(bus, seen_store)
    return bus, finalize


def email_channel(bus: EventBus, *, sent: bool, skipped: bool = False):
    """Stand-in for the SMTP handler, reporting whatever the test needs."""

    def handler(event: DigestRendered, bus: EventBus) -> None:
        if event.config.dry_run or not event.config.email:
            return
        bus.publish(DigestEmailed(sent=sent, skipped=skipped))

    bus.subscribe(DigestRendered, handler)


def commentary(items, run_config, **overrides):
    return CommentaryWritten(items=items, config=replace(run_config, **overrides))


def test_files_only_run_commits_on_files_written(wired, items, run_config, seen_store):
    bus, finalize = wired
    bus.publish(commentary(items, run_config, email=False))
    finalize()

    assert bus.last(DigestFilesWritten) is not None
    assert bus.last(DigestDelivered) is not None
    assert bus.last(ItemsMarkedSeen).count == len(items)
    assert len(seen_store.load()) == len(items)


def test_successful_email_commits(wired, items, run_config, seen_store):
    bus, finalize = wired
    email_channel(bus, sent=True)
    bus.publish(commentary(items, run_config, email=True))
    finalize()

    assert bus.last(DigestDelivered).channels == ["email", "files"]
    assert len(seen_store.load()) == len(items)


def test_a_failed_send_blocks_the_commit(wired, items, run_config, seen_store):
    """The bug: a bad SMTP password used to render a digest, send nothing, and
    still mark every story as delivered - losing them permanently."""
    bus, finalize = wired
    email_channel(bus, sent=False)
    bus.publish(commentary(items, run_config, email=True))
    finalize()

    assert bus.last(DigestFilesWritten) is not None  # the file is still written
    assert bus.last(DigestDelivered) is None
    assert bus.last(ItemsMarkedSeen) is None
    assert seen_store.load() == {}  # so tomorrow tries again
    assert bus.last(DeliveryIncomplete).failed == ["email"]


def test_unconfigured_smtp_is_a_skip_not_a_failure(wired, items, run_config, seen_store):
    """No mailbox set up means email was never on the table - a local run
    without a .env should still commit."""
    bus, finalize = wired
    email_channel(bus, sent=False, skipped=True)
    bus.publish(commentary(items, run_config, email=True))
    finalize()

    assert bus.last(DigestDelivered) is not None
    assert len(seen_store.load()) == len(items)


def test_a_channel_that_raises_blocks_the_commit(bus, cfg, seen_store, health_store, items, run_config):
    """A handler that dies never reports at all; finalize() must not read that
    silence as success."""
    finalize = register_commit(bus)
    register_render(bus, cfg, health_store)

    def broken_files(event, bus):
        raise OSError("disk full")

    bus.subscribe(DigestRendered, broken_files)
    register_seen(bus, seen_store)

    bus.publish(commentary(items, run_config, email=False))
    finalize()

    assert bus.failures  # the raise became a StageFailed
    assert bus.last(DigestDelivered) is None
    assert seen_store.load() == {}
    assert "files" in bus.last(DeliveryIncomplete).failed


def test_dry_run_writes_nothing_and_commits_nothing(wired, items, run_config, seen_store, tmp_path, capsys):
    bus, finalize = wired
    bus.publish(commentary(items, run_config, dry_run=True, email=True))
    finalize()

    assert "# AI News Curator" in capsys.readouterr().out
    assert not (tmp_path / "see news").exists()
    assert bus.last(DigestDelivered) is None
    assert seen_store.load() == {}


def test_files_land_under_the_run_date(wired, items, run_config, tmp_path, now):
    bus, finalize = wired
    bus.publish(commentary(items, run_config, email=False))
    finalize()

    digest_dir = tmp_path / "see news"
    stamp = f"{now:%Y-%m-%d}"
    assert (digest_dir / f"{stamp}.md").exists()
    assert (digest_dir / f"{stamp}.json").exists()
    assert (tmp_path / "latest.md").exists()

    written = json.loads((digest_dir / f"{stamp}.json").read_text())
    assert len(written) == len(items)
    assert "breakdown" in written[0]  # the ranking is explainable from the artifact


def test_a_run_at_just_before_midnight_files_everything_under_one_date(
    wired, items, run_config, tmp_path, now
):
    """Five separate now() calls used to mean a run could file its digest under
    one date and its seen-marks under the next."""
    late = now.replace(hour=23, minute=59, second=58)
    bus, finalize = wired
    bus.publish(commentary(items, run_config, email=False, run_at=late))
    finalize()

    assert (tmp_path / "see news" / f"{late:%Y-%m-%d}.md").exists()
    delivered = bus.last(DigestDelivered)
    assert delivered.config.run_at == late
