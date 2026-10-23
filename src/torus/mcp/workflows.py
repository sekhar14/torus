"""MCP tools behind the workflow skills: onboarding and SEC filings, graph audit,
undo, what-if, memos, challenges and the scorecard.

``server.py`` holds the core record/recall tools; these build on the same
``TorusTools`` state and are registered by ``register_workflow_tools``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from torus.config import has_curator
from torus.core.audit import AuditReport, audit
from torus.core.challenge import WeakPoints, weak_points
from torus.core.digest import Digest, build_digest
from torus.core.filing_diff import Changed, diff_sections
from torus.core.memo import build_memo, render_markdown, save_memo
from torus.core.models import Outcome, QueueStatus, Thesis, ThesisOutcome
from torus.core.onboard import MAP_KIND, onboard
from torus.core.scorecard import Scorecard, refresh_prices, scorecard, tracked
from torus.core.starter import available_packs, seed
from torus.core.undo import SessionRecords, UndoOutcome, recent, undo
from torus.core.whatif import WhatIfResult, what_if
from torus.explorer import build_data, render
from torus.mcp.common import FETCH, READ_ONLY, WRITE, EntityRef, aware
from torus.sources.edgar_docs import SECTION_NAMES, EdgarDocs, base_form, page, section_key
from torus.sources.prices import YahooSource
from torus.watchers import sec_contact
from torus.watchers.edgar import Filing, entity_cik
from torus.watchers.http import UrllibFetcher

if TYPE_CHECKING:
    from torus.mcp.server import TorusTools

MAX_FILING_CHARS = 60_000


# -- onboarding --------------------------------------------------------------------


class OnboardedCompany(BaseModel):
    ref: str
    entity_id: str
    name: str
    created: bool
    queued_for_mapping: bool


class PackSuggestion(BaseModel):
    pack: str
    title: str
    covers: int = Field(description="How many of the onboarded companies the pack includes")


class OnboardResult(BaseModel):
    companies: list[OnboardedCompany]
    unresolved: list[str]
    suggested_packs: list[PackSuggestion]
    mapping_jobs_queued: int


class SeedResult(BaseModel):
    pack: str
    companies_added: int
    links_added: int
    links_already_present: int


class MappingJob(BaseModel):
    queue_id: str
    entity_id: str
    name: str
    cik: str | None = Field(description="None: no SEC filings to read")
    tickers: list[str]
    forms: list[str] = Field(description="Annual report forms to try, in order")
    known_links: list[str] = Field(description="Links already in the graph; confirm or extend")


class NextMapping(BaseModel):
    job: MappingJob | None
    remaining: int = Field(description="Jobs still waiting after this one")


class MappingOutcome(BaseModel):
    queue_id: str
    status: QueueStatus
    remaining: int


# -- undo -------------------------------------------------------------------------------


class RecordRef(BaseModel):
    type: str = Field(description="claim, thesis, relation, hypothesis or evidence")
    id: str


class RecentRecords(BaseModel):
    sessions: list[SessionRecords]


class UndoResult(BaseModel):
    outcomes: list[UndoOutcome]


# -- memos ------------------------------------------------------------------------------


class MemoResult(BaseModel):
    markdown: str
    markdown_path: str
    html_path: str


class ExplorerResult(BaseModel):
    path: str
    opened: bool
    companies: int
    links: int
    theses: int
    events: int


# -- filings --------------------------------------------------------------------------


class FilingRef(BaseModel):
    accession: str
    form: str
    filed: date
    url: str


class FilingDiffResult(BaseModel):
    company: str
    section: str
    latest: FilingRef
    previous: FilingRef
    added: list[str] = Field(description="New sentences")
    removed: list[str] = Field(description="Dropped sentences")
    changed: list[Changed] = Field(description="Reworded sentences, most rewritten first")
    unchanged: int
    total_added: int
    total_removed: int
    total_changed: int


class FilingText(BaseModel):
    company: str
    cik: str
    form: str
    accession: str
    filed: date
    url: str = Field(description="Cite this as the evidence URI (kind: filing)")
    section: str | None
    sections: list[str] = Field(description="Sections this filing can be read by")
    text: str
    offset: int
    next_offset: int | None = Field(description="Pass as offset to read on; None at the end")
    total_chars: int


def edgar_docs(tools: TorusTools) -> EdgarDocs:
    contact = sec_contact(tools.store)
    if not contact:
        raise ToolError(
            "No SEC contact email. Ask the user to run `torus config set sec_contact EMAIL`"
            " (the SEC requires one in the User-Agent)."
        )
    return EdgarDocs(tools.fetcher or UrllibFetcher(), contact, tools.home / "sources" / "edgar")


def company_cik(tools: TorusTools, ref: str) -> tuple[str, str]:
    """(entity id, CIK) for a company, or a tool error if it has no SEC filings."""
    entity = tools.entity(ref)
    cik = entity_cik(entity, tools.reference)
    if cik is None:
        raise ToolError(f"{entity.name} has no known CIK, so there are no SEC filings to read.")
    return entity.id, cik


def _sections(form: str, available: dict[str, str]) -> list[str]:
    names = SECTION_NAMES.get(base_form(form), {})
    friendly = sorted(n for n, k in names.items() if k in available)
    mapped = {names[n] for n in friendly}
    return friendly + [f"item {k}" for k in available if k not in mapped]


def register_workflow_tools(server: MCPServer, tools: TorusTools) -> None:
    store = tools.store
    guard = tools.guard

    # -- onboarding ------------------------------------------------------------------

    @server.tool(name="onboard", annotations=WRITE)
    @guard
    def onboard_tool(
        tickers: Annotated[list[str], Field(description="Tickers or company names to cover")],
        watch: Annotated[bool, Field(description="Watch their SEC filings")] = True,
    ) -> OnboardResult:
        """Start covering companies: add them, watch their filings, suggest starter
        packs, and queue a mapping job for each (run by the company-mapper subagent,
        which reads their annual reports and records suppliers, customers and
        competitors)."""
        report = onboard(
            store,
            tickers,
            tools.reference,
            watch=watch,
            map_companies=has_curator(tools.harness),
        )
        tools.detector.refresh()
        packs = available_packs()
        return OnboardResult(
            companies=[
                OnboardedCompany(
                    ref=c.ref,
                    entity_id=c.entity_id,
                    name=c.name,
                    created=c.created,
                    queued_for_mapping=c.queued,
                )
                for c in report.companies
            ],
            unresolved=[f"{ref} ({why})" for ref, why in report.unresolved],
            suggested_packs=[
                PackSuggestion(pack=name, title=str(packs[name]["title"]), covers=n)
                for name, n in report.packs
            ],
            mapping_jobs_queued=report.queued,
        )

    @server.tool(annotations=WRITE)
    @guard
    def seed_starter_pack(
        pack: Annotated[str, Field(description="semis, software, banks, energy or pharma")],
    ) -> SeedResult:
        """Add a curated starter pack of companies and supplier/competitor links.
        Never overrides links research recorded."""
        try:
            report = seed(store, pack, tools.reference)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        tools.detector.refresh()
        return SeedResult(
            pack=report.pack,
            companies_added=report.entities_created,
            links_added=report.relations_added,
            links_already_present=report.relations_kept,
        )

    @server.tool(annotations=WRITE)
    @guard
    def next_mapping() -> NextMapping:
        """Lease the next onboarded company to map from its annual report. Until
        `complete_mapping`, links you record are marked as curator links."""
        leased = store.lease_work(MAP_KIND, limit=1)
        remaining = len(store.queue_items(kind=MAP_KIND, statuses=[QueueStatus.PENDING]))
        if not leased:
            return NextMapping(job=None, remaining=remaining)
        item = leased[0]
        entity = store.get_entity(store.live_entity_id(str(item.payload["entity_id"])))
        tools.mapping_job = item.id
        names = {eid: name for eid, (_, name) in store.entity_index().items()}
        known = [
            f"{names.get(r.src_id, r.src_id)} -{r.rel.value}-> {names.get(r.dst_id, r.dst_id)}"
            f" ({r.source})"
            for r in store.relations(entity.id)
        ]
        return NextMapping(
            job=MappingJob(
                queue_id=item.id,
                entity_id=entity.id,
                name=entity.name,
                cik=entity_cik(entity, tools.reference),
                tickers=[a.alias for a in entity.aliases if a.alias_type.value == "ticker"],
                forms=[str(f) for f in item.payload.get("forms", ["10-K", "20-F"])],
                known_links=known,
            ),
            remaining=remaining,
        )

    @server.tool(annotations=WRITE)
    @guard
    def complete_mapping(
        queue_id: str,
        summary: Annotated[str, Field(description="Links and claims added, in a sentence")],
        failed: Annotated[
            bool, Field(description="True if it couldn't be done; it will be retried")
        ] = False,
    ) -> MappingOutcome:
        """Finish a leased mapping job (or hand it back if it failed)."""
        if failed:
            item = store.release_work(queue_id, error=summary)
        else:
            item = store.complete_work(queue_id, result={"summary": summary})
        tools.mapping_job = None
        remaining = len(store.queue_items(kind=MAP_KIND, statuses=[QueueStatus.PENDING]))
        return MappingOutcome(queue_id=item.id, status=item.status, remaining=remaining)

    # -- undo ------------------------------------------------------------------------

    @server.tool(annotations=READ_ONLY)
    @guard
    def recent_records(
        sessions: Annotated[int, Field(ge=1, le=10, description="Most recent sessions")] = 1,
    ) -> RecentRecords:
        """What the most recent research sessions recorded, newest first, and which
        records can still be undone."""
        return RecentRecords(sessions=recent(store, sessions))

    @server.tool(annotations=WRITE)
    @guard
    def undo_records(
        session_id: Annotated[str, Field(description="The session, from recent_records")],
        records: Annotated[list[RecordRef], Field(description="What to revert")],
        reason: str = "undone by the user",
    ) -> UndoResult:
        """Revert records the user doesn't want: claims are retracted (corrections go
        back to the earlier version), theses the session created are archived, links
        retired, ruled-out ideas withdrawn. History keeps everything."""
        recorded = set(store.session_records(session_id))
        unknown = [r for r in records if (r.type, r.id) not in recorded]
        if unknown:
            raise ToolError(
                "Not recorded in that session: "
                + ", ".join(f"{r.type} {r.id}" for r in unknown)
                + ". Use recent_records to see what it recorded."
            )
        items = [(r.type, r.id) for r in records]
        return UndoResult(outcomes=undo(store, items, reason, session_id=session_id))

    # -- what if and memos ------------------------------------------------------------

    @server.tool(name="whatif", annotations=READ_ONLY)
    @guard
    def whatif_tool(
        entities: Annotated[list[str], Field(description="Companies the event would be about")],
        description: Annotated[
            str, Field(description="The hypothetical, e.g. 'TSMC fab outage halts CoWoS 3 months'")
        ],
        materiality: Annotated[
            float | None,
            Field(ge=0.0, le=1.0, description="How much it matters (default: from the wording)"),
        ] = None,
        occurred_at: datetime | None = None,
    ) -> WhatIfResult:
        """What if this happened? Which theses would it reach, through which links,
        and would it raise alerts. Records nothing."""
        ids = [tools.entity(e).id for e in entities]
        when = aware(occurred_at) or tools.clock.now()
        return what_if(store, ids, description, when, materiality=materiality)

    @server.tool(annotations=WRITE)
    @guard
    def thesis_memo(thesis_id: str) -> MemoResult:
        """Write a thesis up as a memo (Markdown and a standalone HTML page): what it
        rests on with sources and quotes, triggers, events and verdicts, ruled-out
        ideas and history. Saved under TORUS_HOME/memos."""
        memo = build_memo(store, thesis_id, tools.clock.now())
        markdown_path, html_path = save_memo(memo, tools.home / "memos")
        return MemoResult(
            markdown=render_markdown(memo),
            markdown_path=str(markdown_path),
            html_path=str(html_path),
        )

    @server.tool(annotations=WRITE)
    @guard
    def open_explorer(
        focus: Annotated[
            list[str], Field(description="Companies to centre on (default: whole graph)")
        ] = [],  # noqa: B006
        hops: Annotated[int, Field(ge=1, le=4)] = 2,
        open_browser: Annotated[
            bool, Field(description="Open the HTML file in the default browser")
        ] = True,
    ) -> ExplorerResult:
        """Write the interactive graph explorer HTML file (under TORUS_HOME/explorer.html)
        and open it in the user's browser."""
        import webbrowser

        focus_ids = [tools.entity(ref).id for ref in focus]
        data = build_data(store, focus=focus_ids, hops=hops)
        out = tools.home / "explorer.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render(data), encoding="utf-8")
        opened = False
        if open_browser:
            opened = bool(webbrowser.open(out.resolve().as_uri()))
        counts = data["meta"]["counts"]
        return ExplorerResult(
            path=str(out),
            opened=opened,
            companies=int(counts["entity"]),
            links=int(data["meta"]["links"]),
            theses=int(counts["thesis"]),
            events=int(counts["event"]),
        )

    # -- challenge -------------------------------------------------------------------

    @server.tool(annotations=READ_ONLY)
    @guard
    def thesis_weak_points(thesis_id: str) -> WeakPoints:
        """Where a thesis is weakest: thin or expiring assumptions, strong supplier or
        customer dependencies it never mentions, triggers that can't fire on events,
        and opposing views already in the graph. Input for /torus:challenge."""
        return weak_points(store, thesis_id, tools.clock.now())

    # -- scorecard ---------------------------------------------------------------------

    @server.tool(annotations=WRITE)
    @guard
    def track_thesis(
        thesis_id: str,
        ticker: Annotated[
            str | None, Field(description="Instrument to track (default: the subject's ticker)")
        ] = None,
        entry_price: Annotated[
            float | None, Field(gt=0, description="Default: the close on the entry date")
        ] = None,
        entry_date: Annotated[
            datetime | None, Field(description="Default: when the thesis was recorded")
        ] = None,
        benchmark: Annotated[str | None, Field(description="Default: SPY")] = None,
        stop: Annotated[bool, Field(description="Stop tracking this thesis")] = False,
    ) -> Thesis:
        """Put a thesis on the scorecard: its return against a benchmark from an entry
        price and date, in the direction of its stance."""
        if stop:
            return store.set_thesis_tracking(thesis_id, None)
        thesis = store.get_thesis(thesis_id)
        symbol = ticker
        if symbol is None and thesis.subject_entity_id:
            subject = store.get_entity(thesis.subject_entity_id)
            symbol = next(
                (a.alias for a in subject.aliases if a.alias_type.value == "ticker"), None
            )
        if not symbol:
            raise ToolError("No ticker: pass `ticker` (the thesis's subject has none).")
        return store.set_thesis_tracking(
            thesis_id, symbol, entry_price, aware(entry_date), benchmark
        )

    @server.tool(name="scorecard", annotations=WRITE)
    @guard
    def scorecard_tool(
        refresh: Annotated[
            bool, Field(description="Fetch the latest prices first (Yahoo Finance)")
        ] = False,
    ) -> Scorecard:
        """How tracked theses have done against their benchmarks, their outcomes, and
        calibration: did the user's confidence match how often they were right?"""
        if refresh and tracked(store):
            source = YahooSource(tools.fetcher or UrllibFetcher())
            refresh_prices(store, source, tools.clock.now().date())
        return scorecard(store, tools.clock.now())

    @server.tool(annotations=WRITE)
    @guard
    def set_thesis_outcome(
        thesis_id: str,
        outcome: Outcome,
        note: Annotated[str | None, Field(description="Why")] = None,
    ) -> ThesisOutcome:
        """Record whether a thesis turned out right, wrong or unclear (overrides the
        automatic outcome; used for calibration)."""
        return store.set_thesis_outcome(thesis_id, outcome, "user", note=note)

    # -- audit -----------------------------------------------------------------------

    @server.tool(name="audit", annotations=READ_ONLY)
    @guard
    def audit_tool(
        stale_days: Annotated[
            int, Field(ge=7, le=3650, description="Flag open theses unchanged this long")
        ] = 90,
    ) -> AuditReport:
        """Health check of the research graph: expired or thinly sourced assumptions,
        theses past their horizon or gone stale, claims that may conflict,
        unconfirmed starter links, orphans. Each finding suggests a fix."""
        return audit(store, tools.clock.now(), stale_days=stale_days)

    @server.tool(name="digest", annotations=READ_ONLY)
    @guard
    def digest_tool(
        since_days: Annotated[int, Field(ge=1, le=90, description="How far back to look")] = 7,
    ) -> Digest:
        """What happened in the research over the last days: new alerts by thesis,
        thesis status and confidence changes, theses coming due, audit counts,
        scorecard moves and activity. ``markdown`` is ready to show."""
        now = tools.clock.now()
        return build_digest(store, now - timedelta(days=since_days), now)

    # -- filings ---------------------------------------------------------------------

    @server.tool(annotations=FETCH)
    @guard
    def filing_diff(
        company: EntityRef,
        form: Annotated[str, Field(description="10-K, 10-Q or 20-F")] = "10-K",
        section: Annotated[str, Field(description="e.g. risk_factors, business, mdna")] = (
            "risk_factors"
        ),
        max_items: Annotated[int, Field(ge=5, le=200)] = 40,
    ) -> FilingDiffResult:
        """What changed in a section of a company's latest filing against the previous
        one of the same form: new, dropped and reworded sentences."""
        _, cik = company_cik(tools, company)
        docs = edgar_docs(tools)
        try:
            latest = docs.latest(cik, form, index=0)
            previous = docs.latest(cik, form, index=1)
            diff = diff_sections(
                previous.section(section), latest.section(section), max_items=max_items
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

        def ref(filing: Filing) -> FilingRef:
            return FilingRef(
                accession=filing.accession, form=filing.form, filed=filing.filed, url=filing.url
            )

        return FilingDiffResult(
            company=latest.company,
            section=section_key(latest.filing.form, section),
            latest=ref(latest.filing),
            previous=ref(previous.filing),
            added=diff.added,
            removed=diff.removed,
            changed=diff.changed,
            unchanged=diff.unchanged,
            total_added=diff.total_added,
            total_removed=diff.total_removed,
            total_changed=diff.total_changed,
        )

    @server.tool(annotations=FETCH)
    @guard
    def filing_text(
        company: EntityRef,
        form: Annotated[str, Field(description="10-K, 10-Q, 20-F, 8-K...")] = "10-K",
        section: Annotated[
            str | None,
            Field(
                description="business, risk_factors, mdna, legal, market_risk, financials,"
                " or an item id like 1A; omit for the whole document"
            ),
        ] = None,
        index: Annotated[int, Field(ge=0, le=10, description="0 = latest, 1 = previous")] = 0,
        offset: Annotated[int, Field(ge=0)] = 0,
        max_chars: Annotated[int, Field(ge=2_000, le=MAX_FILING_CHARS)] = 20_000,
    ) -> FilingText:
        """Read a company's SEC filing as text, by section, from EDGAR (cached).
        Long sections come in pages: pass `next_offset` back as `offset`."""
        _, cik = company_cik(tools, company)
        try:
            doc = edgar_docs(tools).latest(cik, form, index=index)
            text = doc.section(section) if section else doc.text
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        chunk, next_offset = page(text, offset, max_chars)
        return FilingText(
            company=doc.company,
            cik=cik,
            form=doc.filing.form,
            accession=doc.filing.accession,
            filed=doc.filing.filed,
            url=doc.filing.url,
            section=section_key(doc.filing.form, section) if section else None,
            sections=_sections(doc.filing.form, doc.sections),
            text=chunk,
            offset=offset,
            next_offset=next_offset,
            total_chars=len(text),
        )
