#!/usr/bin/env python3
"""
SMTP delivery for the daily digest.

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
"""

from __future__ import annotations

import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

DEFAULT_RECIPIENT = "Taha.mmtlmu@gmail.com"


def _recipients() -> list[str]:
    raw = os.getenv("MAIL_TO", DEFAULT_RECIPIENT)
    return [addr.strip() for addr in raw.split(",") if addr.strip()]


def send_digest_email(subject: str, html_body: str, text_body: str) -> bool:
    """Send the digest. Returns True on success, False (with a reason) otherwise."""
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
        print(
            f"[emailer] Skipping send - missing env var(s): {', '.join(missing)}",
            file=sys.stderr,
        )
        return False
    if not to_addrs:
        print("[emailer] Skipping send - MAIL_TO is empty.", file=sys.stderr)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("AI News Update", sender))
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


if __name__ == "__main__":
    # Quick connectivity test: python emailer.py
    ok = send_digest_email(
        subject="AI News Update - test email",
        html_body="<p>If you can read this, SMTP is configured correctly.</p>",
        text_body="If you can read this, SMTP is configured correctly.",
    )
    raise SystemExit(0 if ok else 1)
