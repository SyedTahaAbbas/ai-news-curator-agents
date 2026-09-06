#!/usr/bin/env python3
"""The filesystem channel: `see news/<date>.md`, its JSON sibling, latest.md."""

from __future__ import annotations

import json
from pathlib import Path

from ainews.bus import EventBus
from ainews.events import DigestFilesWritten, DigestRendered
from ainews.models import Item
from ainews.paths import project_root


def digest_dir_default() -> Path:
    return project_root() / "see news"


def write_digest_files(
    items: list[Item], markdown: str, stamp: str, digest_dir: Path | None = None
) -> tuple[Path, Path]:
    """Write the digest and return (markdown path, json path).

    `stamp` is the run's date, taken from RunConfig.run_at rather than from the
    clock, so a run that straddles midnight files everything under one date.
    """
    digest_dir = digest_dir or digest_dir_default()
    digest_dir.mkdir(parents=True, exist_ok=True)
    md_path = digest_dir / f"{stamp}.md"
    json_path = digest_dir / f"{stamp}.json"
    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(
        json.dumps([i.to_json() for i in items], indent=2), encoding="utf-8"
    )
    (digest_dir.parent / "latest.md").write_text(markdown, encoding="utf-8")
    return md_path, json_path


def register_files(bus: EventBus, digest_dir: Path | None = None) -> None:
    def write_files(event: DigestRendered, bus: EventBus) -> None:
        if event.config.dry_run:
            return
        target = digest_dir or digest_dir_default()
        md_path, json_path = write_digest_files(
            event.items,
            event.markdown,
            f"{event.config.run_at:%Y-%m-%d}",
            target,
        )
        print(f"Wrote {md_path.name} and {json_path.name} in {target.name}/")
        bus.publish(
            DigestFilesWritten(markdown_path=str(md_path), json_path=str(json_path))
        )

    def print_digest(event: DigestRendered, bus: EventBus) -> None:
        if event.config.dry_run:
            print("\n" + event.markdown)

    bus.subscribe(DigestRendered, print_digest)
    bus.subscribe(DigestRendered, write_files)
