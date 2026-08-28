#!/usr/bin/env python3
"""
Offline pipeline tests - no network required.

Feeds a set of synthetic entries through the real filtering, scoring, dedup
and rendering code so the logic can be verified without hitting the internet.

    python tests/test_pipeline.py
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.analyst import build_digest, dedupe, term_pattern  # noqa: E402
from agents.writer import (  # noqa: E402
    detect_provider,
    load_voice_prompt,
    write_deep_dive,
    write_simple_summary,
)
from ai_news import markdown_to_html, render_html, render_markdown  # noqa: E402
from models import Item, canonical_url, clean_text, load_config  # noqa: E402

NOW = datetime.now(timezone.utc)
FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        FAILURES.append(label)


def make(title, source, category, hours_ago=1, url=None, summary=""):
    return Item(
        title=title,
        url=url or f"https://example.com/{abs(hash(title))}",
        source=source,
        category=category,
        published=NOW - timedelta(hours=hours_ago),
        summary=summary,
    )


print("\n--- helpers ---")
check(
    "canonical_url strips tracking params",
    canonical_url("https://a.com/x?utm_source=rss&id=5") == "https://a.com/x?id=5",
    canonical_url("https://a.com/x?utm_source=rss&id=5"),
)
check(
    "canonical_url normalises trailing slash + case",
    canonical_url("https://A.com/Post/") == canonical_url("https://a.com/post"),
)
check(
    "clean_text strips markup and entities",
    clean_text("<p>Hello &amp; <b>world</b></p>") == "Hello & world",
    clean_text("<p>Hello &amp; <b>world</b></p>"),
)
check("clean_text truncates", len(clean_text("word " * 300, limit=50)) <= 52)

print("\n--- keyword boundary matching ---")
for term, text, want in [
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
]:
    got = bool(term_pattern(term).search(text))
    check(f"{term!r} vs {text!r} -> {want}", got == want, f"got {got}")

print("\n--- dedup ---")
a = make("OpenAI ships GPT-6 today", "TechCrunch AI", "Industry & Press")
a.score = 3.0
b = Item(
    title="OpenAI ships GPT-6 today",
    url=a.url + "?utm_source=feed",
    source="VentureBeat AI",
    category="Industry & Press",
    published=NOW,
    score=5.0,
)
c = make("Completely different story", "Wired AI", "Industry & Press")
c.score = 1.0
deduped = dedupe([a, b, c])
check("same URL collapses to one item", len(deduped) == 2, f"got {len(deduped)}")
check(
    "dedup keeps the higher-scoring copy",
    any(i.source == "VentureBeat AI" for i in deduped),
)

d1 = make("Google DeepMind unveils new weather model", "The Verge AI", "Industry & Press")
d1.score = 2.0
d2 = make(
    "Google DeepMind unveils new weather model system",
    "Ars Technica AI",
    "Industry & Press",
    url="https://other.com/story",
)
d2.score = 4.0
check("near-identical headlines collapse", len(dedupe([d1, d2])) == 1)

print("\n--- filtering & scoring ---")
cfg = load_config()

raw = [
    make("Anthropic releases Claude update", "Anthropic", "Labs & Releases", 2),
    make("OpenAI announces new model", "OpenAI", "Labs & Releases", 1),
    make("Best sandwich recipes of 2026", "NVIDIA", "Labs & Releases", 1),
    make("New GPU cluster for LLM inference", "NVIDIA", "Labs & Releases", 3),
    make("Stale AI story from last week", "TechCrunch AI", "Industry & Press", 200),
    make("Sponsored webinar replay on AI", "Hacker News (AI, 100+ pts)", "Community", 1),
]

digest = build_digest(cfg, raw, hours=24, max_items=50, skip_seen=False)
titles = [i.title for i in digest]

check("items outside the window are dropped", "Stale AI story from last week" not in titles)
check(
    "keyword filter drops off-topic item from a non-'always' feed",
    "Best sandwich recipes of 2026" not in titles,
)
check(
    "on-topic item from a non-'always' feed is kept",
    "New GPU cluster for LLM inference" in titles,
)
check("items from 'always' feeds are kept", "Anthropic releases Claude update" in titles)
check("digest is sorted by score descending", digest == sorted(digest, key=lambda i: (-i.score, -i.published.timestamp())))
check(
    "high-weight fresh lab item outranks older lower-weight item",
    titles.index("OpenAI announces new model") < titles.index("New GPU cluster for LLM inference"),
)

muted = [i for i in digest if "Sponsored" in i.title]
check("mute terms push items down or out", not muted or muted[0].score < 0)

capped = build_digest(cfg, raw, hours=24, max_items=2, skip_seen=False)
check("max_items caps the digest", len(capped) == 2, f"got {len(capped)}")

print("\n--- rendering ---")
md = render_markdown(digest, 24, {"Dead Feed": "HTTP 404"})
check("markdown has a title", md.startswith("# AI News Update"))
check("markdown links each story", "](https://" in md)
check("markdown reports failed feeds", "Dead Feed" in md)
check("markdown groups by category", "## Labs & Releases" in md)

htm = render_html(digest, 24, {})
check("html is a full document", htm.startswith("<!DOCTYPE html>") and htm.endswith("</html>"))
check("html escapes content", "<script>" not in render_html(
    [make("<script>alert(1)</script>", "OpenAI", "Labs & Releases")], 24, {}
))
check("html contains story links", 'href="https://' in htm)

empty_md = render_markdown([], 24, {})
empty_html = render_html([], 24, {})
check("empty digest renders without crashing", "Nothing crossed" in empty_md and "Nothing crossed" in empty_html)

print("\n--- voice layer ---")
voice = load_voice_prompt()
check("PREFERENCES.md voice section loads", len(voice) > 500, f"{len(voice)} chars")
check("voice prompt mentions the Srinivas method", "Srinivas" in voice)
check("voice prompt excludes section 3", "Changing your mind" not in voice)
check("voice prompt strips the meta blockquote", "sent to the model as its instructions" not in voice)

prov, key = detect_provider()
check(
    "no API key -> voice layer disabled cleanly",
    (prov is None) or (key is not None),
    f"provider={prov}",
)
check("write_simple_summary returns None on empty input", write_simple_summary([]) is None)
check("write_deep_dive returns None on empty input", write_deep_dive([]) is None)

print("\n--- markdown -> html ---")
md_src = """## Top
This **matters** because *inference* costs fell.
- A [link](https://example.com) here
- `code` here

1. numbered
"""
converted = markdown_to_html(md_src)
check("headings convert", "<h3" in converted)
check("bold converts", "<strong>matters</strong>" in converted)
check("italic converts", "<em>inference</em>" in converted)
check("links convert", 'href="https://example.com"' in converted)
check("inline code converts", "<code" in converted)
check("bullets convert", "<ul" in converted and "<li" in converted)
check("numbered list converts", converted.count("<li") == 3, f"{converted.count('<li')} items")
check(
    "raw html from the model is escaped, not injected",
    "<script>" not in markdown_to_html("<script>alert(1)</script>"),
)
check("empty commentary is harmless", markdown_to_html("") == "")

with_comment = render_markdown(digest, 24, {}, simple_commentary="## Top\nQuiet day.")
check("simple commentary lands in markdown", "Quiet day." in with_comment and "## All stories" in with_comment)
html_with = render_html(digest, 24, {}, simple_commentary="## Top\nQuiet day.")
check("simple commentary lands in html", "Quiet day." in html_with)

with_deep = render_markdown(digest, 24, {}, deep_commentary="## Then\nThe detail.")
check("deep commentary lands in markdown", "The detail." in with_deep and "## All stories" in with_deep)
html_deep = render_html(digest, 24, {}, deep_commentary="## Then\nThe detail.")
check("deep commentary lands in html", "The detail." in html_deep)

check(
    "html without commentary is unchanged in shape",
    "All stories" not in render_html(digest, 24, {}),
)

print("\n" + ("-" * 40))
if FAILURES:
    print(f"{len(FAILURES)} test(s) FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("All tests passed.")
