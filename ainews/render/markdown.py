#!/usr/bin/env python3
"""The digest as Markdown - the file that lands in `see news/`."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from ainews.models import Item

CATEGORY_ORDER = ["Labs & Releases", "Industry & Press", "Research", "Community"]

_SAFE_URL_SCHEMES = ("http://", "https://")


def safe_href(url: str) -> str:
    """Item URLs come from external feeds we don't control. Only ever emit
    http(s) links - anything else (javascript:, data:, etc.) becomes a dead
    link instead of a clickable one."""
    return url if url.lower().startswith(_SAFE_URL_SCHEMES) else "#"


# ---------------------------------------------------------------------------
# Escaping untrusted feed text
#
# Titles, summaries and error strings are written by whoever runs the feed,
# and the digest they land in is committed to a repo and rendered on the web.
# The HTML renderer has escaped them all along; this file used to interpolate
# them raw, so a title of `](javascript:alert(1))[` could break out of its own
# link, and a summary beginning "# " became a heading in someone else's digest.
# ---------------------------------------------------------------------------

# `<` is included so untrusted text cannot embed raw HTML in a document that
# some other renderer may be more permissive about than GitHub is.
_MD_SPECIALS = re.compile(r"([\\\[\]`<>])")
_BLOCK_MARKER = re.compile(r"^(\s*)([#>|*+-]|\d+[.)])")
# Characters that terminate or escape a Markdown link target.
_URL_UNSAFE = {" ": "%20", "(": "%28", ")": "%29", "<": "%3C", ">": "%3E", '"': "%22"}


def escape_md(text: str) -> str:
    """Neutralise inline Markdown syntax in untrusted text."""
    return _MD_SPECIALS.sub(r"\\\1", text)


def escape_md_block(text: str) -> str:
    """As escape_md, plus the leading marker that would start a new block -
    a heading, quote, list item or table row."""
    return _BLOCK_MARKER.sub(r"\1\\\2", escape_md(text))


def safe_md_url(url: str) -> str:
    """A link target that cannot escape its own parentheses, and can only ever
    be http(s)."""
    return "".join(_URL_UNSAFE.get(c, c) for c in safe_href(url))


def md_link(text: str, url: str) -> str:
    return f"[{escape_md(text)}]({safe_md_url(url)})"


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
    simple_commentary: str | None = None,
    deep_commentary: str | None = None,
    run_at: datetime | None = None,
    health_warnings: list[str] | None = None,
) -> str:
    today = run_at or datetime.now(timezone.utc)
    lines = [
        f"# AI News Curator - {today:%A, %d %B %Y}",
        "",
        f"*{len(items)} stories from the last {hours} hours. "
        f"Generated {today:%Y-%m-%d %H:%M UTC}.*",
        "",
    ]

    has_commentary = bool(simple_commentary or deep_commentary)
    if simple_commentary:
        lines += [simple_commentary.strip(), ""]
    if deep_commentary:
        lines += [deep_commentary.strip(), ""]
    if has_commentary:
        lines += ["---", "", "## All stories", ""]

    if not items:
        lines += ["Nothing crossed the threshold today.", ""]

    for category, group in group_by_category(items):
        lines += [f"## {category}", ""]
        for item in group:
            lines.append(f"### {md_link(item.title, item.url)}")
            lines.append(
                f"`{item.source.replace('`', chr(39))}` · "
                f"{item.published:%d %b %H:%M UTC} · score {item.score}"
            )
            if item.summary:
                lines += ["", escape_md_block(item.summary)]
            lines.append("")

    if errors or health_warnings:
        lines += ["---", "", "<details><summary>Feeds that did not respond</summary>", ""]
        # Error strings can carry feed-controlled text - a parser exception
        # quotes the document that failed to parse.
        for name, err in sorted(errors.items()):
            lines.append(f"- **{escape_md(name)}** - {escape_md(err)}")
        # A source that has been dead for days is a different problem from one
        # that timed out this morning, and it is the one worth acting on.
        for warning in health_warnings or []:
            lines.append(f"- ⚠️ {escape_md(warning)}")
        lines += ["", "</details>", ""]
    return "\n".join(lines)
