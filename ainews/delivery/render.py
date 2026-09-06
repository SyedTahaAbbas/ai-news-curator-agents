#!/usr/bin/env python3
"""CommentaryWritten -> DigestRendered. The last stage before anything leaves."""

from __future__ import annotations

from ainews.bus import EventBus
from ainews.config import Config
from ainews.events import CommentaryWritten, DigestRendered
from ainews.render import render_markdown
from ainews.state import FeedHealthStore


def register_render(
    bus: EventBus, cfg: Config, health: FeedHealthStore | None = None
) -> None:
    """Subscribe the renderer.

    The health store is queried here rather than threaded through four events:
    the "this feed has been dead for a week" note is presentation, and it is
    only ever needed at the moment the digest is written.
    """

    def on_commentary_written(event: CommentaryWritten, bus: EventBus) -> None:
        warnings = []
        if health is not None:
            warnings = [
                f"{name} has been unreachable for {days} days"
                for name, days in health.dead_for_days(
                    cfg.health.warn_after_dead_days, event.config.run_at
                )
            ]
        markdown = render_markdown(
            event.items,
            event.config.hours,
            event.errors,
            event.simple,
            event.deep,
            run_at=event.config.run_at,
            health_warnings=warnings,
        )
        bus.publish(
            DigestRendered(
                items=event.items,
                markdown=markdown,
                outcomes=event.outcomes,
                simple=event.simple,
                deep=event.deep,
                config=event.config,
            )
        )

    bus.subscribe(CommentaryWritten, on_commentary_written)
