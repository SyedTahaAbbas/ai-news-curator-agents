#!/usr/bin/env python3
"""The digest as an HTML email. Inline styles only - mail clients strip <style>."""

from __future__ import annotations

import html
from datetime import datetime, timezone

from ainews.models import Item
from ainews.render.markdown import group_by_category, safe_href
from ainews.render.md2html import markdown_to_html


def render_html(
    items: list[Item],
    hours: int,
    errors: dict[str, str],
    simple_commentary: str | None = None,
    deep_commentary: str | None = None,
    run_at: datetime | None = None,
    health_warnings: list[str] | None = None,
) -> str:
    today = run_at or datetime.now(timezone.utc)
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
        "<h1 style=\"margin:0 0 4px;font-size:22px;letter-spacing:-0.3px;\">AI News Curator</h1>",
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
        parts.append("<p style='color:#6b7280;'>Nothing crossed the threshold today.</p>")

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
            f"{len(errors)} feed(s) did not respond: " + esc(", ".join(sorted(errors))) + "</p>"
        )

    for warning in health_warnings or []:
        parts.append(
            f"<p style='color:#b45309;font-size:11px;margin:4px 0 0;'>⚠️ {esc(warning)}</p>"
        )

    parts += [
        "<p style='color:#9ca3af;font-size:11px;margin-top:20px;'>"
        "Generated automatically by AI News Curator Agents.</p>",
        "</div></div></body></html>",
    ]
    return "".join(parts)
