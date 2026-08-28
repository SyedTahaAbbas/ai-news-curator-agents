#!/usr/bin/env python3
"""
Stage 2: Analyst.

Takes the gatherer's raw items and turns them into a ranked, deduped
shortlist: keyword filter, weighted scoring, exact + near-duplicate
collapsing, and a check against what's already been sent (.seen.json).

This is deterministic today, same logic as before the pipeline was split
into stages - no LLM involved yet. It's split out on its own specifically
so the ranking can keep improving independently later without touching the
gathering or writing stages.

    python -m agents.analyst --in raw.json --out ranked.json --hours 24 --max-items 35
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import Item, load_config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / ".seen.json"
SEEN_RETENTION_DAYS = 30


@lru_cache(maxsize=512)
def term_pattern(term: str) -> re.Pattern[str]:
    """Word-boundary matcher for a keyword phrase.

    Substring matching is too loose: 'ai' would fire on 'said', 'rag' on
    'fragment'. Boundaries keep 'AI', "AI's" and 'AI-powered' matching while
    ignoring the middle of unrelated words. A trailing '*' means prefix match
    (e.g. 'fine-tun*' covers tune/tuned/tuning).
    """
    term = term.strip().lower()
    prefix = term.endswith("*")
    core = term[:-1] if prefix else term
    escaped = r"[\s\-]+".join(re.escape(part) for part in core.split())
    tail = r"\w*" if prefix else r"\b"
    return re.compile(rf"\b{escaped}{tail}", re.IGNORECASE)


def matches_keywords(item: Item, keywords: Iterable[str]) -> list[str]:
    haystack = f"{item.title}. {item.summary}"
    return [kw for kw in keywords if term_pattern(kw).search(haystack)]


def score_item(item: Item, feed_weight: float, cfg: dict[str, Any], now: datetime) -> float:
    """Higher is better. Recency dominates, then source weight, then topic hits."""
    age_hours = max((now - item.published).total_seconds() / 3600.0, 0.0)
    recency = max(0.0, 24.0 - age_hours) / 24.0  # 1.0 fresh -> 0.0 at 24h

    text = f"{item.title}. {item.summary}"
    boosts = sum(1 for t in cfg["boost_terms"] if term_pattern(t).search(text))
    mutes = sum(1 for t in cfg["mute_terms"] if term_pattern(t).search(text))

    score = (
        feed_weight * 2.0
        + recency * 3.0
        + min(len(item.matched), 5) * 0.3
        + min(boosts, 3) * 0.6
        - mutes * 3.0
    )
    return round(score, 3)


_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "with",
    "is", "are", "as", "at", "by", "its", "it", "new", "this", "that",
}
NEAR_DUP_THRESHOLD = 0.75


def title_tokens(title: str) -> frozenset[str]:
    words = re.sub(r"[^a-z0-9 ]", " ", title.lower()).split()
    return frozenset(w for w in words if w not in _STOPWORDS and len(w) > 2)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def dedupe(items: list[Item]) -> list[Item]:
    """Same story from several outlets: keep the highest-scoring copy.

    Two passes: exact (canonical URL / title hash), then near-duplicate
    headlines via Jaccard overlap of significant title words, so
    "X unveils weather model" and "X unveils weather model system" collapse.
    """
    best: dict[str, Item] = {}
    for item in items:
        key = item.uid
        if key not in best or item.score > best[key].score:
            best[key] = item

    # Highest score first so the winner of each cluster is kept.
    candidates = sorted(best.values(), key=lambda i: -i.score)
    kept: list[tuple[frozenset[str], Item]] = []
    for item in candidates:
        tokens = title_tokens(item.title)
        if any(jaccard(tokens, seen) >= NEAR_DUP_THRESHOLD for seen, _ in kept):
            continue
        kept.append((tokens, item))
    return [item for _, item in kept]


def load_seen() -> dict[str, str]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_seen(seen: dict[str, str]) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=SEEN_RETENTION_DAYS)
    pruned = {}
    for uid, iso in seen.items():
        try:
            if datetime.fromisoformat(iso) >= cutoff:
                pruned[uid] = iso
        except ValueError:
            continue
    STATE_FILE.write_text(json.dumps(pruned, indent=0), encoding="utf-8")


def build_digest(
    cfg: dict[str, Any],
    items: list[Item],
    hours: int,
    max_items: int,
    skip_seen: bool = True,
) -> list[Item]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    weights = {f.get("name"): float(f.get("weight", 1.0)) for f in cfg["feeds"]}
    always = {f.get("name") for f in cfg["feeds"] if f.get("always")}
    keywords = cfg["keywords"]

    kept: list[Item] = []
    for item in items:
        if item.published < cutoff:
            continue
        if item.source in always:
            item.matched = matches_keywords(item, keywords)
        else:
            item.matched = matches_keywords(item, keywords)
            if not item.matched:
                continue
        item.score = score_item(item, weights.get(item.source, 1.0), cfg, now)
        kept.append(item)

    kept = dedupe(kept)

    if skip_seen:
        seen = load_seen()
        fresh = [i for i in kept if i.uid not in seen]
        # if everything was already sent, fall back to the full set rather
        # than mailing an empty digest
        kept = fresh or kept

    kept.sort(key=lambda i: (-i.score, -i.published.timestamp()))
    return kept[:max_items]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Rank, filter, and dedupe raw items.")
    ap.add_argument("--in", dest="input", default="raw.json", help="raw items from the gatherer")
    ap.add_argument("--out", default="ranked.json", help="where to write the ranked shortlist")
    ap.add_argument("--hours", type=int, default=24, help="lookback window (default 24)")
    ap.add_argument("--max-items", type=int, default=35, help="cap on digest length")
    ap.add_argument("--include-seen", action="store_true", help="do not skip previously sent items")
    args = ap.parse_args(argv)

    raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
    items = [Item.from_dict(d) for d in raw["items"]]
    errors = raw.get("errors", {})

    cfg = load_config()
    ranked = build_digest(cfg, items, args.hours, args.max_items, skip_seen=not args.include_seen)
    print(f"{len(ranked)} items kept after filtering, scoring, and dedup.")

    Path(args.out).write_text(
        json.dumps({"items": [i.to_json() for i in ranked], "errors": errors}, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
