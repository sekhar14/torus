"""A thesis as a shareable memo, every claim traced to its sources. No LLM.

``build_memo`` gathers the thesis, its history, the claims it rests on (with
evidence quotes and the claims they derive from), its triggers, the alerts it
received and how they were judged, ruled-out ideas, and the subject's links.
``render_markdown`` and ``render_html`` turn that into a document; the HTML is a
single self-contained page. The ``/torus:memo`` skill can add a summary on top.
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from torus.core.models import (
    AlertStatus,
    Claim,
    RelationType,
    Support,
    ThesisClaimRole,
)
from torus.core.store.base import Backend

DISCLAIMER = "Torus is a research memory, not financial advice."
ROLE_TITLES = {
    ThesisClaimRole.ASSUMPTION: "What it rests on",
    ThesisClaimRole.SUPPORTING: "Supporting evidence",
    ThesisClaimRole.RISK: "Risks",
}


class MemoSource(BaseModel):
    title: str
    uri: str
    publisher: str | None
    published_at: datetime | None
    quote: str | None
    contradicts: bool


class MemoClaim(BaseModel):
    id: str
    text: str
    claim_type: str
    confidence: float
    valid_from: datetime | None
    valid_to: datetime | None
    recorded_at: datetime
    sources: list[MemoSource]
    derived_from: list[str]  # texts of the claims an inference builds on


class MemoHistory(BaseModel):
    at: datetime
    status: str
    confidence: float
    reason: str | None


class MemoAlert(BaseModel):
    occurred_at: datetime
    event: str
    uri: str | None
    status: str
    verdict: str | None
    rationale: str | None
    why: str | None


class Memo(BaseModel):
    thesis_id: str
    title: str
    statement: str
    stance: str
    status: str
    confidence: float
    horizon: str | None
    horizon_date: datetime | None
    subject: str | None
    created_at: datetime
    updated_at: datetime
    generated_at: datetime
    claims: dict[str, list[MemoClaim]]  # role -> claims
    retracted: int  # linked claims since retracted
    triggers: list[str]
    history: list[MemoHistory]
    alerts: list[MemoAlert]
    ruled_out: list[tuple[str, str]]  # (idea, reason)
    links: list[str]
    extra_sections: dict[str, str] = {}  # e.g. "Scorecard" -> markdown


def _claim(store: Backend, claim: Claim) -> MemoClaim:
    sources = []
    for link in claim.evidence:
        ev = store.get_evidence(link.evidence_id)
        sources.append(
            MemoSource(
                title=ev.title or ev.uri,
                uri=ev.uri,
                publisher=ev.publisher,
                published_at=ev.published_at,
                quote=ev.quote,
                contradicts=link.support is Support.CONTRADICTS,
            )
        )
    derived = []
    for claim_id in claim.derived_from:
        source = store.current_claim(store.get_claim(claim_id).lineage_id)
        derived.append(source.text if source else f"(retracted) {store.get_claim(claim_id).text}")
    return MemoClaim(
        id=claim.id,
        text=claim.text,
        claim_type=claim.claim_type.value,
        confidence=claim.confidence,
        valid_from=claim.valid_from,
        valid_to=claim.valid_to,
        recorded_at=claim.recorded_at,
        sources=sources,
        derived_from=derived,
    )


def build_memo(store: Backend, thesis_id: str, now: datetime) -> Memo:
    thesis = store.get_thesis(thesis_id)
    names = {eid: name for eid, (_, name) in store.entity_index().items()}
    claims: dict[str, list[MemoClaim]] = {role.value: [] for role in ROLE_TITLES}
    retracted = 0
    for link in thesis.claims:
        claim = store.current_claim(link.claim_lineage_id)
        if claim is None:
            retracted += 1
            continue
        claims[link.role.value].append(_claim(store, claim))
    alerts = []
    for alert in store.alerts(statuses=list(AlertStatus), thesis_ids=[thesis.id]):
        event = store.get_event(alert.event_id)
        top = alert.paths[0] if alert.paths else None
        alerts.append(
            MemoAlert(
                occurred_at=event.occurred_at,
                event=event.title,
                uri=event.uri,
                status=alert.status.value,
                verdict=alert.verdict.value if alert.verdict else None,
                rationale=alert.rationale,
                why=f"{top.route} via {top.via}" if top else None,
            )
        )
    alerts.sort(key=lambda a: a.occurred_at)
    ruled_out: list[tuple[str, str]] = []
    links: list[str] = []
    if thesis.subject_entity_id:
        ruled_out = [
            (h.text, h.reason) for h in store.rejected_hypotheses(thesis.subject_entity_id)
        ]
        rels = store.relations(thesis.subject_entity_id)
        # COMPETES_WITH runs both ways: list each pair once, preferring a research link.
        rels.sort(key=lambda r: r.source.startswith("starter:"))
        seen_rivals: set[frozenset[str]] = set()
        for rel in rels:
            if rel.rel is RelationType.COMPETES_WITH:
                pair = frozenset((rel.src_id, rel.dst_id))
                if pair in seen_rivals:
                    continue
                seen_rivals.add(pair)
            src, dst = names.get(rel.src_id, rel.src_id), names.get(rel.dst_id, rel.dst_id)
            starter = " (starter pack, unconfirmed)" if rel.source.startswith("starter:") else ""
            links.append(f"{src} {rel.rel.value.replace('_', ' ').lower()} {dst}{starter}")
    extra = {}
    if thesis.instrument:
        from torus.core.scorecard import render_score, score

        extra["Scorecard"] = render_score(score(store, thesis, now))
    return Memo(
        thesis_id=thesis.id,
        title=thesis.title,
        statement=thesis.statement,
        stance=thesis.stance.value,
        status=thesis.status.value,
        confidence=thesis.confidence,
        horizon=thesis.horizon,
        horizon_date=thesis.horizon_date,
        subject=names.get(thesis.subject_entity_id) if thesis.subject_entity_id else None,
        created_at=thesis.created_at,
        updated_at=thesis.updated_at,
        generated_at=now,
        claims=claims,
        retracted=retracted,
        triggers=[t.description for t in thesis.triggers],
        history=[
            MemoHistory(at=h.at, status=h.status.value, confidence=h.confidence, reason=h.reason)
            for h in store.thesis_history(thesis.id)
        ],
        alerts=alerts,
        ruled_out=ruled_out,
        links=links,
        extra_sections=extra,
    )


def _date(value: datetime | None) -> str:
    return f"{value:%Y-%m-%d}" if value else ""


def _window(claim: MemoClaim) -> str:
    if claim.valid_from or claim.valid_to:
        return f", valid {_date(claim.valid_from) or '...'} to {_date(claim.valid_to) or '...'}"
    return ""


def render_markdown(memo: Memo) -> str:
    out = [f"# {memo.title}", ""]
    meta = [
        f"**Stance:** {memo.stance}",
        f"**Status:** {memo.status.replace('_', ' ')}",
        f"**Confidence:** {memo.confidence:.0%}",
    ]
    if memo.subject:
        meta.insert(0, f"**Subject:** {memo.subject}")
    if memo.horizon:
        due = f" (due {_date(memo.horizon_date)})" if memo.horizon_date else ""
        meta.append(f"**Horizon:** {memo.horizon}{due}")
    out += [" · ".join(meta), "", f"> {memo.statement}", ""]
    out += [
        f"*Opened {_date(memo.created_at)}, last revised {_date(memo.updated_at)}."
        f" Generated by Torus on {_date(memo.generated_at)}.*",
        "",
    ]
    for name, body in memo.extra_sections.items():
        out += [f"## {name}", "", body.strip(), ""]
    for role, title in ROLE_TITLES.items():
        claims = memo.claims.get(role.value, [])
        if not claims:
            continue
        out += [f"## {title}", ""]
        for i, claim in enumerate(claims, 1):
            out.append(
                f"{i}. **{claim.text}** — {claim.claim_type}, confidence {claim.confidence:.0%}"
                f"{_window(claim)}"
            )
            for source in claim.sources:
                label = "contradicts" if source.contradicts else "source"
                meta = [m for m in (source.publisher, _date(source.published_at)) if m]
                by = f" ({', '.join(meta)})" if meta else ""
                out.append(f"   - {label}: [{source.title}]({source.uri}){by}")
                if source.quote:
                    out.append(f"     > {source.quote.strip()}")
            for text in claim.derived_from:
                out.append(f"   - derived from: {text}")
            if not claim.sources and not claim.derived_from:
                out.append("   - *no source recorded*")
        out.append("")
    if memo.retracted:
        out += [f"*{memo.retracted} claim(s) it once rested on have since been retracted.*", ""]
    if memo.triggers:
        out += ["## What would change our mind", ""]
        out += [f"- {t}" for t in memo.triggers] + [""]
    if memo.alerts:
        out += ["## Events since", "", "| Date | Event | Verdict | Why |", "|---|---|---|---|"]
        for a in memo.alerts:
            event = f"[{_cell(a.event)}]({a.uri})" if a.uri else _cell(a.event)
            verdict = a.verdict or a.status
            note = _cell(a.rationale or a.why or "")
            out.append(f"| {_date(a.occurred_at)} | {event} | {verdict} | {note} |")
        out.append("")
    if memo.ruled_out:
        out += ["## Ruled out", ""]
        out += [f"- {idea} — {reason}" for idea, reason in memo.ruled_out] + [""]
    if memo.links:
        out += [f"## {memo.subject or 'Subject'} in the graph", ""]
        out += [f"- {link}" for link in memo.links[:30]] + [""]
    out += ["## History", "", "| Date | Status | Confidence | Why |", "|---|---|---|---|"]
    for h in memo.history:
        reason = _cell(h.reason or "")
        out.append(
            f"| {_date(h.at)} | {h.status.replace('_', ' ')} | {h.confidence:.0%} | {reason} |"
        )
    out += ["", "---", f"*{DISCLAIMER} Thesis {memo.thesis_id}.*", ""]
    return "\n".join(out)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")


def _inline(text: str) -> str:
    escaped = html.escape(text, quote=False)
    escaped = _LINK.sub(
        lambda m: (
            f'<a href="{html.escape(m.group(2))}">{m.group(1)}</a>'
            if m.group(2).startswith(("http://", "https://"))
            else m.group(1)
        ),
        escaped,
    )
    escaped = _BOLD.sub(r"<strong>\1</strong>", escaped)
    return _ITALIC.sub(r"<em>\1</em>", escaped)


STYLE = """
:root {
  --bg:#fbfaf7;
  --fg:#1d1d1b;
  --muted:#6b6a65;
  --rule:#e2e0d9;
  --accent:#1f5f8b;
  --quote:#f1efe8;
}
@media (prefers-color-scheme: dark) {
  :root {
  --bg:#17181a;
  --fg:#e8e6e1;
  --muted:#a19f99;
  --rule:#2e3034;
  --accent:#7fb4dc;
  --quote:#212327;
}
}
body {
  background:var(--bg);
  color:var(--fg);
  margin:0;
  font:16px/1.55 ui-serif, Georgia, "Times New Roman", serif;
}
main {
  max-width:760px;
  margin:0 auto;
  padding:40px 16px 64px;
}
h1 {
  font-size:2rem;
  line-height:1.2;
  margin:0 0 12px;
}
h2 {
  font:600 0.8rem/1.4 ui-sans-serif, system-ui, sans-serif;
  text-transform:uppercase;
  letter-spacing:0.08em;
  color:var(--muted);
  margin:36px 0 12px;
  border-bottom:1px solid var(--rule);
  padding-bottom:6px;
}
p, li, td, th {
  overflow-wrap:anywhere;
}
blockquote {
  font-size:1.15rem;
  margin:16px 0;
  padding:12px 16px;
  border-left:3px solid var(--accent);
  background:var(--quote);
}
a {
  color:var(--accent);
}
ol, ul {
  padding-left:22px;
} li {
  margin:0 0 12px;
}
li > div + div {
  font:0.88rem/1.5 ui-sans-serif, system-ui, sans-serif;
  color:var(--muted);
}
.quote {
  font-style:italic;
  border-left:2px solid var(--rule);
  padding-left:8px;
  margin:2px 0;
}
table {
  width:100%;
  border-collapse:collapse;
  font:0.88rem/1.4 ui-sans-serif, system-ui, sans-serif;
}
th, td {
  text-align:left;
  padding:6px 8px;
  border-bottom:1px solid var(--rule);
  vertical-align:top;
}
hr {
  border:0;
  border-top:1px solid var(--rule);
  margin:32px 0 12px;
}
em {
  color:var(--muted);
}
"""


def render_html(memo: Memo) -> str:
    """The markdown memo as one self-contained HTML page (light and dark)."""
    body: list[str] = []
    lines = render_markdown(memo).splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("# "):
            body.append(f"<h1>{_inline(line[2:])}</h1>")
        elif line.startswith("## "):
            body.append(f"<h2>{_inline(line[3:])}</h2>")
        elif line.startswith("> "):
            body.append(f"<blockquote>{_inline(line[2:])}</blockquote>")
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append(lines[i])
                i += 1
            cells = [[c.strip() for c in re.split(r"(?<!\\)\|", r)[1:-1]] for r in rows]
            head = "".join(f"<th>{_inline(c)}</th>" for c in cells[0])
            rest = "".join(
                "<tr>"
                + "".join(f"<td>{_inline(c.replace(chr(92) + '|', '|'))}</td>" for c in r)
                + "</tr>"
                for r in cells[2:]
            )
            body.append(f"<table><thead><tr>{head}</tr></thead><tbody>{rest}</tbody></table>")
            continue
        elif re.match(r"^\d+\. ", line):
            items = []
            while i < len(lines) and (re.match(r"^\d+\. ", lines[i]) or lines[i].startswith("   ")):
                if re.match(r"^\d+\. ", lines[i]):
                    items.append([re.sub(r"^\d+\. ", "", lines[i])])
                else:
                    items[-1].append(lines[i].strip())
                i += 1
            parts = []
            for item in items:
                details = "".join(
                    f'<div class="quote">{_inline(d[2:])}</div>'
                    if d.startswith("> ")
                    else f"<div>{_inline(d[2:] if d.startswith('- ') else d)}</div>"
                    for d in item[1:]
                )
                parts.append(f"<li><div>{_inline(item[0])}</div>{details}</li>")
            body.append(f"<ol>{''.join(parts)}</ol>")
            continue
        elif line.startswith("- "):
            bullets = []
            while i < len(lines) and lines[i].startswith("- "):
                bullets.append(f"<li>{_inline(lines[i][2:])}</li>")
                i += 1
            body.append(f"<ul>{''.join(bullets)}</ul>")
            continue
        elif line == "---":
            body.append("<hr>")
        elif line.strip():
            body.append(f"<p>{_inline(line)}</p>")
        i += 1
    title = html.escape(memo.title)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
{STYLE}</style></head>
<body><main>
{chr(10).join(body)}
</main></body></html>
"""


def save_memo(memo: Memo, directory: Path) -> tuple[Path, Path]:
    """Write the memo as Markdown and HTML; returns both paths."""
    directory.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", memo.title.casefold()).strip("-")[:60] or "thesis"
    stem = directory / f"{slug}-{memo.thesis_id[-8:].lower()}"
    markdown, page = stem.with_suffix(".md"), stem.with_suffix(".html")
    markdown.write_text(render_markdown(memo), "utf-8")
    page.write_text(render_html(memo), "utf-8")
    return markdown, page
