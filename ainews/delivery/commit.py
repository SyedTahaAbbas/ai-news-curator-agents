#!/usr/bin/env python3
"""
The commit point.

Marking stories as seen is the only irreversible act in the run: it is what
stops them ever being sent again. It used to happen unconditionally, right
after rendering, in the same breath as writing the files - which meant a bad
SMTP password produced a rendered digest, no email, and a batch of stories
marked as delivered. They were then never sent, on that day or any other. The
symptom is a newsletter that just gets thinner.

So the commit is a join, not a step. This coordinator watches every channel
that was supposed to deliver and publishes DigestDelivered only once they all
have. If any of them failed - including by raising, in which case its report
never arrives at all - it publishes DeliveryIncomplete instead, nothing is
marked seen, and tomorrow's run picks the same stories up again.

    --no-email             -> files are the commit
    SMTP env vars missing  -> email skipped; files are the commit
    SMTP configured, sent  -> both must report; then commit
    SMTP configured, FAILED-> no commit, retried tomorrow
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable

from ainews.bus import EventBus
from ainews.events import (
    DeliveryIncomplete,
    DigestDelivered,
    DigestEmailed,
    DigestFilesWritten,
    DigestRendered,
    RunConfig,
)


@dataclass
class _Pending:
    config: RunConfig
    expected: set[str] = field(default_factory=set)
    done: set[str] = field(default_factory=set)
    failed: list[str] = field(default_factory=list)
    item_count: int = 0
    settled: bool = False


def register_commit(bus: EventBus) -> Callable[[], None]:
    """Subscribe the delivery join. Returns a finalize() for the caller to run
    once the bus has drained.

    Registered before the channel handlers so `expected` is populated by the
    time any of them reports back. Dispatch is FIFO, so the channels' events
    are queued behind DigestRendered and always arrive after this has run.
    """
    state: dict[str, _Pending] = {}

    def _settle(pending: _Pending, bus: EventBus, force: bool = False) -> None:
        accounted = pending.done | set(pending.failed)
        if pending.settled or not (force or accounted >= pending.expected):
            return
        pending.settled = True
        # A channel whose handler raised never reports at all; at finalize time
        # its silence is the failure.
        pending.failed += sorted(pending.expected - accounted)
        if pending.failed:
            reason = f"{', '.join(pending.failed)} did not deliver"
            print(f"[commit] {reason}; nothing marked seen.", file=sys.stderr)
            bus.publish(DeliveryIncomplete(failed=sorted(pending.failed), reason=reason))
            return
        bus.publish(
            DigestDelivered(
                channels=sorted(pending.done),
                item_count=pending.item_count,
                config=pending.config,
            )
        )

    def on_rendered(event: DigestRendered, bus: EventBus) -> None:
        if event.config.dry_run:
            return  # a dry run delivers nothing, so it commits nothing
        expected = {"files"}
        if event.config.email:
            expected.add("email")
        state["run"] = _Pending(
            config=event.config, expected=expected, item_count=len(event.items)
        )

    def on_files_written(event: DigestFilesWritten, bus: EventBus) -> None:
        pending = state.get("run")
        if pending:
            pending.done.add("files")
            _settle(pending, bus)

    def on_emailed(event: DigestEmailed, bus: EventBus) -> None:
        pending = state.get("run")
        if not pending:
            return
        if event.sent or event.skipped:
            pending.done.add("email")
        else:
            pending.failed.append("email")
        _settle(pending, bus)

    bus.subscribe(DigestRendered, on_rendered)
    bus.subscribe(DigestFilesWritten, on_files_written)
    bus.subscribe(DigestEmailed, on_emailed)

    def finalize() -> None:
        """Close out a run whose channels never all reported.

        A handler that raised leaves the join waiting forever, and a run that
        silently never commits is exactly the ambiguity this coordinator
        exists to remove. Called by the CLI once the bus has drained.
        """
        pending = state.get("run")
        if pending and not pending.settled:
            _settle(pending, bus, force=True)

    return finalize
