#!/usr/bin/env python3
"""
Stage 2: Analyst.

Listens for StoriesGathered and turns those raw items into a ranked, deduped
shortlist: keyword filter, weighted scoring, exact + near-duplicate collapsing,
and a check against what has already been sent. The result goes out as
StoriesRanked; who consumes it is not this stage's problem.

Deterministic - no LLM involved. Two things that used to be hardcoded here now
are not, because this is the stage most likely to keep changing:

* the scoring coefficients live in sources.yaml (`scoring:`), so tuning the
  ranking is a config edit rather than a code change;
* every score carries a breakdown of what produced it, so a story in the wrong
  place can be diagnosed from the digest's JSON sibling.
"""

from __future__ import annotations

import re
from datetime import timedelta
from functools import lru_cache
from typing import Iterable

from ainews.bus import EventBus
from ainews.config import Config
from ainews.events import StoriesGathered, StoriesRanked
from ainews.models import Item, ScoreBreakdown
from ainews.state import SeenStore


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


def score_item(item: Item, feed_weight: float, cfg: Config, now, window_hours: int) -> ScoreBreakdown:
    """Higher is better. Recency dominates, then source weight, then topic hits.

    The recency curve spans the actual lookback window. It used to be hardcoded
    to 24 hours regardless: with `--hours 48`, everything older than a day
    scored an identical 0.0 for recency, so half the window was ranked on
    source weight alone.
    """
    weights = cfg.scoring
    age_hours = max((now - item.published).total_seconds() / 3600.0, 0.0)
    span = float(max(window_hours, 1))
    recency = max(0.0, span - age_hours) / span  # 1.0 fresh -> 0.0 at the edge

    text = f"{item.title}. {item.summary}"
    boosts = sum(1 for t in cfg.boost_terms if term_pattern(t).search(text))
    mutes = sum(1 for t in cfg.mute_terms if term_pattern(t).search(text))

    return ScoreBreakdown(
        feed_weight=round(feed_weight * weights.feed_weight, 3),
        recency=round(recency * weights.recency, 3),
        keywords=round(
            min(len(item.matched), weights.max_keyword_matches) * weights.keyword_match, 3
        ),
        boosts=round(min(boosts, weights.max_boosts) * weights.boost, 3),
        mutes=round(mutes * weights.mute, 3),
    )


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


def build_digest(
    cfg: Config,
    items: list[Item],
    hours: int,
    max_items: int,
    now,
    seen_store: SeenStore | None = None,
) -> list[Item]:
    """Filter to the window, score, dedupe, drop what's already been sent."""
    cutoff = now - timedelta(hours=hours)
    # Joined on feed id, not on the display name: two feeds sharing a name
    # used to silently share one weight.
    weights = {feed.id: feed.weight for feed in cfg.feeds}
    always = {feed.id for feed in cfg.feeds if feed.always}

    kept: list[Item] = []
    for item in items:
        if item.published < cutoff:
            continue
        item.matched = matches_keywords(item, cfg.keywords)
        if not item.matched and item.feed_id not in always:
            continue
        item.breakdown = score_item(item, weights.get(item.feed_id, 1.0), cfg, now, hours)
        item.score = item.breakdown.total
        kept.append(item)

    kept = dedupe(kept)

    if seen_store is not None:
        seen = seen_store.load()
        fresh = [i for i in kept if i.uid not in seen]
        # if everything was already sent, fall back to the full set rather
        # than mailing an empty digest
        kept = fresh or kept

    kept.sort(key=lambda i: (-i.score, -i.published.timestamp()))
    return kept[:max_items]


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def register(bus: EventBus, cfg: Config, seen_store: SeenStore) -> None:
    """Subscribe this agent to the bus.

    The seen store arrives as a dependency rather than being a module-level
    file path, so tests get an in-memory one and never touch real state.
    """

    def on_stories_gathered(event: StoriesGathered, bus: EventBus) -> None:
        ranked = build_digest(
            cfg,
            event.items,
            event.config.hours,
            event.config.max_items,
            now=event.config.run_at,
            seen_store=None if event.config.include_seen else seen_store,
        )
        print(f"{len(ranked)} items kept after filtering, scoring, and dedup.")
        bus.publish(
            StoriesRanked(items=ranked, outcomes=event.outcomes, config=event.config)
        )

    bus.subscribe(StoriesGathered, on_stories_gathered)
