#!/usr/bin/env python3
"""
Stage 1: Gatherer.

Fetches every feed in sources.yaml in parallel and returns raw Items -
no filtering, scoring, or ranking. That's the analyst's job (agents/analyst.py).

    python -m agents.gatherer --out raw.json
    python -m agents.gatherer --check-feeds
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import feedparser

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import Item, clean_text, load_config, parse_date  # noqa: E402

USER_AGENT = (
    "Mozilla/5.0 (compatible; AINewsUpdate/1.0; "
    "+https://github.com/) python-feedparser"
)
FETCH_TIMEOUT = 25
MAX_WORKERS = 12

# feedparser has no per-call timeout argument, so a hung feed connection would
# otherwise block its worker thread forever - and since a stuck feed also
# blocks ThreadPoolExecutor.map()'s in-order iteration, that can stall the
# entire run. This process-wide socket default is the standard fix.
socket.setdefaulttimeout(FETCH_TIMEOUT)


def fetch_feed(feed: dict[str, Any]) -> tuple[dict[str, Any], list[Item], str | None]:
    """Fetch one feed. Never raises - a dead source must not kill the run."""
    name = feed.get("name", feed.get("url", "unknown"))
    url = feed.get("url", "")
    category = feed.get("category", "Uncategorised")

    try:
        parsed = feedparser.parse(url, agent=USER_AGENT)
    except Exception as exc:  # network, DNS, malformed XML, anything
        return feed, [], f"{type(exc).__name__}: {exc}"

    status = getattr(parsed, "status", None)
    if status and status >= 400:
        return feed, [], f"HTTP {status}"
    if not parsed.entries:
        bozo = getattr(parsed, "bozo_exception", None)
        return feed, [], f"no entries ({bozo})" if bozo else "no entries"

    items: list[Item] = []
    for entry in parsed.entries:
        link = entry.get("link") or ""
        title = clean_text(entry.get("title", ""), limit=300)
        if not title or not link:
            continue
        summary = clean_text(
            entry.get("summary") or entry.get("description") or "", limit=400
        )
        published = parse_date(entry) or datetime.now(timezone.utc)
        items.append(
            Item(
                title=title,
                url=link,
                source=name,
                category=category,
                published=published,
                summary=summary,
            )
        )
    return feed, items, None


def collect(cfg: dict[str, Any]) -> tuple[list[Item], dict[str, str]]:
    """Fetch all feeds in parallel. Returns (items, {feed_name: error})."""
    items: list[Item] = []
    errors: dict[str, str] = {}
    feeds = cfg["feeds"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for feed, got, err in pool.map(fetch_feed, feeds):
            if err:
                errors[feed.get("name", feed.get("url", "?"))] = err
            items.extend(got)
    return items, errors


def check_feeds(cfg: dict[str, Any]) -> int:
    print(f"Checking {len(cfg['feeds'])} feeds\n")
    ok = dead = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for feed, items, err in pool.map(fetch_feed, cfg["feeds"]):
            name = feed.get("name", "?")
            if err:
                dead += 1
                print(f"  DEAD  {name:<30} {err}")
            else:
                ok += 1
                print(f"  OK    {name:<30} {len(items):>3} items")
    print(f"\n{ok} healthy, {dead} unreachable")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch raw items from every feed in sources.yaml.")
    ap.add_argument("--out", default="raw.json", help="where to write the raw items")
    ap.add_argument("--check-feeds", action="store_true", help="health-check sources and exit")
    args = ap.parse_args(argv)

    cfg = load_config()

    if args.check_feeds:
        return check_feeds(cfg)

    items, errors = collect(cfg)
    print(f"Fetched {len(items)} raw items; {len(errors)} feed(s) unreachable.")

    Path(args.out).write_text(
        json.dumps({"items": [i.to_json() for i in items], "errors": errors}, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
