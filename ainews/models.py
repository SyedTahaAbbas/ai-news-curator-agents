#!/usr/bin/env python3
"""
The Item, and the text helpers that produce one.

An Item is what every stage passes around. It round-trips through JSON
unchanged, so the same object can travel over the in-process bus or through a
file between two CI steps and be indistinguishable at the far end.
"""

from __future__ import annotations

import hashlib
import html
import re
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any

_TRACKING_PARAMS = re.compile(
    r"(?:^|&)(utm_[^=]+|ref|ref_src|source|fbclid|gclid|mc_cid|mc_eid)=[^&]*"
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def canonical_url(url: str) -> str:
    """Strip tracking params and trailing slashes so the same story dedups."""
    if not url:
        return ""
    url = url.split("#", 1)[0]
    if "?" in url:
        base, _, query = url.partition("?")
        query = _TRACKING_PARAMS.sub("", query).strip("&")
        url = f"{base}?{query}" if query else base
    return url.rstrip("/").lower()


def clean_text(raw: str, limit: int = 400) -> str:
    """Feed summaries are full of markup and whitespace. Flatten them."""
    if not raw:
        return ""
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def parse_date(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc)
            except (ValueError, OverflowError, TypeError):
                continue
    return None


@dataclass
class ScoreBreakdown:
    """Why an item scored what it did.

    The ranking is the part of this system most likely to keep changing, and a
    bare float tells you nothing when a story lands in the wrong place. Every
    component that went into the total is kept and written to the digest's JSON
    sibling, so a bad ranking can be diagnosed from the artifact alone.
    """

    feed_weight: float = 0.0
    recency: float = 0.0
    keywords: float = 0.0
    boosts: float = 0.0
    mutes: float = 0.0

    @property
    def total(self) -> float:
        return round(
            self.feed_weight + self.recency + self.keywords + self.boosts - self.mutes, 3
        )


@dataclass
class Item:
    title: str
    url: str
    source: str
    category: str
    published: datetime
    summary: str = ""
    score: float = 0.0
    matched: list[str] = field(default_factory=list)
    # Stable join key back to the feed in sources.yaml. `source` is the display
    # label and may be renamed or duplicated; this may not.
    feed_id: str = ""
    breakdown: ScoreBreakdown | None = None

    @property
    def uid(self) -> str:
        """Stable id: canonical URL if we have one, else a title hash."""
        key = canonical_url(self.url) or self.title.lower().strip()
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["published"] = self.published.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Item":
        """Reverse of to_json().

        Tolerant on purpose: digests written by an older version of this code
        are still readable (missing fields take their defaults), and fields
        this version no longer knows about are dropped rather than raising.
        """
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in d.items() if k in known}
        published = data.get("published")
        if isinstance(published, str):
            data["published"] = datetime.fromisoformat(published)
        breakdown = data.get("breakdown")
        if isinstance(breakdown, dict):
            valid = {f.name for f in fields(ScoreBreakdown)}
            data["breakdown"] = ScoreBreakdown(
                **{k: v for k, v in breakdown.items() if k in valid}
            )
        return cls(**data)
