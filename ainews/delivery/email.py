#!/usr/bin/env python3
"""
The email channel.

Configured entirely through environment variables so nothing secret ever lands
in the repo. Locally these come from a .env file; in GitHub Actions they come
from repository secrets.

    SMTP_HOST      default smtp.gmail.com
    SMTP_PORT      default 587  (587 = STARTTLS, 465 = implicit SSL)
    SMTP_USER      the mailbox you send from
    SMTP_PASSWORD  Gmail app password (NOT your account password)
    MAIL_FROM      defaults to SMTP_USER
    MAIL_TO        comma-separated recipients

Gmail note: with 2FA on, generate an app password at
https://myaccount.google.com/apppasswords and use that as SMTP_PASSWORD.

The distinction this module reports back matters more than it looks. "No
mailbox is configured" is a skip - you never asked for email, so the run is
still a success. "We tried and it failed" is a failure that blocks the commit,
because a digest went missing and those stories must stay unsent so tomorrow
can try again.
"""

from __future__ import annotations

import os
import smtplib
import ssl
import sys
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from ainews.bus import EventBus
from ainews.config import Config
from ainews.events import DigestEmailed, DigestRendered
from ainews.render import render_html
from ainews.state import FeedHealthStore

DEFAULT_RECIPIENT = "Taha.mmtlmu@gmail.com"


class EmailNotConfigured(Exception):
    """No mailbox set up. A skip, not a failure."""


def _recipients() -> list[str]:
    raw = os.getenv("MAIL_TO", DEFAULT_RECIPIENT)
    return [addr.strip() for addr in raw.split(",") if addr.strip()]


def send_digest_email(subject: str, html_body: str, text_body: str) -> bool:
    """Send the digest.

    Returns True on success, False on a genuine send failure. Raises
    EmailNotConfigured when there is no mailbox to send from - the caller
    treats that as "email was never on the table", not as a lost digest.
    """
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    sender = os.getenv("MAIL_FROM", user or "")
    to_addrs = _recipients()

    missing = [
        name
        for name, value in (("SMTP_USER", user), ("SMTP_PASSWORD", password))
        if not value
    ]
    if missing:
        raise EmailNotConfigured(f"missing env var(s): {', '.join(missing)}")
    if not to_addrs:
        raise EmailNotConfigured("MAIL_TO is empty")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("AI News Curator", sender))
    msg["To"] = ", ".join(to_addrs)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="ai-news-update.local")
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
                server.login(user, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(user, password)
                server.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        print(
            "[emailer] Authentication failed. For Gmail you must use an app "
            "password (https://myaccount.google.com/apppasswords), not your "
            "normal account password.",
            file=sys.stderr,
        )
        return False
    except Exception as exc:
        print(f"[emailer] Send failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False

    print(f"[emailer] Digest sent to {', '.join(to_addrs)}")
    return True


def register_email(
    bus: EventBus, cfg: Config, health: FeedHealthStore | None = None
) -> None:
    def send_email(event: DigestRendered, bus: EventBus) -> None:
        if event.config.dry_run or not event.config.email:
            return

        warnings = []
        if health is not None:
            warnings = [
                f"{name} has been unreachable for {days} days"
                for name, days in health.dead_for_days(
                    cfg.health.warn_after_dead_days, event.config.run_at
                )
            ]

        run_at: datetime = event.config.run_at
        subject = f"AI News Curator - {run_at:%d %b %Y} ({len(event.items)} stories)"
        try:
            sent = send_digest_email(
                subject=subject,
                html_body=render_html(
                    event.items,
                    event.config.hours,
                    event.errors,
                    event.simple,
                    event.deep,
                    run_at=run_at,
                    health_warnings=warnings,
                ),
                text_body=event.markdown,
            )
        except EmailNotConfigured as exc:
            print(f"[emailer] Skipping send - {exc}.", file=sys.stderr)
            bus.publish(DigestEmailed(sent=False, skipped=True, reason=str(exc)))
            return

        bus.publish(
            DigestEmailed(sent=sent, skipped=False, reason="" if sent else "send failed")
        )

    bus.subscribe(DigestRendered, send_email)
