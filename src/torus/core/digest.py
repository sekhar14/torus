"""What happened in the research since a point in time. No LLM.

``build_digest`` gathers, in order of urgency: new alerts by thesis, thesis status and
confidence changes, theses coming due, research health (the audit), scorecard moves,
and activity counts. ``render_markdown`` and ``render_html`` turn it into a document,
written by ``torus digest`` and returned by the MCP ``digest`` tool.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, computed_field

from torus.core.audit import Severity, audit
from torus.core.changes import ChangeKind, what_changed
from torus.core.impact import strength
from torus.core.mdhtml import markdown_to_html
from torus.core.memo import DISCLAIMER
from torus.core.models import AlertStatus, ThesisStatus
from torus.core.scorecard import benchmark_for, stance_move, tracked
from torus.core.store.base import Backend

DUE_WITHIN = timedelta(days=30)
_SINCE = re.compile(r"^(\d+)\s*([hdw])$")
_UNITS = {"h": "hours", "d": "days", "w": "weeks"}
CONFIDENCE_STEP = 0.1  # smaller confidence changes aren't worth a line
TOP_FINDINGS = 3
TOP_MOVES = 3
OPEN_STATUSES = (ThesisStatus.ACTIVE, ThesisStatus.AT_RISK)
ACTIVITY_LABELS = {
    ChangeKind.CLAIM_RECORDED: "claim(s) recorded",
    ChangeKind.CLAIM_SUPERSEDED: "claim(s) corrected",
    ChangeKind.CLAIM_RETRACTED: "claim(s) retracted",
    ChangeKind.THESIS_CREATED: "thesis/theses created",
    ChangeKind.THESIS_CHANGED: "thesis update(s)",
    ChangeKind.EVENT_ADDED: "event(s)",
    ChangeKind.ENTITY_ADDED: "company/companies added",
}


class DigestAlert(BaseModel):
    alert_id: str
    event_title: str
    event_at: datetime
    uri: str | None
    score: float
    strength: str
    status: str  # open, or the verdict it was judged with


class DigestThesisAlerts(BaseModel):
    thesis_id: str
    thesis_title: str
    alerts: list[DigestAlert]


class DigestThesisChange(BaseModel):
    thesis_id: str
    title: str
    status_from: ThesisStatus
    status_to: ThesisStatus
    confidence_from: float
    confidence_to: float
    reason: str | None


class DigestDue(BaseModel):
    thesis_id: str
    title: str
    horizon: str | None
    due: datetime
    days: int  # negative: past due


class DigestFinding(BaseModel):
    severity: Severity
    title: str


class DigestMove(BaseModel):
    thesis_id: str
    title: str
    symbol: str
    benchmark: str
    move: float  # return against the benchmark in the thesis's direction


class Digest(BaseModel):
    since: datetime
    until: datetime
    new_alerts: list[DigestThesisAlerts]
    older_open_alerts: int
    thesis_changes: list[DigestThesisChange]
    coming_due: list[DigestDue]
    audit_counts: dict[str, int]
    top_findings: list[DigestFinding]
    moves: list[DigestMove]
    activity: dict[str, int]
    markdown: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def empty(self) -> bool:
        """Nothing that needs attention: health and activity alone don't count."""
        return not (self.new_alerts or self.thesis_changes or self.coming_due)


def _new_alerts(store: Backend, since: datetime) -> tuple[list[DigestThesisAlerts], int]:
    groups: dict[str, DigestThesisAlerts] = {}
    older_open = 0
    for alert in store.alerts(statuses=list(AlertStatus)):  # highest score first
        if alert.created_at < since:
            older_open += alert.status is AlertStatus.OPEN
            continue
        event = store.get_event(alert.event_id)
        if alert.thesis_id not in groups:
            thesis = store.get_thesis(alert.thesis_id)
            groups[alert.thesis_id] = DigestThesisAlerts(
                thesis_id=thesis.id, thesis_title=thesis.title, alerts=[]
            )
        groups[alert.thesis_id].alerts.append(
            DigestAlert(
                alert_id=alert.id,
                event_title=event.title,
                event_at=event.occurred_at,
                uri=event.uri,
                score=round(alert.score, 2),
                strength=strength(alert.score),
                status=alert.verdict.value if alert.verdict else alert.status.value,
            )
        )
    return list(groups.values()), older_open


def _thesis_changes(store: Backend, since: datetime, until: datetime) -> list[DigestThesisChange]:
    """One line per thesis: its state before ``since`` against its state at ``until``
    (inclusive, so a change made just now counts)."""
    changed = {e.thesis_id for e in store.thesis_changes(since=since) if e.at <= until}
    out = []
    for thesis_id in sorted(changed):
        history = store.thesis_history(thesis_id)
        before = [e for e in history if e.at < since]
        during = [e for e in history if since <= e.at <= until]
        if not before or not during:  # created in the window: counted under activity
            continue
        first, last = before[-1], during[-1]
        moved = abs(last.confidence - first.confidence) >= CONFIDENCE_STEP - 1e-9
        if last.status is first.status and not moved:
            continue
        out.append(
            DigestThesisChange(
                thesis_id=thesis_id,
                title=store.get_thesis(thesis_id).title,
                status_from=first.status,
                status_to=last.status,
                confidence_from=first.confidence,
                confidence_to=last.confidence,
                reason=last.reason,
            )
        )
    severity = {ThesisStatus.BROKEN: 0, ThesisStatus.AT_RISK: 1}
    out.sort(key=lambda c: (severity.get(c.status_to, 2), c.title))
    return out


def _coming_due(store: Backend, now: datetime) -> list[DigestDue]:
    due = [
        DigestDue(
            thesis_id=t.id,
            title=t.title,
            horizon=t.horizon,
            due=t.horizon_date,
            days=(t.horizon_date - now).days,
        )
        for t in store.theses(statuses=OPEN_STATUSES)
        if t.horizon_date is not None and t.horizon_date <= now + DUE_WITHIN
    ]
    return sorted(due, key=lambda d: d.due)


def _moves(store: Backend, since: datetime, until: datetime) -> list[DigestMove]:
    moves = []
    for thesis in tracked(store):
        move = stance_move(store, thesis, since.date(), until.date())
        if move is not None:
            moves.append(
                DigestMove(
                    thesis_id=thesis.id,
                    title=thesis.title,
                    symbol=thesis.instrument or "",
                    benchmark=benchmark_for(store, thesis),
                    move=move,
                )
            )
    moves.sort(key=lambda m: -abs(m.move))
    return moves[:TOP_MOVES]


def build_digest(store: Backend, since: datetime, now: datetime) -> Digest:
    new_alerts, older_open = _new_alerts(store, since)
    report = audit(store, now)
    changes = what_changed(store, since).changes  # up to the present, including now
    activity = Counter(c.kind for c in changes if c.at <= now)
    digest = Digest(
        since=since,
        until=now,
        new_alerts=new_alerts,
        older_open_alerts=older_open,
        thesis_changes=_thesis_changes(store, since, now),
        coming_due=_coming_due(store, now),
        audit_counts=report.by_severity,
        top_findings=[
            DigestFinding(severity=f.severity, title=f.title)
            for f in report.findings
            if f.severity is Severity.HIGH
        ][:TOP_FINDINGS],
        moves=_moves(store, since, now),
        activity={kind.value: activity[kind] for kind in ChangeKind if activity[kind]},
    )
    return digest.model_copy(update={"markdown": render_markdown(digest)})


def _day(value: datetime) -> str:
    return f"{value:%Y-%m-%d}"


def _status(value: ThesisStatus) -> str:
    return value.value.replace("_", " ")


def render_markdown(d: Digest) -> str:
    out = [f"# Torus digest, {_day(d.since)} to {_day(d.until)}", ""]
    if d.empty:
        out += [f"Nothing needs your attention since {d.since:%Y-%m-%d %H:%M} UTC.", ""]

    if d.new_alerts:
        total = sum(len(g.alerts) for g in d.new_alerts)
        out += ["## New alerts", "", f"{total} alert(s) on {len(d.new_alerts)} thesis/theses."]
        for group in d.new_alerts:
            out += ["", f"**{group.thesis_title}**", ""]
            for a in group.alerts:
                title = f"[{a.event_title}]({a.uri})" if a.uri else a.event_title
                line = f"{a.strength} ({a.score:.2f}), {_day(a.event_at)}: {title}"
                out.append(f"- {line} *{a.status}*")
        out.append("")
    if d.older_open_alerts:
        out += [f"{d.older_open_alerts} older alert(s) are still open.", ""]

    if d.thesis_changes:
        out += ["## Thesis changes", ""]
        for c in d.thesis_changes:
            parts = []
            if c.status_to is not c.status_from:
                parts.append(f"{_status(c.status_from)} to {_status(c.status_to)}")
            if abs(c.confidence_to - c.confidence_from) >= CONFIDENCE_STEP - 1e-9:
                parts.append(f"confidence {c.confidence_from:.0%} to {c.confidence_to:.0%}")
            reason = f" ({c.reason})" if c.reason else ""
            out.append(f"- **{c.title}**: {', '.join(parts)}{reason}")
        out.append("")

    if d.coming_due:
        out += ["## Coming due", ""]
        for due in d.coming_due:
            when = f"in {due.days} days" if due.days >= 0 else f"{-due.days} days ago"
            horizon = f"horizon {due.horizon}, " if due.horizon else ""
            out.append(f"- **{due.title}**: {horizon}due {_day(due.due)} ({when})")
        out.append("")

    counts = d.audit_counts
    if counts:
        out += [
            "## Research health",
            "",
            f"{counts.get('high', 0)} high, {counts.get('medium', 0)} medium and"
            f" {counts.get('low', 0)} low audit findings. Run `torus audit` for the list.",
        ]
        if d.top_findings:
            out += ["", *(f"- {f.title}" for f in d.top_findings)]
        out.append("")

    if d.moves:
        out += ["## Scorecard", ""]
        for m in d.moves:
            out.append(
                f"- **{m.title}** ({m.symbol} against {m.benchmark}): {m.move:+.1%}"
                " in the thesis's direction"
            )
        out.append("")

    if d.activity:
        items = [f"{n} {ACTIVITY_LABELS[ChangeKind(k)]}" for k, n in d.activity.items()]
        out += ["## Activity", "", ", ".join(items) + ".", ""]

    out += ["---", f"*{DISCLAIMER}*", ""]
    return "\n".join(out)


def render_html(d: Digest) -> str:
    return markdown_to_html(d.markdown or render_markdown(d), f"Torus digest {_day(d.until)}")


def parse_since(text: str, now: datetime) -> datetime:
    """``12h``, ``1d``, ``2w`` before ``now``, or a date (``2026-10-01``, midnight UTC)."""
    text = text.strip().lower()
    match = _SINCE.match(text)
    if match:
        return now - timedelta(**{_UNITS[match.group(2)]: int(match.group(1))})
    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        raise ValueError(f"can't read {text!r}: use 12h, 1d, 2w or YYYY-MM-DD") from None


def save_digest(d: Digest, directory: Path) -> tuple[Path, Path]:
    """Write the digest as Markdown and HTML; returns both paths."""
    directory.mkdir(parents=True, exist_ok=True)
    stem = directory / f"digest-{_day(d.until)}"
    markdown, page = stem.with_suffix(".md"), stem.with_suffix(".html")
    markdown.write_text(d.markdown or render_markdown(d), "utf-8", newline="\n")
    page.write_text(render_html(d), "utf-8", newline="\n")
    return markdown, page
