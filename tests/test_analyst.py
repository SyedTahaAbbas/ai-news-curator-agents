from dataclasses import replace
from datetime import timedelta

import pytest

from ainews.agents.analyst import build_digest, dedupe, score_item, term_pattern
from ainews.config import Scoring
from tests.conftest import make_item


@pytest.mark.parametrize(
    "term, text, expected",
    [
        ("ai", "He said the chain was fine", False),
        ("ai", "AI-powered search launches", True),
        ("ai", "AI's impact on jobs", True),
        ("ai", "Certainly a bargain", False),
        ("gpt", "Egypt travel guide", False),
        ("llm", "LLM inference costs", True),
        ("inference", "Inferences about markets", False),
        ("fine-tun*", "Fine-tuning open models", True),
        ("open source", "An open-source release", True),
        ("benchmark*", "New benchmarks published", True),
    ],
)
def test_keyword_matching_respects_word_boundaries(term, text, expected):
    assert bool(term_pattern(term).search(text)) is expected


def test_exact_duplicates_collapse_keeping_the_higher_score():
    a = make_item("OpenAI ships GPT-6 today")
    a.score = 3.0
    b = make_item("OpenAI ships GPT-6 today", url=a.url + "?utm_source=feed",
                  source="VentureBeat AI", feed_id="vb")
    b.score = 5.0
    c = make_item("Completely different story")
    c.score = 1.0

    deduped = dedupe([a, b, c])
    assert len(deduped) == 2
    assert any(i.source == "VentureBeat AI" for i in deduped)


def test_near_identical_headlines_collapse():
    a = make_item("Google DeepMind unveils new weather model")
    a.score = 2.0
    b = make_item("Google DeepMind unveils new weather model system",
                  url="https://other.com/story")
    b.score = 4.0
    assert len(dedupe([a, b])) == 1


def test_window_keyword_filter_and_always_feeds(cfg, items, now):
    digest = build_digest(cfg, items, hours=24, max_items=50, now=now)
    titles = [i.title for i in digest]

    assert "Stale AI story from last week" not in titles       # outside the window
    assert "Best sandwich recipes of 2026" not in titles       # off-topic, filtered feed
    assert "New GPU cluster for LLM inference" in titles       # on-topic, filtered feed
    assert "Anthropic releases Claude update" in titles        # `always` feed


def test_digest_is_sorted_by_score_then_recency(cfg, items, now):
    digest = build_digest(cfg, items, hours=24, max_items=50, now=now)
    assert digest == sorted(digest, key=lambda i: (-i.score, -i.published.timestamp()))


def test_max_items_caps_the_digest(cfg, items, now):
    assert len(build_digest(cfg, items, hours=24, max_items=2, now=now)) == 2


def test_mute_terms_push_items_down(cfg, items, now):
    digest = build_digest(cfg, items, hours=24, max_items=50, now=now)
    muted = [i for i in digest if "Sponsored" in i.title]
    assert not muted or muted[0].score < 0


def test_recency_curve_spans_the_actual_window(cfg, now):
    """The curve was hardcoded to 24h: with --hours 48 everything older than a
    day scored an identical 0.0 and half the window ranked on feed weight alone."""
    fresh = make_item("LLM inference gets cheaper", hours_ago=1)
    middling = make_item("Another LLM inference story", hours_ago=30)
    stale = make_item("A third LLM inference story", hours_ago=46)

    scores = [
        score_item(i, 1.0, cfg, now, window_hours=48).recency
        for i in (fresh, middling, stale)
    ]
    assert scores[0] > scores[1] > scores[2] > 0.0


def test_scoring_weights_come_from_config(cfg, now):
    item = make_item("An LLM release", summary="ai inference")
    item.matched = ["ai", "llm"]

    default = score_item(item, 2.0, cfg, now, 24)
    louder = score_item(item, 2.0, replace(cfg, scoring=Scoring(recency=30.0)), now, 24)
    assert louder.recency == pytest.approx(default.recency * 10)
    assert louder.total > default.total


def test_score_breakdown_explains_the_total(cfg, now):
    item = make_item("OpenAI announces a new model", summary="ai llm inference")
    digest = build_digest(cfg, [item], hours=24, max_items=5, now=now)
    breakdown = digest[0].breakdown
    assert breakdown is not None
    assert breakdown.total == digest[0].score
    assert breakdown.recency > 0 and breakdown.keywords > 0


def test_weights_join_on_feed_id_not_display_name(cfg, now):
    """Two feeds can share a display label; the weight must follow the id."""
    heavy = make_item("An AI story", feed_id="openai", source="Same Label", hours_ago=1)
    light = make_item("A different AI story", feed_id="techcrunch", source="Same Label", hours_ago=1)
    digest = build_digest(cfg, [heavy, light], hours=24, max_items=5, now=now)
    by_title = {i.title: i for i in digest}
    assert by_title["An AI story"].breakdown.feed_weight > by_title["A different AI story"].breakdown.feed_weight


def test_seen_items_are_skipped(cfg, items, now, seen_store):
    first = build_digest(cfg, items, hours=24, max_items=50, now=now, seen_store=seen_store)
    seen_store.mark([i.uid for i in first], now)

    again = build_digest(cfg, items, hours=24, max_items=50, now=now, seen_store=seen_store)
    # everything was seen, so it falls back to the full set rather than mailing nothing
    assert len(again) == len(first)

    partial = build_digest(cfg, items, hours=24, max_items=50, now=now, seen_store=seen_store)
    assert partial  # never empty


def test_unseen_items_survive_when_only_some_were_sent(cfg, items, now, seen_store):
    digest = build_digest(cfg, items, hours=24, max_items=50, now=now, seen_store=seen_store)
    seen_store.mark([digest[0].uid], now)

    again = build_digest(cfg, items, hours=24, max_items=50, now=now, seen_store=seen_store)
    assert digest[0].uid not in {i.uid for i in again}
    assert len(again) == len(digest) - 1


def test_the_window_is_measured_from_run_at(cfg, now):
    """`now` is injected, so a run that straddles midnight can't disagree with
    itself about which stories are in the window."""
    item = make_item("An AI story", hours_ago=25)
    assert not build_digest(cfg, [item], hours=24, max_items=5, now=now)
    assert build_digest(cfg, [item], hours=24, max_items=5, now=now + timedelta(hours=-2))
