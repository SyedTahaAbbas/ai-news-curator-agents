#!/usr/bin/env python3
"""
The voice layer.

Turns the ranked list of stories into written commentary, using the "Voice"
section of PREFERENCES.md verbatim as the model's system prompt. Edit that
file and the next digest changes — no code changes needed.

Entirely optional. With no API key set, the pipeline falls back to the plain
link digest and nothing breaks.

Provider is auto-detected from whichever key is present:

    ANTHROPIC_API_KEY  ->  Claude   (default model: claude-sonnet-4-5)
    OPENAI_API_KEY     ->  OpenAI   (default model: gpt-4.1-mini)

Override with LLM_PROVIDER=anthropic|openai|none and LLM_MODEL=<model-id>.

Both providers are called over plain HTTPS via urllib, so there is no extra
dependency to install and nothing to keep in version lockstep.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
PREFERENCES_FILE = ROOT / "PREFERENCES.md"

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-5",
    "openai": "gpt-4.1-mini",
}

REQUEST_TIMEOUT = 120
MAX_OUTPUT_TOKENS = 2000

FALLBACK_VOICE = """\
Write a short daily AI news digest for a technical reader.
Be specific: names, numbers, versions. Explain what changed and what it
pressures. No hype words. Say so plainly if nothing important happened.
Under 700 words."""


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------


def load_voice_prompt(path: Path = PREFERENCES_FILE) -> str:
    """Extract everything under the '## 2. Voice' heading in PREFERENCES.md.

    The section is passed to the model as-is, which is the whole point: the
    user edits prose, not code. If the file or heading is missing we fall back
    to a terse built-in prompt rather than failing the run.
    """
    if not path.exists():
        print(f"[summarizer] {path.name} not found; using fallback voice.", file=sys.stderr)
        return FALLBACK_VOICE

    text = path.read_text(encoding="utf-8")
    match = re.search(r"^##\s*2\.\s*Voice\s*$", text, re.MULTILINE | re.IGNORECASE)
    if not match:
        print(
            "[summarizer] No '## 2. Voice' heading in PREFERENCES.md; using fallback.",
            file=sys.stderr,
        )
        return FALLBACK_VOICE

    section = text[match.end():]
    # Stop at the next top-level (##) heading, so "Changing your mind" is excluded.
    nxt = re.search(r"^##\s+(?!#)", section, re.MULTILINE)
    if nxt:
        section = section[: nxt.start()]

    # Drop the "everything below is sent to the model" meta-note.
    section = re.sub(r"^>.*$", "", section, flags=re.MULTILINE)
    section = section.strip()
    return section or FALLBACK_VOICE


# ---------------------------------------------------------------------------
# Provider dispatch
# ---------------------------------------------------------------------------


def detect_provider() -> tuple[str | None, str | None]:
    """Returns (provider, api_key). (None, None) means 'skip the voice layer'."""
    forced = os.getenv("LLM_PROVIDER", "").strip().lower()
    if forced == "none":
        return None, None
    if forced == "anthropic":
        return ("anthropic", os.getenv("ANTHROPIC_API_KEY"))
    if forced == "openai":
        return ("openai", os.getenv("OPENAI_API_KEY"))

    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic", os.getenv("ANTHROPIC_API_KEY")
    if os.getenv("OPENAI_API_KEY"):
        return "openai", os.getenv("OPENAI_API_KEY")
    return None, None


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def call_anthropic(api_key: str, model: str, system: str, user: str) -> str:
    data = _post_json(
        "https://api.anthropic.com/v1/messages",
        {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        {
            "model": model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
    )
    return "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    ).strip()


def call_openai(api_key: str, model: str, system: str, user: str) -> str:
    data = _post_json(
        "https://api.openai.com/v1/chat/completions",
        {"Authorization": f"Bearer {api_key}"},
        {
            "model": model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
    )
    return data["choices"][0]["message"]["content"].strip()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def format_stories_for_model(items: list[Any]) -> str:
    """Serialise the ranked items into the user turn."""
    lines = [
        "Here are today's collected AI stories, already filtered and ranked "
        "(higher score = more likely to matter). Write the digest.",
        "",
    ]
    for n, item in enumerate(items, 1):
        lines.append(f"{n}. {item.title}")
        lines.append(f"   source: {item.source} ({item.category})")
        lines.append(f"   published: {item.published:%Y-%m-%d %H:%M UTC}  score: {item.score}")
        lines.append(f"   url: {item.url}")
        if item.summary:
            lines.append(f"   summary: {item.summary}")
        lines.append("")
    lines.append(
        "Write the digest in Markdown. Link every story you mention to its url. "
        "Do not invent details that are not in the material above — if the "
        "source is thin, say the source is thin."
    )
    return "\n".join(lines)


def write_commentary(items: list[Any]) -> str | None:
    """Generate the digest commentary. Returns None if unavailable.

    None is a normal outcome, not an error: it means no key is configured, or
    the call failed. Either way the caller falls back to the plain digest.
    """
    if not items:
        return None

    provider, api_key = detect_provider()
    if not provider:
        print(
            "[summarizer] No ANTHROPIC_API_KEY or OPENAI_API_KEY set - "
            "sending the plain link digest.",
            file=sys.stderr,
        )
        return None
    if not api_key:
        print(
            f"[summarizer] LLM_PROVIDER={provider} but its API key is unset - "
            "sending the plain link digest.",
            file=sys.stderr,
        )
        return None

    model = os.getenv("LLM_MODEL") or DEFAULT_MODELS[provider]
    system = load_voice_prompt()
    user = format_stories_for_model(items)

    try:
        if provider == "anthropic":
            text = call_anthropic(api_key, model, system, user)
        else:
            text = call_openai(api_key, model, system, user)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        print(f"[summarizer] {provider} HTTP {exc.code}: {body}", file=sys.stderr)
        return None
    except Exception as exc:
        print(f"[summarizer] {provider} call failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None

    if not text:
        print("[summarizer] Model returned empty text.", file=sys.stderr)
        return None

    print(f"[summarizer] Commentary written by {provider}/{model} ({len(text)} chars).")
    return text


if __name__ == "__main__":
    # Inspect what the model is actually being told: python summarizer.py
    print("=" * 70)
    print("VOICE PROMPT (from PREFERENCES.md section 2)")
    print("=" * 70)
    print(load_voice_prompt())
    print()
    provider, key = detect_provider()
    if provider and key:
        model = os.getenv("LLM_MODEL") or DEFAULT_MODELS[provider]
        print(f"Provider: {provider} / {model} (key present)")
    else:
        print("Provider: none configured - digests will be plain links.")
