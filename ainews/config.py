#!/usr/bin/env python3
"""
sources.yaml, loaded and validated into typed objects.

Previously this was four `setdefault` calls and a raw dict, so a typo like
`weight: 1.5x` surfaced as a ValueError from inside the scoring loop - on the
daily cron, with no indication of which feed was at fault. Now every problem
is caught at load time and named:

    sources.yaml: feeds[7] ('Wired AI'): weight must be a number, got '1.5x'

The scoring weights live here too. Tuning the ranking is the most likely
reason to touch this system, and it should not require a code change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from ainews.paths import project_root

def sources_file() -> Path:
    return project_root() / "sources.yaml"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class ConfigError(Exception):
    """sources.yaml is malformed. Always names the offending entry."""


def slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.strip().lower()).strip("-")


@dataclass(frozen=True)
class Feed:
    id: str
    name: str
    url: str
    weight: float = 1.0
    category: str = "Uncategorised"
    always: bool = False


@dataclass(frozen=True)
class Scoring:
    """Coefficients for analyst.score_item(). Defaults reproduce the original
    hardcoded formula exactly, so changing nothing changes nothing."""

    feed_weight: float = 2.0
    recency: float = 3.0
    keyword_match: float = 0.3
    max_keyword_matches: int = 5
    boost: float = 0.6
    max_boosts: int = 3
    mute: float = 3.0


@dataclass(frozen=True)
class Health:
    """When degraded sources should fail the run rather than be noted.

    A daily job's worst failure mode is not crashing, it's quietly returning
    less every day until you stop trusting it. These thresholds are what turn
    that into something loud.
    """

    fail_if_no_items: bool = True
    fail_above_dead_ratio: float = 0.5
    warn_after_dead_days: int = 3


@dataclass(frozen=True)
class Config:
    feeds: list[Feed] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    boost_terms: list[str] = field(default_factory=list)
    mute_terms: list[str] = field(default_factory=list)
    scoring: Scoring = field(default_factory=Scoring)
    health: Health = field(default_factory=Health)

    def feed_by_id(self, feed_id: str) -> Feed | None:
        for feed in self.feeds:
            if feed.id == feed_id:
                return feed
        return None


# ---------------------------------------------------------------------------
# Loading + validation
# ---------------------------------------------------------------------------


def _require_mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"sources.yaml: {where} must be a mapping, got {type(value).__name__}")
    return value


def _string_list(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"sources.yaml: {where} must be a list, got {type(value).__name__}")
    out: list[str] = []
    for n, entry in enumerate(value):
        if not isinstance(entry, str) or not entry.strip():
            raise ConfigError(f"sources.yaml: {where}[{n}] must be a non-empty string")
        out.append(entry.strip())
    return out


def _parse_feed(raw: Any, index: int) -> Feed:
    entry = _require_mapping(raw, f"feeds[{index}]")
    name = str(entry.get("name", "")).strip()
    where = f"feeds[{index}]" + (f" ({name!r})" if name else "")

    if not name:
        raise ConfigError(f"sources.yaml: {where}: name is required")

    url = str(entry.get("url", "")).strip()
    if not url:
        raise ConfigError(f"sources.yaml: {where}: url is required")
    if not url.lower().startswith(("http://", "https://")):
        raise ConfigError(f"sources.yaml: {where}: url must be http(s), got {url!r}")

    try:
        weight = float(entry.get("weight", 1.0))
    except (TypeError, ValueError):
        raise ConfigError(
            f"sources.yaml: {where}: weight must be a number, got {entry.get('weight')!r}"
        ) from None
    if weight < 0:
        raise ConfigError(f"sources.yaml: {where}: weight must not be negative")

    always = entry.get("always", False)
    if not isinstance(always, bool):
        raise ConfigError(f"sources.yaml: {where}: always must be true or false, got {always!r}")

    return Feed(
        id=str(entry.get("id") or slug(name)),
        name=name,
        url=url,
        weight=weight,
        category=str(entry.get("category", "Uncategorised")).strip() or "Uncategorised",
        always=always,
    )


def _parse_section(raw: Any, cls: type, where: str):
    """Build a Scoring/Health from a YAML block, rejecting unknown keys.

    Silently ignoring a misspelled key is how you end up convinced you've
    tuned something you haven't.
    """
    if raw is None:
        return cls()
    entry = _require_mapping(raw, where)
    known = {f.name for f in fields(cls)}
    unknown = set(entry) - known
    if unknown:
        raise ConfigError(
            f"sources.yaml: {where}: unknown key(s) {sorted(unknown)}; "
            f"valid keys are {sorted(known)}"
        )
    values: dict[str, Any] = {}
    for key, value in entry.items():
        default = getattr(cls(), key)
        try:
            values[key] = type(default)(value) if not isinstance(default, bool) else bool(value)
        except (TypeError, ValueError):
            raise ConfigError(
                f"sources.yaml: {where}.{key}: expected {type(default).__name__}, got {value!r}"
            ) from None
    return cls(**values)


def load_config(path: Path | None = None) -> Config:
    """Read and validate sources.yaml. Raises ConfigError with a usable message."""
    path = path or sources_file()
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        raise ConfigError(f"{path} not found") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from None

    raw = _require_mapping(raw, "top level")

    feeds_raw = raw.get("feeds") or []
    if not isinstance(feeds_raw, list):
        raise ConfigError("sources.yaml: feeds must be a list")
    feeds = [_parse_feed(entry, n) for n, entry in enumerate(feeds_raw)]

    # Feed id is the join key the analyst ranks by. Two feeds sharing one is
    # not a cosmetic clash: one feed's weight would silently apply to both.
    seen: dict[str, str] = {}
    for feed in feeds:
        if feed.id in seen:
            raise ConfigError(
                f"sources.yaml: two feeds resolve to the same id {feed.id!r} "
                f"({seen[feed.id]!r} and {feed.name!r}); rename one or give it an explicit `id:`"
            )
        seen[feed.id] = feed.name

    health = _parse_section(raw.get("health"), Health, "health")
    if not 0.0 <= health.fail_above_dead_ratio <= 1.0:
        raise ConfigError("sources.yaml: health.fail_above_dead_ratio must be between 0 and 1")

    return Config(
        feeds=feeds,
        keywords=_string_list(raw.get("keywords"), "keywords"),
        boost_terms=_string_list(raw.get("boost_terms"), "boost_terms"),
        mute_terms=_string_list(raw.get("mute_terms"), "mute_terms"),
        scoring=_parse_section(raw.get("scoring"), Scoring, "scoring"),
        health=health,
    )
