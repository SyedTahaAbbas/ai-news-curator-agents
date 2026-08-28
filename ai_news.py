#!/usr/bin/env python3
"""
AI News Curator Agents
=======================
A 3-stage pipeline that collects AI news daily, ranks it, writes commentary,
and emails a digest:

    agents/gatherer.py  - fetch raw items from every feed in sources.yaml
    agents/analyst.py   - filter, score, dedupe, check against .seen.json
    agents/writer.py    - optional LLM commentary at two levels (simple + deep)
    ai_news.py           - render + deliver (this file), and the orchestrator

No Twitter/X scraping: X requires login for search and actively blocks
automated clients, so anonymous scraping is both unreliable and against their
terms. The feeds in sources.yaml cover the same ground (labs, press, research,
HN/Reddit) without auth, cost, or breakage.

Usage
-----
Local/Docker (runs all three stages in-process, in memory - same as always):

    python ai_news.py                     # collect last 24h, write digest, email it
    python ai_news.py --hours 48          # widen the window
    python ai_news.py --no-email          # write files only
    python ai_news.py --dry-run           # print to stdout, write nothing
    python ai_news.py --check-feeds       # health-check every source and exit
    python ai_news.py --max-items 40      # cap digest length
    python ai_news.py --no-commentary     # skip the LLM voice layer

Each stage independently (what the GitHub Actions workflow runs, one stage
per step, passing a JSON file between them):

    python -m agents.gatherer --out raw.json
    python -m agents.analyst  --in raw.json --out ranked.json
    python -m agents.writer   --in ranked.json --out-simple simple.md --out-deep deep.md
    python ai_news.py deliver --in ranked.json --simple simple.md --deep deep.md

Environment (see .env.example)
------------------------------
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, MAIL_FROM, MAIL_TO
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:  # optional: load a local .env when present
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional
    pass

from agents import analyst, gatherer, writer
from emailer import send_digest_email
from models import Item, load_config

ROOT = Path(__file__).resolve().parent
DIGEST_DIR = ROOT / "see news"

# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

CATEGORY_ORDER = ["Labs & Releases", "Industry & Press", "Research", "Community"]

_SAFE_URL_SCHEMES = ("http://", "https://")


def safe_href(url: str) -> str:
    """Item URLs come from external feeds we don't control. Only ever emit
    http(s) links into the HTML email - anything else (javascript:, data:,
    etc.) becomes a dead link instead of a clickable one."""
    return url if url.lower().startswith(_SAFE_URL_SCHEMES) else "#"


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
) -> str:
    today = datetime.now(timezone.utc)
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
        # The model writes these links from feed-sourced URLs it was given -
        # external, untrusted input - so only ever render http(s) as clickable.
        s = _MD_LINK.sub(
            lambda m: f'<a href="{m.group(2) if m.group(2).lower().startswith(("http://", "https://")) else "#"}" '
            f'style="color:#4f46e5;">{m.group(1)}</a>',
            s,
        )
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
    simple_commentary: str | None = None,
    deep_commentary: str | None = None,
) -> str:
    today = datetime.now(timezone.utc)
    esc = html.escape

    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>AI News Curator</title></head>",
        "<body style=\"margin:0;padding:0;background:#f4f5f7;\">",
        "<div style=\"max-width:680px;margin:0 auto;padding:24px 16px;"
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
        "color:#1a1a1a;line-height:1.5;\">",
        "<div style=\"background:#ffffff;border-radius:12px;padding:28px 26px;"
        "box-shadow:0 1px 3px rgba(0,0,0,0.08);\">",
        f"<h1 style=\"margin:0 0 4px;font-size:22px;letter-spacing:-0.3px;\">AI News Curator</h1>",
        f"<p style=\"margin:0 0 24px;color:#6b7280;font-size:13px;\">"
        f"{today:%A, %d %B %Y} &middot; {len(items)} stories from the last {hours}h</p>",
    ]

    has_commentary = bool(simple_commentary or deep_commentary)
    if has_commentary:
        commentary_html = markdown_to_html((simple_commentary or "").strip())
        if deep_commentary:
            commentary_html += markdown_to_html(deep_commentary.strip())
        parts.append(
            "<div style=\"background:#fafafa;border-left:3px solid #6366f1;"
            "border-radius:0 6px 6px 0;padding:4px 18px 10px;margin:0 0 26px;\">"
            + commentary_html
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
                f"<a href=\"{esc(safe_href(item.url))}\" style=\"color:#111827;font-weight:600;"
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
        "Generated automatically by AI News Curator Agents.</p>",
        "</div></div></body></html>",
    ]
    return "".join(parts)


# ---------------------------------------------------------------------------
# Delivery (render, write files, email, mark seen)
# ---------------------------------------------------------------------------


def _write_digest_files(items: list[Item], markdown: str) -> None:
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


def _send_email(
    items: list[Item],
    hours: int,
    errors: dict[str, str],
    markdown: str,
    simple_commentary: str | None,
    deep_commentary: str | None,
) -> None:
    subject = f"AI News Curator - {datetime.now(timezone.utc):%d %b %Y} ({len(items)} stories)"
    sent = send_digest_email(
        subject=subject,
        html_body=render_html(items, hours, errors, simple_commentary, deep_commentary),
        text_body=markdown,
    )
    if not sent:
        print("Email not sent (see message above).", file=sys.stderr)


def _mark_seen(items: list[Item]) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    seen = analyst.load_seen()
    seen.update({i.uid: now_iso for i in items})
    analyst.save_seen(seen)


def _read_optional(path: str) -> str | None:
    p = Path(path)
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8").strip()
    return text or None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _run(args: argparse.Namespace) -> int:
    """Full pipeline, in-process, no intermediate files - the local/Docker path."""
    cfg = load_config()

    if args.check_feeds:
        return gatherer.check_feeds(cfg)

    raw, errors = gatherer.collect(cfg)
    print(f"Fetched {len(raw)} raw items; {len(errors)} feed(s) unreachable.")

    items = analyst.build_digest(
        cfg, raw, args.hours, args.max_items, skip_seen=not args.include_seen
    )
    print(f"{len(items)} items in today's digest.")

    if args.no_commentary:
        simple_commentary, deep_commentary = None, None
    else:
        simple_commentary = writer.write_simple_summary(items)
        deep_commentary = writer.write_deep_dive(items)

    markdown = render_markdown(items, args.hours, errors, simple_commentary, deep_commentary)

    if args.dry_run:
        print("\n" + markdown)
        return 0

    _write_digest_files(items, markdown)

    if not args.no_email:
        _send_email(items, args.hours, errors, markdown, simple_commentary, deep_commentary)

    _mark_seen(items)
    return 0


def _cmd_deliver(args: argparse.Namespace) -> int:
    """Render the analyst's + writer's output into a digest, write it, email it.

    This is the last of the four CI steps - what `_run()` does after the
    write stage, but reading everything from files instead of holding it in
    memory, so it can be its own GitHub Actions step.
    """
    ranked = json.loads(Path(args.input).read_text(encoding="utf-8"))
    items = [Item.from_dict(d) for d in ranked["items"]]
    errors = ranked.get("errors", {})

    simple_commentary = _read_optional(args.simple)
    deep_commentary = _read_optional(args.deep)

    markdown = render_markdown(items, args.hours, errors, simple_commentary, deep_commentary)

    if args.dry_run:
        print("\n" + markdown)
        return 0

    _write_digest_files(items, markdown)

    if not args.no_email:
        _send_email(items, args.hours, errors, markdown, simple_commentary, deep_commentary)

    _mark_seen(items)
    return 0


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

    sub = ap.add_subparsers(dest="command")
    p_deliver = sub.add_parser(
        "deliver",
        help="render the analyst+writer output into a digest, write it, and email it "
        "(the last of the four CI steps; gather/analyze/write are `python -m agents.<stage>`)",
    )
    p_deliver.add_argument("--in", dest="input", default="ranked.json", help="ranked items from the analyst")
    p_deliver.add_argument("--simple", default="simple.md", help="top-level summary from the writer")
    p_deliver.add_argument("--deep", default="deep.md", help="deep dive from the writer")
    p_deliver.add_argument("--hours", type=int, default=24, help="lookback window, for display only")
    p_deliver.add_argument("--no-email", action="store_true", help="write files but do not send")
    p_deliver.add_argument("--dry-run", action="store_true", help="print only, write nothing")

    args = ap.parse_args(argv)

    if args.command == "deliver":
        return _cmd_deliver(args)
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
