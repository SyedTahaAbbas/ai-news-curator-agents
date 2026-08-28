#!/usr/bin/env python3
"""
Shared data model and config loader.

Used by every stage of the pipeline (agents/gatherer.py, agents/analyst.py,
ai_news.py) so an Item produced by one stage can be serialised to JSON,
handed to the next stage as a file, and read back identically.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent
SOURCES_FILE = ROOT / "sources.yaml"

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
    import time

    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc)
            except (ValueError, OverflowError, TypeError):
                continue
    return None


def load_config(path: Path = SOURCES_FILE) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    cfg.setdefault("feeds", [])
    cfg.setdefault("keywords", [])
    cfg.setdefault("boost_terms", [])
    cfg.setdefault("mute_terms", [])
    return cfg


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
        """Reverse of to_json() - reconstitutes an Item read back from an
        intermediate JSON file (raw.json / ranked.json) passed between
        pipeline stages."""
        d = dict(d)
        d["published"] = datetime.fromisoformat(d["published"])
        return cls(**d)
