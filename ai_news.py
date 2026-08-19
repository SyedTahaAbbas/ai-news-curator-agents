#!/usr/bin/env python3
"""
AI News Update
==============
Collects AI news every day from public RSS/Atom feeds, ranks it, renders a
digest, and emails it.

No Twitter/X scraping: X requires login for search and actively blocks
automated clients, so anonymous scraping is both unreliable and against their
terms. The feeds in sources.yaml cover the same ground (labs, press, research,
HN/Reddit) without auth, cost, or breakage.

Usage
-----
    python ai_news.py                     # collect last 24h, write digest, email it
    python ai_news.py --hours 48          # widen the window
    python ai_news.py --no-email          # write files only
    python ai_news.py --dry-run           # print to stdout, write nothing
    python ai_news.py --check-feeds       # health-check every source and exit
    python ai_news.py --max-items 40      # cap digest length

Environment (see .env.example)
------------------------------
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, MAIL_FROM, MAIL_TO
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import feedparser
import yaml

try:  # optional: load a local .env when present
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional
    pass

from emailer import send_digest_email
from summarizer import write_commentary

ROOT = Path(__file__).resolve().parent
SOURCES_FILE = ROOT / "sources.yaml"
DIGEST_DIR = ROOT / "see news"
STATE_FILE = ROOT / ".seen.json"

USER_AGENT = (
    "Mozilla/5.0 (compatible; AINewsUpdate/1.0; "
    "+https://github.com/) python-feedparser"
)
FETCH_TIMEOUT = 25
MAX_WORKERS = 12
SEEN_RETENTION_DAYS = 30


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def load_config(path: Path = SOURCES_FILE) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    cfg.setdefault("feeds", [])
    cfg.setdefault("keywords", [])
    cfg.setdefault("boost_terms", [])
    cfg.setdefault("mute_terms", [])
    return cfg


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Filtering, scoring, dedup
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

CATEGORY_ORDER = ["Labs & Releases", "Industry & Press", "Research", "Community"]


def group_by_category(items: list[Item]) -> list[tuple[str, list[Item]]]:
    buckets: dict[str, list[Item]] = {}
    for item in items:
        buckets.setdefault(item.category, []).append(item)
    ordered = [(c, buckets.pop(c)) for c in CATEGORY_ORDER if c in buckets]
    ordered += sorted(buckets.items())
    return ordered


def render_markdown(
    items: list[Item],
    hours: int,
    errors: dict[str, str],
    commentary: str | None = None,
) -> str:
    today = datetime.now(timezone.utc)
    lines = [
        f"# AI News Update - {today:%A, %d %B %Y}",
        "",
        f"*{len(items)} stories from the last {hours} hours. "
        f"Generated {today:%Y-%m-%d %H:%M UTC}.*",
        "",
    ]

    if commentary:
        lines += [commentary.strip(), "", "---", "", "## All stories", ""]

    if not items:
        lines += ["Nothing crossed the threshold today.", ""]

    for category, group in group_by_category(items):
        lines += [f"## {category}", ""]
        for item in group:
            lines.append(f"### [{item.title}]({item.url})")
            lines.append(
                f"`{item.source}` · {item.published:%d %b %H:%M UTC} · score {item.score}"
            )
            if item.summary:
                lines += ["", item.summary]
            lines.append("")

    if errors:
        lines += ["---", "", "<details><summary>Feeds that did not respond</summary>", ""]
        for name, err in sorted(errors.items()):
            lines.append(f"- **{name}** - {err}")
        lines += ["", "</details>", ""]
    return "\n".join(lines)


_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_MD_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_MD_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_MD_CODE = re.compile(r"`([^`]+)`")


def markdown_to_html(text: str) -> str:
    """Minimal Markdown -> HTML for the model's commentary.

    Deliberately small: headings, bold, italic, inline code, links, bullet and
    numbered lists, paragraphs. Everything is escaped first, so a model that
    emits raw HTML cannot inject it into the email.
    """
    out: list[str] = []
    in_list = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    def inline(s: str) -> str:
        s = html.escape(s)
        s = _MD_CODE.sub(
            r'<code style="background:#f3f4f6;padding:1px 4px;border-radius:3px;'
            r'font-size:12px;">\1</code>',
            s,
        )
        s = _MD_BOLD.sub(r"<strong>\1</strong>", s)
        s = _MD_ITALIC.sub(r"<em>\1</em>", s)
        s = _MD_LINK.sub(r'<a href="\2" style="color:#4f46e5;">\1</a>', s)
        return s

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            close_list()
            continue
        if line.strip() in {"---", "***", "___"}:
            close_list()
            out.append("<hr style='border:0;border-top:1px solid #e5e7eb;margin:18px 0;'>")
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            close_list()
            level = min(len(heading.group(1)) + 1, 6)
            size = {2: 17, 3: 15, 4: 14}.get(level, 13)
            out.append(
                f"<h{level} style='font-size:{size}px;margin:20px 0 8px;"
                f"color:#111827;'>{inline(heading.group(2))}</h{level}>"
            )
            continue

        bullet = re.match(r"^\s*[-*+]\s+(.*)$", line) or re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if bullet:
            if not in_list:
                out.append("<ul style='margin:8px 0;padding-left:20px;'>")
                in_list = True
            out.append(
                f"<li style='margin:4px 0;font-size:14px;color:#374151;'>"
                f"{inline(bullet.group(1))}</li>"
            )
            continue

        close_list()
        out.append(
            f"<p style='margin:10px 0;font-size:14px;line-height:1.6;color:#374151;'>"
            f"{inline(line)}</p>"
        )

    close_list()
    return "".join(out)


def render_html(
    items: list[Item],
    hours: int,
    errors: dict[str, str],
    commentary: str | None = None,
) -> str:
    today = datetime.now(timezone.utc)
    esc = html.escape

    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>AI News Update</title></head>",
        "<body style=\"margin:0;padding:0;background:#f4f5f7;\">",
        "<div style=\"max-width:680px;margin:0 auto;padding:24px 16px;"
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
        "color:#1a1a1a;line-height:1.5;\">",
        "<div style=\"background:#ffffff;border-radius:12px;padding:28px 26px;"
        "box-shadow:0 1px 3px rgba(0,0,0,0.08);\">",
        f"<h1 style=\"margin:0 0 4px;font-size:22px;letter-spacing:-0.3px;\">AI News Update</h1>",
        f"<p style=\"margin:0 0 24px;color:#6b7280;font-size:13px;\">"
        f"{today:%A, %d %B %Y} &middot; {len(items)} stories from the last {hours}h</p>",
    ]

    if commentary:
        parts.append(
            "<div style=\"background:#fafafa;border-left:3px solid #6366f1;"
            "border-radius:0 6px 6px 0;padding:4px 18px 10px;margin:0 0 26px;\">"
            + markdown_to_html(commentary)
            + "</div>"
        )
        parts.append(
            "<h2 style=\"font-size:12px;text-transform:uppercase;letter-spacing:1.2px;"
            "color:#9ca3af;margin:0 0 4px;\">All stories</h2>"
        )

    if not items:
        parts.append(
            "<p style='color:#6b7280;'>Nothing crossed the threshold today.</p>"
        )

    for category, group in group_by_category(items):
        parts.append(
            "<h2 style=\"font-size:12px;text-transform:uppercase;letter-spacing:1.2px;"
            "color:#6366f1;margin:28px 0 12px;border-bottom:1px solid #e5e7eb;"
            f"padding-bottom:6px;\">{esc(category)}</h2>"
        )
        for item in group:
            parts.append("<div style='margin:0 0 18px;'>")
            parts.append(
                f"<a href=\"{esc(item.url)}\" style=\"color:#111827;font-weight:600;"
                f"font-size:15px;text-decoration:none;\">{esc(item.title)}</a>"
            )
            parts.append(
                f"<div style='color:#9ca3af;font-size:12px;margin:3px 0 0;'>"
                f"{esc(item.source)} &middot; {item.published:%d %b %H:%M UTC}</div>"
            )
            if item.summary:
                parts.append(
                    f"<div style='color:#4b5563;font-size:13px;margin:6px 0 0;'>"
                    f"{esc(item.summary)}</div>"
                )
            parts.append("</div>")

    if errors:
        parts.append(
            "<p style='color:#9ca3af;font-size:11px;margin-top:28px;"
            "border-top:1px solid #e5e7eb;padding-top:12px;'>"
            f"{len(errors)} feed(s) did not respond: "
            + esc(", ".join(sorted(errors)))
            + "</p>"
        )

    parts += [
        "<p style='color:#9ca3af;font-size:11px;margin-top:20px;'>"
        "Generated automatically by AI News Update.</p>",
        "</div></div></body></html>",
    ]
    return "".join(parts)


# ---------------------------------------------------------------------------
# Feed health check
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Daily AI news digest from public feeds.")
    ap.add_argument("--hours", type=int, default=24, help="lookback window (default 24)")
    ap.add_argument("--max-items", type=int, default=35, help="cap on digest length")
    ap.add_argument("--no-email", action="store_true", help="write files but do not send")
    ap.add_argument("--dry-run", action="store_true", help="print only, write nothing")
    ap.add_argument("--check-feeds", action="store_true", help="health-check sources and exit")
    ap.add_argument("--include-seen", action="store_true", help="do not skip previously sent items")
    ap.add_argument(
        "--no-commentary",
        action="store_true",
        help="skip the LLM voice layer even if an API key is set",
    )
    args = ap.parse_args(argv)

    cfg = load_config()

    if args.check_feeds:
        return check_feeds(cfg)

    raw, errors = collect(cfg)
    print(f"Fetched {len(raw)} raw items; {len(errors)} feed(s) unreachable.")

    items = build_digest(
        cfg, raw, args.hours, args.max_items, skip_seen=not args.include_seen
    )
    print(f"{len(items)} items in today's digest.")

    commentary = None if args.no_commentary else write_commentary(items)

    markdown = render_markdown(items, args.hours, errors, commentary)

    if args.dry_run:
        print("\n" + markdown)
        return 0

    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    md_path = DIGEST_DIR / f"{stamp}.md"
    json_path = DIGEST_DIR / f"{stamp}.json"
    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(
        json.dumps([i.to_json() for i in items], indent=2), encoding="utf-8"
    )
    (ROOT / "latest.md").write_text(markdown, encoding="utf-8")
    print(f"Wrote {md_path.relative_to(ROOT)} and {json_path.relative_to(ROOT)}")

    if not args.no_email:
        subject = f"AI News Update - {datetime.now(timezone.utc):%d %b %Y} ({len(items)} stories)"
        sent = send_digest_email(
            subject=subject,
            html_body=render_html(items, args.hours, errors, commentary),
            text_body=markdown,
        )
        if not sent:
            print("Email not sent (see message above).", file=sys.stderr)

    now_iso = datetime.now(timezone.utc).isoformat()
    seen = load_seen()
    seen.update({i.uid: now_iso for i in items})
    save_seen(seen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
