#!/usr/bin/env python3
"""
The one place that knows how to retry.

Every network call in this system is made once a day, unattended, with no one
watching. A single 429 from the model provider used to cost the entire voice
layer for that day; a rate-limited Reddit fetch cost that source until
tomorrow. Both are transient, and both are now retried with exponential
backoff and jitter.

Only transient conditions are retried - timeouts, connection errors, 408, 429,
5xx. A 401 or a 404 is a fact about the world, and retrying it just wastes the
run's time budget.
"""

from __future__ import annotations

import json
import random
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, TypeVar

T = TypeVar("T")

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
MAX_BACKOFF = 30.0


class RetriesExhausted(Exception):
    """All attempts failed. Carries the last exception as __cause__."""


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in RETRYABLE_STATUS
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError, OSError)):
        return True
    return False


def _retry_after(exc: BaseException) -> float | None:
    """Honour a server's Retry-After when it sends one - it knows better than
    our backoff curve does."""
    if not isinstance(exc, urllib.error.HTTPError):
        return None
    raw = exc.headers.get("Retry-After") if exc.headers else None
    if not raw:
        return None
    try:
        return max(0.0, min(float(raw), MAX_BACKOFF))
    except (TypeError, ValueError):
        return None  # the HTTP-date form; our own backoff is fine


def with_retries(
    operation: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    label: str = "request",
    retry_on: Callable[[BaseException], bool] = is_transient,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run `operation`, retrying transient failures with backoff + jitter.

    Jitter matters more than it looks: 22 feeds retrying on the same schedule
    would otherwise resynchronise into bursts against the same hosts.
    """
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            last = exc
            if attempt == attempts or not retry_on(exc):
                break
            delay = _retry_after(exc)
            if delay is None:
                delay = min(base_delay * (2 ** (attempt - 1)), MAX_BACKOFF)
                delay *= 0.5 + random.random()  # 50-150% jitter
            print(
                f"[http] {label}: {type(exc).__name__} on attempt {attempt}/{attempts}, "
                f"retrying in {delay:.1f}s",
                file=sys.stderr,
            )
            sleep(delay)

    assert last is not None
    raise last


def post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    *,
    timeout: float = 120.0,
    attempts: int = 3,
    label: str = "request",
) -> dict[str, Any]:
    """POST JSON, get JSON back, with retries on transient failures."""

    def once() -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    return with_retries(once, attempts=attempts, label=label)
