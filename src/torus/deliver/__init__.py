"""Send the digest somewhere the user will see it: their webhook and/or email.

Nothing is sent until the user names a destination. ``send_scheduled`` runs at the
end of each ``torus watch run``: when a daily or weekly digest is due it writes the
digest files, sends them, and records when (``digest_last_sent``) and what failed
(``digest_last_error``). A failure never fails the watch run.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from torus.core.clock import from_ts, to_ts
from torus.core.digest import (
    PERIODS,
    WEEKDAYS,
    Digest,
    build_digest,
    digest_due,
    save_digest,
)
from torus.core.store.base import Backend
from torus.deliver import mail, webhook

DIGEST = "digest"  # off (default) | daily | weekly
DIGEST_HOUR = "digest_hour"  # 0-23, local time (default 8)
DIGEST_WEEKDAY = "digest_weekday"  # mon..sun (default mon)
DIGEST_SEND_EMPTY = "digest_send_empty"  # on | off (default)
WEBHOOK = "digest_webhook"
WEBHOOK_FORMAT = "digest_webhook_format"  # auto (default) | slack | discord | ntfy | json
EMAIL_TO = "digest_email_to"  # comma-separated
EMAIL_FROM = "digest_email_from"  # default: smtp_user
SMTP_HOST = "smtp_host"
SMTP_PORT = "smtp_port"  # default 587; 465 is implicit TLS
SMTP_USER = "smtp_user"
LAST_SENT = "digest_last_sent"
LAST_ERROR = "digest_last_error"

SECRET_SETTINGS = frozenset({WEBHOOK})  # masked when shown, left out of exports


@dataclass
class Delivery:
    """How to reach the outside world; tests replace both."""

    poster: webhook.Poster = webhook._post
    connect: mail.SMTPFactory = mail._connect
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))


@dataclass
class DeliveryResult:
    sent: list[str] = field(default_factory=list)  # destinations reached
    errors: list[str] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)
    skipped: str | None = None  # why nothing was sent


def destinations(store: Backend) -> list[str]:
    """Configured destinations, safe to show: ``webhook https://.../AbCd``, ``email a@b``."""
    out = []
    if url := store.get_setting(WEBHOOK):
        out.append(f"webhook {webhook.mask_url(url)}")
    if (to := store.get_setting(EMAIL_TO)) and store.get_setting(SMTP_HOST):
        out.append(f"email {to}")
    return out


def deliver(
    store: Backend, d: Digest, home: Path, full_at: Path | None, delivery: Delivery
) -> DeliveryResult:
    """Send ``d`` to every configured destination; one failing doesn't stop the other."""
    result = DeliveryResult()
    if url := store.get_setting(WEBHOOK):
        fmt = store.get_setting(WEBHOOK_FORMAT) or "auto"
        try:
            webhook.send(url, fmt, d, str(full_at) if full_at else None, delivery.poster)
            result.sent.append(f"webhook {webhook.mask_url(url)}")
        except webhook.WebhookError as exc:
            result.errors.append(f"webhook: {exc}")
    to = [a.strip() for a in (store.get_setting(EMAIL_TO) or "").split(",") if a.strip()]
    host = store.get_setting(SMTP_HOST)
    if to and host:
        user = store.get_setting(SMTP_USER)
        try:
            mail.send(
                d,
                host,
                int(store.get_setting(SMTP_PORT) or mail.DEFAULT_PORT),
                user,
                mail.read_password(home, delivery.env),
                store.get_setting(EMAIL_FROM) or user or to[0],
                to,
                delivery.connect,
            )
            result.sent.append(f"email {', '.join(to)}")
        except mail.EmailError as exc:
            result.errors.append(f"email: {exc}")
    if not result.sent and not result.errors:
        result.skipped = "no destination configured (digest_webhook or digest_email_to)"
    return result


def record(store: Backend, result: DeliveryResult, now: datetime) -> None:
    store.set_setting(LAST_SENT, to_ts(now))
    store.set_setting(LAST_ERROR, "; ".join(result.errors) or None)


def last_sent(store: Backend) -> datetime | None:
    value = store.get_setting(LAST_SENT)
    return from_ts(value) if value else None


def send_scheduled(
    store: Backend, home: Path, now: datetime, delivery: Delivery
) -> DeliveryResult | None:
    """Send the daily or weekly digest if one is due; None when it isn't."""
    schedule = store.get_setting(DIGEST) or "off"
    hour = int(store.get_setting(DIGEST_HOUR) or 8)
    weekday = store.get_setting(DIGEST_WEEKDAY) or WEEKDAYS[0]
    previous = last_sent(store)
    if not digest_due(schedule, hour, weekday, previous, now):
        return None
    since = previous if previous and previous < now else now - PERIODS[schedule]
    d = build_digest(store, since, now)
    markdown, page = save_digest(d, home / "digests")
    if d.empty and (store.get_setting(DIGEST_SEND_EMPTY) or "off") != "on":
        result = DeliveryResult(files=[markdown, page], skipped="nothing new")
    else:
        result = deliver(store, d, home, page, delivery)
        result.files = [markdown, page]
    record(store, result, now)
    return result
