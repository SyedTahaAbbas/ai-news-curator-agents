#!/usr/bin/env python3
"""
Minimal Markdown -> HTML, for the model's commentary only.

Deliberately small: headings, bold, italic, inline code, links, bullet and
numbered lists, paragraphs. Everything is escaped first, so a model that emits
raw HTML - or that was talked into emitting it by a hostile feed title -
cannot inject it into the email.
"""

from __future__ import annotations

import html
import re

_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_MD_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_MD_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_MD_CODE = re.compile(r"`([^`]+)`")


def _inline(s: str) -> str:
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


def markdown_to_html(text: str) -> str:
    out: list[str] = []
    in_list = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

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
                f"color:#111827;'>{_inline(heading.group(2))}</h{level}>"
            )
            continue

        bullet = re.match(r"^\s*[-*+]\s+(.*)$", line) or re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if bullet:
            if not in_list:
                out.append("<ul style='margin:8px 0;padding-left:20px;'>")
                in_list = True
            out.append(
                f"<li style='margin:4px 0;font-size:14px;color:#374151;'>"
                f"{_inline(bullet.group(1))}</li>"
            )
            continue

        close_list()
        out.append(
            f"<p style='margin:10px 0;font-size:14px;line-height:1.6;color:#374151;'>"
            f"{_inline(line)}</p>"
        )

    close_list()
    return "".join(out)
