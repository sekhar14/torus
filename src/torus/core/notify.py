"""Which new alerts to show as desktop notifications, and how to word them.

Setting ``notify``: ``strong`` (default), ``moderate`` or ``off``. Only alerts from the
last 24 hours count, so turning notifications on later, or fixing a sender that kept
failing, doesn't replay old alerts. One notification per thesis, at most three per pass.
Sending is platform code (``torus.notify``); this module only decides what to send.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from torus.core.impact import STRENGTH_BANDS, strength
from torus.core.models import Alert
from torus.core.store.base import Backend

NOTIFY_SETTING = "notify"
NOTIFY_DEFAULT = "strong"
NOTIFY_FLOORS = {label: floor for floor, label in STRENGTH_BANDS if label != "weak"}
WINDOW = timedelta(hours=24)
MAX_PER_PASS = 3
TITLE_CHARS = 90
BODY_CHARS = 200


@dataclass(frozen=True)
class Notification:
    title: str
    body: str
    thesis_id: str | None = None


class NotifyError(Exception):
    """A notification couldn't be shown."""


class Sender(Protocol):
    def send(self, notification: Notification) -> None: ...


@dataclass
class NotifyResult:
    alerts: int = 0  # alerts covered by this pass
    sent: int = 0  # notifications shown
    errors: list[str] = field(default_factory=list)


def notify_floor(store: Backend) -> float | None:
    """The lowest alert score to notify, or None when notifications are off."""
    level = store.get_setting(NOTIFY_SETTING) or NOTIFY_DEFAULT
    return NOTIFY_FLOORS.get(level)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def pending(store: Backend, now: datetime) -> tuple[list[Notification], list[str]]:
    """The notifications to show now, and the ids of the alerts they cover."""
    floor = notify_floor(store)
    if floor is None:
        return [], []
    alerts = store.alerts_to_notify(floor, now - WINDOW)
    groups: dict[str, list[Alert]] = {}
    for alert in alerts:  # highest score first, so each group's first alert is its top
        groups.setdefault(alert.thesis_id, []).append(alert)

    notes: list[Notification] = []
    ordered = list(groups.items())
    shown = ordered if len(ordered) <= MAX_PER_PASS else ordered[: MAX_PER_PASS - 1]
    for thesis_id, group in shown:
        top = group[0]
        thesis = store.get_thesis(thesis_id)
        body = store.get_event(top.event_id).title
        if len(group) > 1:
            body += f" (and {len(group) - 1} more)"
        notes.append(
            Notification(
                title=_clip(f"Torus: {strength(top.score)} alert on {thesis.title}", TITLE_CHARS),
                body=_clip(body, BODY_CHARS),
                thesis_id=thesis_id,
            )
        )
    rest = ordered[len(shown) :]
    if rest:
        count = sum(len(g) for _, g in rest)
        notes.append(
            Notification(
                title=f"Torus: {len(rest)} more theses have new alerts",
                body=f"{count} alert(s). Run `torus alerts list`, or ask Claude to review them.",
            )
        )
    return notes, [a.id for a in alerts]


def notify_pending(store: Backend, sender: Sender, now: datetime) -> NotifyResult:
    """Show what ``pending`` returns and mark those alerts notified.

    Alerts are marked even when showing fails, so a broken sender doesn't pile up a
    backlog; the errors are returned instead of raised.
    """
    notes, alert_ids = pending(store, now)
    result = NotifyResult(alerts=len(alert_ids))
    for note in notes:
        try:
            sender.send(note)
            result.sent += 1
        except NotifyError as exc:
            result.errors.append(str(exc))
    store.mark_notified(alert_ids)
    return result
