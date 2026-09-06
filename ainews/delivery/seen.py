#!/usr/bin/env python3
"""Recording what was delivered, so tomorrow doesn't send it again.

Subscribed to DigestDelivered, never to DigestRendered: a digest that exists
is not a digest that arrived. See ainews/delivery/commit.py for what decides
the difference.
"""

from __future__ import annotations

from ainews.bus import EventBus
from ainews.events import DigestDelivered, DigestRendered, ItemsMarkedSeen
from ainews.state import SeenStore


def register_seen(bus: EventBus, store: SeenStore) -> None:
    # The uids to mark come from the rendered digest; the decision to mark them
    # comes from delivery having succeeded. Holding the last rendered item list
    # keeps DigestDelivered from having to carry the whole payload again.
    latest: dict[str, list[str]] = {}

    def remember(event: DigestRendered, bus: EventBus) -> None:
        latest["uids"] = [item.uid for item in event.items]

    def mark_seen(event: DigestDelivered, bus: EventBus) -> None:
        uids = latest.get("uids", [])
        if not uids:
            return
        store.mark(uids, event.config.run_at)
        print(f"Marked {len(uids)} item(s) as seen.")
        bus.publish(ItemsMarkedSeen(count=len(uids)))

    bus.subscribe(DigestRendered, remember)
    bus.subscribe(DigestDelivered, mark_seen)
