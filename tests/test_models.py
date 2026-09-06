from datetime import datetime, timezone

from ainews.models import Item, ScoreBreakdown, canonical_url, clean_text


def test_canonical_url_strips_tracking_params():
    assert canonical_url("https://a.com/x?utm_source=rss&id=5") == "https://a.com/x?id=5"


def test_canonical_url_normalises_trailing_slash_and_case():
    assert canonical_url("https://A.com/Post/") == canonical_url("https://a.com/post")


def test_clean_text_strips_markup_and_entities():
    assert clean_text("<p>Hello &amp; <b>world</b></p>") == "Hello & world"


def test_clean_text_truncates():
    assert len(clean_text("word " * 300, limit=50)) <= 52


def test_item_round_trips_through_json():
    item = Item(
        title="A story",
        url="https://example.com/a",
        source="OpenAI",
        feed_id="openai",
        category="Labs & Releases",
        published=datetime(2026, 9, 5, 8, 30, tzinfo=timezone.utc),
        summary="something happened",
        score=4.5,
        matched=["ai"],
        breakdown=ScoreBreakdown(feed_weight=4.0, recency=2.0),
    )
    restored = Item.from_dict(item.to_json())
    assert restored == item
    assert restored.uid == item.uid
    assert restored.breakdown.total == item.breakdown.total


def test_from_dict_tolerates_old_and_unknown_fields():
    """A digest written by an older version must still be readable, and a
    field this version dropped must not crash the load."""
    legacy = {
        "title": "Old story",
        "url": "https://example.com/old",
        "source": "OpenAI",
        "category": "Labs & Releases",
        "published": "2026-08-01T00:00:00+00:00",
        "retired_field": "whatever",
    }
    item = Item.from_dict(legacy)
    assert item.feed_id == ""  # added after that file was written
    assert item.breakdown is None
    assert item.title == "Old story"


def test_score_breakdown_totals_and_subtracts_mutes():
    breakdown = ScoreBreakdown(feed_weight=2.0, recency=3.0, keywords=0.6, boosts=1.2, mutes=3.0)
    assert breakdown.total == 3.8


def test_uid_is_stable_across_tracking_variants():
    a = Item(title="t", url="https://x.com/a?utm_source=rss", source="s",
             category="c", published=datetime.now(timezone.utc))
    b = Item(title="t", url="https://x.com/a/", source="s",
             category="c", published=datetime.now(timezone.utc))
    assert a.uid == b.uid
