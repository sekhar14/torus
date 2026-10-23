"""Command line interface: ``torus init | setup | status | resolve | detect | serve | hook |
watch | notify | schedule | feed | alerts | onboard | audit | whatif | memo | scorecard | config |
graph | explore | demo | doctor | export | import``."""

from __future__ import annotations

import os

# Hooks start a fresh process per event; skip pydantic's plugin discovery (~30 ms).
os.environ.setdefault("PYDANTIC_DISABLE_PLUGINS", "__all__")

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from torus import __version__
from torus.config import Paths, paths
from torus.core.detect import DEFAULT_MIN_CONFIDENCE, EntityDetector
from torus.core.reference import SEC_TICKERS_FILENAME, ReferenceData, fetch_sec_tickers
from torus.core.resolve import Resolver
from torus.core.store.sqlite import SQLiteBackend

ENV_CONTACT = "TORUS_SEC_CONTACT"
AUDIT_LOW_PER_KIND = 3  # low findings of one kind listed before `torus audit` summarises


def _reference(p: Paths) -> ReferenceData:
    return ReferenceData.load(p.reference / SEC_TICKERS_FILENAME)


def _require_db(p: Paths) -> bool:
    if not p.db.exists():
        print(f"No database at {p.db}. Run `torus init` first.", file=sys.stderr)
        return False
    return True


def _cmd_init(args: argparse.Namespace) -> int:
    p = paths().ensure()
    contact = args.contact or os.environ.get(ENV_CONTACT, "")
    if contact:
        problem = _check_config_value("sec_contact", contact)
        if problem:
            print(problem, file=sys.stderr)
            return 2
    with SQLiteBackend.open(p.db) as store:
        version = store.schema_version()
        if contact:
            store.set_setting("sec_contact", contact)
    print(f"Torus home: {p.home}")
    print(f"Database:   {p.db} (schema v{version})")
    if args.refresh_reference:
        if not contact:
            print(
                f"--refresh-reference needs --contact EMAIL (or {ENV_CONTACT}); "
                "the SEC requires it in the User-Agent.",
                file=sys.stderr,
            )
            return 2
        try:
            data = fetch_sec_tickers(p.reference / SEC_TICKERS_FILENAME, contact)
        except (OSError, ValueError) as exc:
            print(f"Reference download failed: {exc}", file=sys.stderr)
            return 1
        print(f"Reference:  {len(data)} SEC companies")
    else:
        print(f"Reference:  {len(_reference(p))} SEC companies cached")
    return 0


def _cmd_setup(args: argparse.Namespace) -> int:
    from torus.config import ENV_HOME
    from torus.schedule import Scheduler
    from torus.setup import (
        CLAUDE_CODE_COMMANDS,
        claude_desktop_config_path,
        claude_desktop_installed,
        codex_config_path,
        codex_installed,
        configure_claude_desktop,
        configure_codex,
    )

    p = paths().ensure()
    explicit_home = p.home if os.environ.get(ENV_HOME) else None
    with SQLiteBackend.open(p.db) as store:
        version = store.schema_version()
        contact = (
            args.contact
            or os.environ.get(ENV_CONTACT, "")
            or store.get_setting("sec_contact")
            or ""
        )
        if not contact and sys.stdin.isatty():
            try:
                contact = input(
                    "Email for SEC EDGAR requests (the SEC asks for one; Enter to skip): "
                ).strip()
            except EOFError:  # Windows reports NUL as a terminal
                print()
        if contact:
            problem = _check_config_value("sec_contact", contact)
            if problem:
                print(problem, file=sys.stderr)
                return 2
            store.set_setting("sec_contact", contact)

    print(f"Torus home:     {p.home}")
    print(f"Database:       {p.db} (schema v{version})")
    if contact:
        print(f"SEC contact:    {contact}")

    if not args.no_refresh_reference:
        if contact:
            try:
                data = fetch_sec_tickers(p.reference / SEC_TICKERS_FILENAME, contact)
                print(f"Reference:      {len(data)} SEC companies")
            except (OSError, ValueError) as exc:
                print(f"Reference download failed: {exc}", file=sys.stderr)
                return 1
        else:
            cached = len(_reference(p))
            print(
                f"Reference:      {cached} SEC companies cached"
                " (pass --contact EMAIL to download the SEC ticker list)"
            )
    else:
        print(f"Reference:      {len(_reference(p))} SEC companies cached")

    # Unless told otherwise, configure the apps that look installed.
    claude_path = claude_desktop_config_path()
    detected = claude_desktop_installed(claude_path)
    want_claude = detected if args.claude_desktop is None else args.claude_desktop
    codex_path = codex_config_path()
    want_codex = codex_installed(codex_path) if args.codex is None else args.codex

    if want_claude:
        try:
            configure_claude_desktop(claude_path, home=explicit_home)
            print(f"Claude Desktop: {claude_path}")
        except ValueError as exc:
            print(f"Claude Desktop setup failed: {exc}", file=sys.stderr)
            return 1
    else:
        print("Claude Desktop: not found (pass --claude-desktop to configure it anyway)")

    if want_codex:
        try:
            codex = configure_codex(codex_path, home=explicit_home)
        except ValueError as exc:
            print(f"Codex setup failed: {exc}", file=sys.stderr)
            return 1
        kept = "" if codex.updated else "; kept your existing [mcp_servers.torus]"
        print(f"Codex:          {codex.config} ({len(codex.skills)} skills{kept})")
    else:
        print("Codex:          not found (pass --codex to configure it anyway)")

    print("Claude Code:    run these in Claude Code to add the plugin:")
    for line in CLAUDE_CODE_COMMANDS:
        print(f"                  {line}")

    if not args.no_schedule:
        scheduler = Scheduler(p.home)
        try:
            what = scheduler.install(1)
            print(f"Schedule:       {what}")
        except (RuntimeError, ValueError) as exc:
            print(f"Schedule:       not installed ({exc})", file=sys.stderr)

    print("\nRestart Claude Desktop or Codex to load Torus.")
    print("New to Torus? `torus demo` builds a sample graph to look around in.")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    p = paths()
    if not _require_db(p):
        return 1
    with SQLiteBackend.open(p.db) as store:
        version = store.schema_version()
        counts = store.counts()
        queue = store.queue_counts()
    if args.json:
        info = {"home": str(p.home), "schema_version": version, "counts": counts, "queue": queue}
        print(json.dumps(info, indent=2))
    else:
        print(f"Torus home: {p.home} (schema v{version})")
        for table, n in counts.items():
            print(f"  {table:<22} {n}")
        for kind, by_status in queue.items():
            states = ", ".join(f"{n} {status}" for status, n in sorted(by_status.items()))
            print(f"  queue {kind}: {states}")
    return 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    p = paths()
    if not _require_db(p):
        return 1
    with SQLiteBackend.open(p.db) as store:
        result = Resolver(store, _reference(p)).resolve(args.ref, create=args.create)
    info = {
        "status": result.status.value,
        "method": result.method,
        "canonical_key": result.canonical_key,
        "entity": result.entity.model_dump(mode="json") if result.entity else None,
        "reference": asdict(result.reference) if result.reference else None,
        "candidates": [c.model_dump(mode="json") for c in result.candidates]
        + [asdict(c) for c in result.reference_candidates],
    }
    print(json.dumps(info, indent=2))
    return 0 if result.status.value in ("resolved", "reference") else 1


def _cmd_detect(args: argparse.Namespace) -> int:
    p = paths()
    if not _require_db(p):
        return 1
    with SQLiteBackend.open(p.db) as store:
        result = EntityDetector(store, _reference(p)).detect(args.text, args.min_confidence)
    if args.json:
        print(
            json.dumps(
                {
                    "detections": [asdict(d) for d in result.detections],
                    "ambiguous": [asdict(a) for a in result.ambiguous],
                },
                indent=2,
            )
        )
    else:
        for d in result.detections:
            where = d.entity_id or d.canonical_key
            print(f"{d.surface!r:<24} {d.method:<8} {d.confidence:.2f}  {d.name} [{where}]")
        for a in result.ambiguous:
            print(f"{a.surface!r:<24} ambiguous: {', '.join(a.candidates)}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    # Imported lazily: the MCP SDK is only needed by this command.
    from torus.mcp.server import serve

    p = paths().ensure()
    with SQLiteBackend.open(p.db) as store:
        serve(store, _reference(p))
    return 0


def _cmd_hook(args: argparse.Namespace) -> int:
    from torus.hooks import run_hook

    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    output = run_hook(args.event, raw, env=os.environ)
    if output:
        sys.stdout.write(output)
        sys.stdout.flush()
    return 0  # hooks never fail the harness


CONFIG_KEYS = {
    "sec_contact": "contact email sent to the SEC in the User-Agent",
    "alert_threshold": "minimum alert score for theses without their own (default 0.2)",
    "auto_distill": "off, ask (default: Claude offers /torus:distill) or on (background curator)",
    "capture_receipts": "on (default) or off: tell the user what each turn recorded",
    "filing_diffs": "on (default) or off: summarise risk-factor changes in 10-K/10-Q events",
    "benchmark": "ticker theses are measured against on the scorecard (default SPY)",
    "price_source": "yahoo (default) or none: where the scorecard gets daily closes",
    "notify": "strong (default), moderate or off: desktop notifications for new alerts",
}


def _check_config_value(key: str, value: str) -> str | None:
    """An error message if ``value`` isn't valid for ``key``."""
    if key == "sec_contact" and "@" not in value:
        return "sec_contact must be an email address."
    if key == "alert_threshold":
        try:
            if not 0.0 <= float(value) <= 10.0:
                raise ValueError
        except ValueError:
            return "alert_threshold must be a number from 0 to 10."
    if key == "auto_distill" and value not in ("off", "ask", "on"):
        return "auto_distill must be off, ask or on."
    if key in ("capture_receipts", "filing_diffs") and value not in ("on", "off"):
        return f"{key} must be on or off."
    if key == "price_source" and value not in ("yahoo", "none"):
        return "price_source must be yahoo or none."
    if key == "notify" and value not in ("strong", "moderate", "off"):
        return "notify must be strong, moderate or off."
    return None


def _open_store(p: Paths) -> SQLiteBackend:
    return SQLiteBackend.open(p.ensure().db)


def _cmd_watch_run(args: argparse.Namespace) -> int:
    from torus.notify import DesktopSender
    from torus.watchers import run_watchers

    p = paths()
    with _open_store(p) as store:
        report = run_watchers(
            store,
            reference=_reference(p),
            edgar=not args.no_edgar,
            rss=not args.no_rss,
            home=p.home,
            notifier=None if args.no_notify else DesktopSender(),
        )
    if args.json:
        rows = [asdict(s) for s in report.sources]
        if report.notify is not None:
            rows.append({"source": "notify", **asdict(report.notify)})
        print(json.dumps(rows, indent=2))
    else:
        for s in report.sources:
            status = (
                f"ERROR {s.error}"
                if s.error
                else (
                    f"{s.new_events} new event(s), {s.alerts} alert(s), {s.skipped} skipped"
                    f" of {s.checked}"
                )
            )
            print(f"{s.source:<40} {status}")
        if report.notify is not None and (report.notify.alerts or report.notify.errors):
            n = report.notify
            status = f"{n.sent} notification(s) for {n.alerts} alert(s)"
            status += "".join(f"; failed: {e}" for e in n.errors)
            print(f"{'notify':<40} {status}")
        print(f"Total: {report.new_events} new event(s), {report.alerts} alert(s)")
    return 1 if report.errors else 0


def _cmd_notify_test(args: argparse.Namespace) -> int:
    from torus.core.notify import Notification, NotifyError
    from torus.notify import DesktopSender

    sender = DesktopSender()
    try:
        sender.send(
            Notification(
                "Torus: test notification",
                "Strong alerts on your theses will show up like this.",
            )
        )
    except NotifyError as exc:
        print(f"Notification failed: {exc}", file=sys.stderr)
        return 1
    print("Sent a test notification. If none appeared, check the system's notification settings.")
    return 0


def _cmd_watch_add(args: argparse.Namespace) -> int:
    p = paths()
    with _open_store(p) as store:
        result = Resolver(store, _reference(p)).resolve(args.ref, create=True)
        if result.entity is None:
            print(f"Can't resolve {args.ref!r} ({result.status.value}).", file=sys.stderr)
            return 1
        entity = store.watch_entity(result.entity.id, note=args.note)
    print(f"Watching {entity.name} ({entity.id})")
    return 0


def _cmd_watch_remove(args: argparse.Namespace) -> int:
    p = paths()
    with _open_store(p) as store:
        result = Resolver(store, _reference(p)).resolve(args.ref)
        if result.entity is None or not store.unwatch_entity(result.entity.id):
            print(f"{args.ref!r} is not on the watchlist.", file=sys.stderr)
            return 1
    print(f"Stopped watching {result.entity.name}")
    return 0


def _cmd_watch_list(args: argparse.Namespace) -> int:
    with _open_store(paths()) as store:
        explicit = {e.id for e in store.watchlist()}
        targets = store.watch_targets()
        feeds = store.feeds()
    for e in targets:
        why = "watchlist" if e.id in explicit else "open thesis"
        print(f"{e.name:<45} {e.canonical_key or '':<18} ({why})")
    for f in feeds:
        print(f"feed {f['url']}" + (f" ({f['name']})" if f["name"] else ""))
    if not targets and not feeds:
        print("Nothing watched yet. Add with `torus watch add TICKER` or `torus feed add URL`.")
    return 0


def _cmd_schedule(args: argparse.Namespace) -> int:
    from torus.schedule import Scheduler

    scheduler = Scheduler(paths().ensure().home)
    if args.schedule_command == "install":
        try:
            what = scheduler.install(args.every)
        except (RuntimeError, ValueError) as exc:
            print(f"Couldn't install the schedule: {exc}", file=sys.stderr)
            return 1
        print(f"Installed: {what}. Output goes to {scheduler.log}.")
    elif args.schedule_command == "remove":
        print("Removed." if scheduler.remove() else "No Torus schedule was installed.")
    else:
        status = scheduler.status()
        print(status or "Not scheduled. Install with `torus schedule install`.")
        return 0 if status else 1
    return 0


def _cmd_feed_add(args: argparse.Namespace) -> int:
    with _open_store(paths()) as store:
        feed = store.add_feed(args.url, args.name)
    print(f"Added feed {feed['url']}")
    return 0


def _cmd_feed_remove(args: argparse.Namespace) -> int:
    with _open_store(paths()) as store:
        removed = store.remove_feed(args.url)
    if not removed:
        print(f"No feed {args.url}", file=sys.stderr)
        return 1
    print(f"Removed feed {args.url}")
    return 0


def _cmd_alerts_list(args: argparse.Namespace) -> int:
    from torus.core.impact import strength
    from torus.core.models import AlertStatus

    statuses = list(AlertStatus) if args.all else [AlertStatus.OPEN]
    with _open_store(paths()) as store:
        alerts = store.alerts(statuses=statuses)
        if not alerts:
            print("No open alerts." if not args.all else "No alerts.")
            return 0
        for alert in alerts:
            event = store.get_event(alert.event_id)
            thesis = store.get_thesis(alert.thesis_id)
            verdict = alert.verdict.value if alert.verdict else alert.status.value
            print(f"{alert.score:5.2f} {strength(alert.score):<8} [{verdict}] {event.title}")
            print(f"                -> {thesis.title} (thesis {thesis.id}, alert {alert.id})")
            if alert.graph_score is not None:
                print(
                    f"                graph {alert.graph_score:.2f}"
                    f" x materiality {alert.materiality:.2f}"
                    f" ({event.materiality_reason or 'n/a'})"
                    f" x (1 + trigger {alert.trigger_match or 0:.2f})"
                )
    return 0


def _cmd_alerts_stats(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime, timedelta

    from torus.core.alert_stats import alert_stats

    since = datetime.now(UTC) - timedelta(days=args.days) if args.days else None
    with _open_store(paths()) as store:
        stats = alert_stats(store, since)
    if args.json:
        print(stats.model_dump_json(indent=2))
        return 0
    window = f"last {args.days} days" if args.days else "all time"
    precision = f"{stats.precision:.0%}" if stats.precision is not None else "n/a"
    print(
        f"Alerts ({window}): {stats.alerts} raised, {stats.open} open, {stats.judged} judged."
        f" Precision {precision} ({stats.unrelated} judged unrelated)."
    )
    for title, buckets in (
        ("source", stats.by_source),
        ("form", stats.by_form),
        ("hops", stats.by_hops),
        ("materiality", stats.by_materiality),
    ):
        print(f"\nBy {title}:")
        for b in buckets:
            p = f"{b.precision:.0%}" if b.precision is not None else "  - "
            print(f"  {b.label:<20} {b.alerts:>4} alerts  {b.judged:>4} judged  precision {p}")
    return 0


def _cmd_alerts_threshold(args: argparse.Namespace) -> int:
    value = None if args.value == "default" else args.value
    if value is not None:
        problem = _check_config_value("alert_threshold", value)
        if problem:
            print(problem, file=sys.stderr)
            return 2
    with _open_store(paths()) as store:
        if args.thesis:
            thesis = store.set_alert_threshold(args.thesis, float(value) if value else None)
            shown = thesis.alert_threshold if thesis.alert_threshold is not None else "global"
            print(f"Alert threshold for {thesis.title}: {shown}")
        else:
            store.set_setting("alert_threshold", value)
            print(f"Global alert threshold: {value or 'default (0.2)'}")
    return 0


def _cmd_alerts_mute(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from torus.core.models import EventKind

    p = paths()
    with _open_store(p) as store:
        entity_id = None
        if args.entity:
            entity_id = _resolve_or_fail(store, p, args.entity)
            if entity_id is None:
                return 1
        until = datetime.fromisoformat(args.until).replace(tzinfo=UTC) if args.until else None
        try:
            mute = store.add_thesis_mute(
                args.thesis,
                entity_id=entity_id,
                event_kind=EventKind(args.kind) if args.kind else None,
                form=args.form,
                until=until,
                note=args.note,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    print(f"Muted (mute {mute.id}).")
    return 0


def _cmd_alerts_unmute(args: argparse.Namespace) -> int:
    with _open_store(paths()) as store:
        if not store.remove_thesis_mute(args.mute_id):
            print(f"No active mute {args.mute_id}.", file=sys.stderr)
            return 1
    print(f"Removed mute {args.mute_id}.")
    return 0


def _cmd_alerts_mutes(args: argparse.Namespace) -> int:
    with _open_store(paths()) as store:
        mutes = store.thesis_mutes(args.thesis)
        names = {eid: name for eid, (_, name) in store.entity_index().items()}
        for m in mutes:
            parts = [
                f"entity={names.get(m.entity_id, m.entity_id)}" if m.entity_id else "",
                f"kind={m.event_kind.value}" if m.event_kind else "",
                f"form={m.form}" if m.form else "",
                f"until={m.until:%Y-%m-%d}" if m.until else "",
            ]
            title = store.get_thesis(m.thesis_id).title
            print(f"{m.id}  {title}: {' '.join(x for x in parts if x)}")
    if not mutes:
        print("No mutes.")
    return 0


def _cmd_onboard(args: argparse.Namespace) -> int:
    from torus.core.onboard import onboard, read_tickers
    from torus.core.starter import available_packs

    refs = list(args.tickers)
    if args.file:
        refs += read_tickers(Path(args.file).read_text(encoding="utf-8-sig"))
    if not refs:
        print("Give tickers, or --file with a CSV of tickers.", file=sys.stderr)
        return 2
    p = paths()
    reference = _reference(p)
    with _open_store(p) as store:
        report = onboard(store, refs, reference, watch=not args.no_watch, map_companies=True)
    for c in report.companies:
        state = "new" if c.created else "known"
        job = ", queued for mapping" if c.queued else ""
        print(f"  {c.ref:<8} {c.name} ({state}{job})")
    for ref, why in report.unresolved:
        print(f"  {ref:<8} not added: {why}", file=sys.stderr)
    print(
        f"Onboarded {len(report.companies)} companies"
        + ("" if args.no_watch else ", watching their filings")
        + f"; {report.queued} mapping job(s) queued."
    )
    packs = available_packs()
    for name, covered in report.packs:
        print(
            f"  Suggested pack: {name} ({packs[name]['title']}), covers {covered}."
            f" Add it with `torus graph seed {name}`."
        )
    if report.queued:
        print(
            "Run /torus:onboard in Claude Code to map their suppliers, customers and"
            " competitors from their annual reports."
        )
    if not len(reference):
        print(
            "Note: no SEC reference data; run `torus init --refresh-reference --contact EMAIL`"
            " so tickers resolve to SEC companies.",
            file=sys.stderr,
        )
    return 1 if report.unresolved and not report.companies else 0


def _cmd_audit(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from torus.core.audit import Severity, audit

    with _open_store(paths()) as store:
        report = audit(store, datetime.now(UTC), stale_days=args.stale_days)
    if args.json:
        print(report.model_dump_json(indent=2))
        return 0
    if not report.findings:
        print("No findings: the graph looks healthy.")
        return 0
    # Low findings of one kind (e.g. every starter link after seeding a pack) can run to
    # dozens; show a few and summarise the rest so the high and medium ones stay visible.
    shown: dict[str, int] = {}
    for f in report.findings:
        if not args.all and f.severity is Severity.LOW:
            shown[f.kind] = shown.get(f.kind, 0) + 1
            if shown[f.kind] > AUDIT_LOW_PER_KIND:
                continue
        print(f"[{f.severity.value:<6}] {f.title}")
        print(f"         {f.detail[:160]}")
        print(f"         -> {f.suggestion} ({f.object_type} {', '.join(f.object_ids)})")
    for kind, count in shown.items():
        if count > AUDIT_LOW_PER_KIND:
            more = count - AUDIT_LOW_PER_KIND
            print(f"[low   ] ... and {more} more {kind.replace('_', ' ')} finding(s)")
    if any(count > AUDIT_LOW_PER_KIND for count in shown.values()):
        print("         (torus audit --all lists every one)")
    s = report.by_severity
    print(
        f"\n{len(report.findings)} finding(s): {s.get('high', 0)} high, {s.get('medium', 0)}"
        f" medium, {s.get('low', 0)} low. Fix them with /torus:audit in Claude Code."
    )
    return 0


def _cmd_whatif(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from torus.core.impact import strength
    from torus.core.whatif import what_if

    p = paths()
    with _open_store(p) as store:
        ids = []
        for ref in args.entities:
            entity_id = _resolve_or_fail(store, p, ref)
            if entity_id is None:
                return 1
            ids.append(entity_id)
        result = what_if(store, ids, args.description, datetime.now(UTC), args.materiality)
    if args.json:
        print(result.model_dump_json(indent=2))
        return 0
    print(
        f"Materiality {result.materiality:.2f} ({result.materiality_reason}),"
        f" alert threshold {result.threshold:.2f}"
    )
    if not result.hits:
        print("No open thesis would be reached.")
    for h in result.hits:
        mark = f"ALERT {strength(h.hit.score)}" if h.would_alert else "below"
        print(
            f"{h.hit.score:5.2f} {mark:<14} {h.hit.thesis_title}"
            f" ({h.stance.value}, {h.status.value})"
        )
        for path in h.hit.paths:
            print(f"        {path.route} via {path.via}")
        if h.hit.matched_trigger:
            print(f"        matches trigger: {h.hit.matched_trigger}")
    return 0


def _cmd_memo(args: argparse.Namespace) -> int:
    import webbrowser
    from datetime import UTC, datetime

    from torus.core.errors import NotFoundError
    from torus.core.memo import build_memo, save_memo

    p = paths()
    with _open_store(p) as store:
        try:
            memo = build_memo(store, args.thesis, datetime.now(UTC))
        except NotFoundError:
            print(f"No thesis {args.thesis}.", file=sys.stderr)
            return 1
    out_dir = Path(args.out) if args.out else p.home / "memos"
    markdown, page = save_memo(memo, out_dir)
    print(f"Wrote {markdown}\nWrote {page}")
    if args.open:
        webbrowser.open(page.resolve().as_uri())
    return 0


def _pct(value: float | None) -> str:
    return f"{value:+.1%}" if value is not None else "   n/a"


def _cmd_scorecard(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from torus.core.scorecard import MIN_DECIDED, refresh_prices, scorecard, tracked
    from torus.sources.prices import YahooSource
    from torus.watchers.http import UrllibFetcher

    now = datetime.now(UTC)
    with _open_store(paths()) as store:
        if args.refresh and tracked(store):
            result = refresh_prices(store, YahooSource(UrllibFetcher()), now.date())
            for symbol, rows in result.items():
                if isinstance(rows, str):
                    print(f"  {symbol}: {rows}", file=sys.stderr)
        card = scorecard(store, now)
    if args.json:
        print(card.model_dump_json(indent=2))
        return 0
    if not card.theses:
        print(
            "No theses tracked against a price yet."
            " Track one with `torus scorecard track THESIS_ID [TICKER]`."
        )
    for s in card.theses:
        outcome = f" [{s.outcome.value}]" if s.outcome else ""
        print(f"{s.symbol:<6} {s.stance.value:<8} {_pct(s.stance_return)}  {s.title}{outcome}")
        print(
            f"       return {_pct(s.asset_return)} vs {s.benchmark} {_pct(s.benchmark_return)},"
            f" drawdown {_pct(s.max_drawdown)}" + (f"  ({s.missing})" if s.missing else "")
        )
    c = card.calibration
    if c.decided:
        print(
            f"\nCalibration (every decided thesis, tracked or not): {c.right}/{c.decided} right,"
            f" Brier {c.brier:.3f} (0 is perfect, 0.25 is a coin flip)"
        )
        if c.decided < MIN_DECIDED:
            print(
                f"  Only {c.decided} decided {'thesis' if c.decided == 1 else 'theses'}:"
                f" too few to read much into; {MIN_DECIDED}+ makes it meaningful."
            )
        for band in c.bands:
            if band.theses:
                print(
                    f"  confidence {band.label:<9} {band.theses:>3} theses,"
                    f" right {band.hit_rate:.0%} (mean confidence {band.mean_confidence:.0%})"
                )
    else:
        print("\nCalibration: no decided theses yet.")
    return 0


def _cmd_scorecard_track(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    with _open_store(paths()) as store:
        thesis = store.get_thesis(args.thesis)
        symbol = args.ticker
        if symbol is None and thesis.subject_entity_id:
            subject = store.get_entity(thesis.subject_entity_id)
            symbol = next(
                (a.alias for a in subject.aliases if a.alias_type.value == "ticker"), None
            )
        if not symbol:
            print("Give a ticker: the thesis's subject has none.", file=sys.stderr)
            return 2
        entry_at = (
            datetime.fromisoformat(args.entry_date).replace(tzinfo=UTC) if args.entry_date else None
        )
        thesis = store.set_thesis_tracking(
            thesis.id, symbol, args.entry_price, entry_at, args.benchmark
        )
    print(f"Tracking {thesis.title} as {thesis.instrument}. Run `torus scorecard --refresh`.")
    return 0


def _cmd_scorecard_outcome(args: argparse.Namespace) -> int:
    with _open_store(paths()) as store:
        if args.outcome == "clear":
            store.clear_thesis_outcome(args.thesis)
            print("Outcome cleared.")
        else:
            store.set_thesis_outcome(args.thesis, args.outcome, "user", note=args.note)
            print(f"Outcome set: {args.outcome}.")
    return 0


def _cmd_scorecard_import(args: argparse.Namespace) -> int:
    from torus.sources.prices import read_price_csv

    rows = read_price_csv(Path(args.file).read_text(encoding="utf-8-sig"))
    if not rows:
        print("No date,close rows found.", file=sys.stderr)
        return 1
    with _open_store(paths()) as store:
        store.upsert_prices(args.symbol, rows, "csv")
    print(f"Imported {len(rows)} closes for {args.symbol.upper()} ({rows[0][0]} to {rows[-1][0]}).")
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    if args.key not in CONFIG_KEYS:
        print(f"Unknown key {args.key!r}. Known: {', '.join(CONFIG_KEYS)}", file=sys.stderr)
        return 2
    with _open_store(paths()) as store:
        if args.action == "set":
            problem = _check_config_value(args.key, args.value) if args.value else None
            if problem:
                print(problem, file=sys.stderr)
                return 2
            store.set_setting(args.key, args.value or None)
        value = store.get_setting(args.key)
    print(f"{args.key} = {value if value is not None else '(unset)'}")
    return 0


def _cmd_graph_packs(args: argparse.Namespace) -> int:
    from torus.core.starter import available_packs

    for name, info in available_packs().items():
        print(
            f"{name:<10} v{info['version']}  {info['entities']} entities, {info['relations']} links"
        )
        print(f"           {info['title']}")
    return 0


def _cmd_graph_seed(args: argparse.Namespace) -> int:
    from torus.core.starter import seed

    p = paths()
    reference = _reference(p)
    if not len(reference):
        print(
            "Note: no SEC reference data, so companies are keyed by ticker. Run"
            " `torus init --refresh-reference --contact EMAIL` first for CIK-keyed companies.",
            file=sys.stderr,
        )
    with _open_store(p) as store:
        try:
            report = seed(store, args.pack, reference)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    print(
        f"Seeded {report.pack}: {report.entities_created} new companies"
        f" ({report.entities_existing} existing), {report.relations_added} new links"
        f" ({report.relations_kept} already present)."
    )
    for note in report.notes:
        print(f"  note: {note}")
    return 0


def _resolve_or_fail(store: SQLiteBackend, p: Paths, ref: str) -> str | None:
    result = Resolver(store, _reference(p)).resolve(ref)
    if result.entity is None:
        print(f"Can't resolve {ref!r} in the graph ({result.status.value}).", file=sys.stderr)
        return None
    return result.entity.id


def _cmd_graph_neighbors(args: argparse.Namespace) -> int:
    from torus.core.graph import neighbors

    p = paths()
    with _open_store(p) as store:
        entity_id = _resolve_or_fail(store, p, args.ref)
        if entity_id is None:
            return 1
        sub = neighbors(store, [entity_id], hops=args.hops)
    names = {n.id: n.label for n in sub.nodes}
    for edge in sub.edges:
        if edge.kind.isupper():
            print(
                f"{names[edge.source]} -{edge.kind}-> {names[edge.target]}"
                f"  ({edge.confidence:.2f}, {edge.provenance})"
            )
    theses = [n for n in sub.nodes if n.kind == "thesis"]
    for t in theses:
        print(f"thesis [{t.data['status']}] {t.label}")
    return 0


def _cmd_graph_paths(args: argparse.Namespace) -> int:
    from torus.core.graph import find_paths

    p = paths()
    with _open_store(p) as store:
        a = _resolve_or_fail(store, p, args.source)
        b = _resolve_or_fail(store, p, args.target)
        if a is None or b is None:
            return 1
        result = find_paths(store, a, b, max_hops=args.max_hops)
    if not result.paths:
        print(f"No path within {args.max_hops} hops.")
        return 1
    for path, text in zip(result.paths, result.texts, strict=True):
        print(f"{path.confidence:.2f}  {text}")
    return 0


def _cmd_explore(args: argparse.Namespace) -> int:
    import webbrowser

    from torus.explorer import build_data, render

    p = paths()
    with _open_store(p) as store:
        focus = []
        for ref in args.focus or []:
            entity_id = _resolve_or_fail(store, p, ref)
            if entity_id is None:
                return 1
            focus.append(entity_id)
        data = build_data(store, focus=focus, hops=args.hops, include_isolated=args.all)
    out = Path(args.out) if args.out else p.home / "explorer.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(data), encoding="utf-8")
    counts = data["meta"]["counts"]
    print(
        f"Wrote {out} ({counts['entity']} companies, {data['meta']['links']} links,"
        f" {counts['thesis']} theses, {counts['event']} events)"
    )
    if not args.no_open:
        webbrowser.open(out.resolve().as_uri())
    return 0


DEMO_MARKER = ".torus-demo"  # marks a home `torus demo` made, so it may be rebuilt


def _cmd_demo(args: argparse.Namespace) -> int:
    import webbrowser

    from torus.core.demo import DEMO_PACK, seed_demo
    from torus.core.models import AlertStatus
    from torus.explorer import build_data, render

    real = paths()
    home = Path(args.home).expanduser().resolve() if args.home else real.home / "demo"
    if home == real.home:
        print(
            f"{home} is your TORUS_HOME; the demo needs a home of its own (pass --home DIR).",
            file=sys.stderr,
        )
        return 2
    p = Paths(home)
    if p.db.exists() and not (home / DEMO_MARKER).exists():
        print(f"{p.db} isn't a demo graph; pick another --home.", file=sys.stderr)
        return 2
    p.ensure()
    (home / DEMO_MARKER).write_text("made by `torus demo`; safe to delete\n", "utf-8")
    for suffix in ("", "-wal", "-shm"):
        Path(str(p.db) + suffix).unlink(missing_ok=True)

    counts = seed_demo(p.db, pack=None if args.no_pack else DEMO_PACK, reference=_reference(real))
    with SQLiteBackend.open(p.db) as store:
        data = build_data(store)
        open_alerts = len(store.alerts(statuses=[AlertStatus.OPEN]))
    out = home / "explorer.html"
    out.write_text(render(data), encoding="utf-8")
    print(
        f"Demo graph in {home}: {counts['entities']} companies, {counts['theses']} theses,"
        f" {open_alerts} open alert(s). The research is marked [demo]; your own graph"
        " is untouched."
    )
    print(f"Explorer:  {out}")
    env = f'$env:TORUS_HOME = "{home}"' if os.name == "nt" else f'export TORUS_HOME="{home}"'
    print("To try commands on it, point TORUS_HOME at it first:")
    print(f"  {env}")
    print("  torus alerts list; torus audit; torus memo <thesis id>")
    if not args.no_open:
        webbrowser.open(out.resolve().as_uri())
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    from torus.config import ENV_HOME
    from torus.maintenance import Level, doctor, fix, repair_duplicate_links, repair_search_index

    p = paths()
    if args.contact:
        problem = _check_config_value("sec_contact", args.contact)
        if problem:
            print(problem, file=sys.stderr)
            return 2
    if args.repair and not args.fix:
        repair_search_index(p)
        closed = repair_duplicate_links(p)
        print(f"Search index rebuilt; {closed} duplicate link(s) closed.")
    checks = doctor(p)
    if args.fix:
        contact = args.contact
        needs_email = {"watchers", "SEC reference"} & {
            c.name for c in checks if c.level is not Level.OK
        }
        if not contact and needs_email and sys.stdin.isatty():
            with SQLiteBackend.open(p.db) as store:
                stored = store.get_setting("sec_contact") or os.environ.get(ENV_CONTACT)
            if not stored:
                try:
                    contact = input(
                        "Email for SEC EDGAR requests (the SEC asks for one; Enter to skip): "
                    ).strip()
                except EOFError:  # Windows reports NUL as a terminal
                    print()
                if contact and _check_config_value("sec_contact", contact):
                    print("Not an email address; skipping.", file=sys.stderr)
                    contact = ""
        explicit_home = p.home if os.environ.get(ENV_HOME) else None
        done = fix(p, checks, contact=contact or None, home=explicit_home)
        for line in done:
            print(f"[fix ] {line}")
        if done:
            print()
            checks = doctor(p)
        else:
            print("Nothing Torus can fix by itself.\n")
    marks = {Level.OK: "ok  ", Level.INFO: "info", Level.WARN: "WARN", Level.FAIL: "FAIL"}
    for check in checks:
        print(f"[{marks[check.level]}] {check.name:<20} {check.detail}")
    failed = [c for c in checks if c.level is Level.FAIL]
    warned = [c for c in checks if c.level is Level.WARN]
    print(f"\n{len(failed)} problem(s), {len(warned)} warning(s).")
    return 1 if failed else 0


def _cmd_export(args: argparse.Namespace) -> int:
    from torus.maintenance import default_export_path, export_graph

    p = paths()
    out = Path(args.out) if args.out else default_export_path(p)
    out.parent.mkdir(parents=True, exist_ok=True)
    with _open_store(p) as store, out.open("w", encoding="utf-8", newline="\n") as handle:
        counts = export_graph(store, handle)
    print(f"Exported {sum(counts.values())} rows from {len(counts)} tables to {out}")
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    from torus.maintenance import import_graph

    p = paths()
    with _open_store(p) as store, open(args.file, encoding="utf-8") as handle:
        try:
            counts = import_graph(store, handle, force=args.force)
        except ValueError as exc:
            print(f"Import failed, nothing changed: {exc}", file=sys.stderr)
            return 1
    print(f"Imported {sum(counts.values())} rows into {p.db}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="torus", description=__doc__)
    parser.add_argument("--version", action="version", version=f"torus {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create TORUS_HOME and the database")
    init.add_argument(
        "--refresh-reference",
        action="store_true",
        help="download the SEC company ticker list",
    )
    init.add_argument("--contact", help=f"email for the SEC User-Agent (or ${ENV_CONTACT})")
    init.set_defaults(func=_cmd_init)

    setup = sub.add_parser(
        "setup",
        help="initialise Torus and configure Claude Desktop and Codex desktop",
    )
    setup.add_argument("--contact", help=f"email for the SEC User-Agent (or ${ENV_CONTACT})")
    setup.add_argument(
        "--claude-desktop",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="configure Claude Desktop (default: if it is installed)",
    )
    setup.add_argument(
        "--codex",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="configure Codex and install the Torus skills (default: if it is installed)",
    )
    setup.add_argument(
        "--no-schedule",
        action="store_true",
        help="don't install the hourly watcher schedule",
    )
    setup.add_argument(
        "--no-refresh-reference",
        action="store_true",
        help="don't download the SEC company ticker list",
    )
    setup.set_defaults(func=_cmd_setup)

    status = sub.add_parser("status", help="show database location and counts")
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.set_defaults(func=_cmd_status)

    resolve = sub.add_parser("resolve", help="resolve a ticker, CIK, name or id to an entity")
    resolve.add_argument("ref")
    resolve.add_argument(
        "--create", action="store_true", help="add reference-only companies to the graph"
    )
    resolve.set_defaults(func=_cmd_resolve)

    detect = sub.add_parser("detect", help="list entities mentioned in a piece of text")
    detect.add_argument("text")
    detect.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE)
    detect.add_argument("--json", action="store_true", help="machine-readable output")
    detect.set_defaults(func=_cmd_detect)

    serve = sub.add_parser("serve", help="run the MCP server over stdio")
    serve.set_defaults(func=_cmd_serve)

    from torus.hooks import EVENTS

    hook = sub.add_parser("hook", help="handle a harness hook event (JSON on stdin)")
    hook.add_argument("event", choices=EVENTS)
    hook.set_defaults(func=_cmd_hook)

    watch = sub.add_parser("watch", help="watch companies and feeds for new events")
    watch_sub = watch.add_subparsers(dest="watch_command", required=True)
    run = watch_sub.add_parser("run", help="check EDGAR and feeds once (for a scheduler)")
    run.add_argument("--no-edgar", action="store_true")
    run.add_argument("--no-rss", action="store_true")
    run.add_argument("--no-notify", action="store_true", help="no desktop notifications")
    run.add_argument("--json", action="store_true", help="machine-readable output")
    run.set_defaults(func=_cmd_watch_run)
    add = watch_sub.add_parser("add", help="watch a company (ticker, name, CIK or id)")
    add.add_argument("ref")
    add.add_argument("--note")
    add.set_defaults(func=_cmd_watch_add)
    remove = watch_sub.add_parser("remove", help="stop watching a company")
    remove.add_argument("ref")
    remove.set_defaults(func=_cmd_watch_remove)
    watch_list = watch_sub.add_parser("list", help="what is watched, and why")
    watch_list.set_defaults(func=_cmd_watch_list)

    notify = sub.add_parser("notify", help="desktop notifications for new alerts")
    notify_sub = notify.add_subparsers(dest="notify_command", required=True)
    notify_sub.add_parser("test", help="show a test notification").set_defaults(
        func=_cmd_notify_test
    )

    schedule = sub.add_parser("schedule", help="run the watchers on a schedule")
    schedule_sub = schedule.add_subparsers(dest="schedule_command", required=True)
    sched_install = schedule_sub.add_parser(
        "install", help="run `torus watch run` regularly (schtasks, launchd or cron)"
    )
    sched_install.add_argument("--every", type=int, default=1, help="hours between runs")
    sched_install.set_defaults(func=_cmd_schedule)
    schedule_sub.add_parser("remove", help="remove the schedule").set_defaults(func=_cmd_schedule)
    schedule_sub.add_parser("status", help="show the schedule").set_defaults(func=_cmd_schedule)

    feed = sub.add_parser("feed", help="manage RSS/Atom feeds")
    feed_sub = feed.add_subparsers(dest="feed_command", required=True)
    feed_add = feed_sub.add_parser("add")
    feed_add.add_argument("url")
    feed_add.add_argument("--name")
    feed_add.set_defaults(func=_cmd_feed_add)
    feed_remove = feed_sub.add_parser("remove")
    feed_remove.add_argument("url")
    feed_remove.set_defaults(func=_cmd_feed_remove)

    alerts = sub.add_parser("alerts", help="alerts raised by events on theses")
    alerts_sub = alerts.add_subparsers(dest="alerts_command", required=True)
    al_list = alerts_sub.add_parser("list", help="open alerts and how they scored")
    al_list.add_argument("--all", action="store_true", help="include judged alerts")
    al_list.set_defaults(func=_cmd_alerts_list)
    al_stats = alerts_sub.add_parser("stats", help="how often alerts were judged related")
    al_stats.add_argument("--days", type=int, help="only alerts from the last N days")
    al_stats.add_argument("--json", action="store_true", help="machine-readable output")
    al_stats.set_defaults(func=_cmd_alerts_stats)
    al_thr = alerts_sub.add_parser("threshold", help="minimum alert score (global or per thesis)")
    al_thr.add_argument("value", help="a number from 0 to 10, or 'default'")
    al_thr.add_argument("--thesis", help="set it for this thesis id only")
    al_thr.set_defaults(func=_cmd_alerts_threshold)
    al_mute = alerts_sub.add_parser("mute", help="stop alerts on a thesis from matching events")
    al_mute.add_argument("thesis", help="thesis id")
    al_mute.add_argument("--entity", help="only events about this company")
    al_mute.add_argument(
        "--kind", choices=["filing", "news", "earnings", "price_move", "macro", "manual"]
    )
    al_mute.add_argument("--form", help="only this SEC form, e.g. 10-Q")
    al_mute.add_argument("--until", help="date (YYYY-MM-DD) the mute ends")
    al_mute.add_argument("--note")
    al_mute.set_defaults(func=_cmd_alerts_mute)
    al_unmute = alerts_sub.add_parser("unmute", help="remove a mute")
    al_unmute.add_argument("mute_id")
    al_unmute.set_defaults(func=_cmd_alerts_unmute)
    al_mutes = alerts_sub.add_parser("mutes", help="list mutes")
    al_mutes.add_argument("thesis", nargs="?", help="only this thesis")
    al_mutes.set_defaults(func=_cmd_alerts_mutes)

    onboard_p = sub.add_parser(
        "onboard", help="start covering companies (tickers or a portfolio CSV)"
    )
    onboard_p.add_argument("tickers", nargs="*", help="tickers or company names")
    onboard_p.add_argument("--file", help="CSV with a ticker or symbol column")
    onboard_p.add_argument("--no-watch", action="store_true", help="don't watch their filings")
    onboard_p.set_defaults(func=_cmd_onboard)

    audit_p = sub.add_parser("audit", help="find stale, thin or conflicting research")
    audit_p.add_argument("--stale-days", type=int, default=90, help="days unchanged = stale")
    audit_p.add_argument("--json", action="store_true", help="machine-readable output")
    audit_p.add_argument(
        "--all", action="store_true", help="list every low finding instead of a summary"
    )
    audit_p.set_defaults(func=_cmd_audit)

    whatif = sub.add_parser("whatif", help="which theses would a hypothetical event reach?")
    whatif.add_argument("description", help="e.g. 'TSMC fab outage halts CoWoS for 3 months'")
    whatif.add_argument("entities", nargs="+", help="companies the event is about")
    whatif.add_argument("--materiality", type=float, help="0-1 (default: from the wording)")
    whatif.add_argument("--json", action="store_true", help="machine-readable output")
    whatif.set_defaults(func=_cmd_whatif)

    memo = sub.add_parser("memo", help="write a thesis up as a sourced memo (Markdown + HTML)")
    memo.add_argument("thesis", help="thesis id")
    memo.add_argument("--out", help="directory (default TORUS_HOME/memos)")
    memo.add_argument("--open", action="store_true", help="open the HTML in a browser")
    memo.set_defaults(func=_cmd_memo)

    card = sub.add_parser("scorecard", help="how tracked theses did, and calibration")
    card.add_argument("--refresh", action="store_true", help="fetch the latest prices first")
    card.add_argument("--json", action="store_true", help="machine-readable output")
    card.set_defaults(func=_cmd_scorecard)
    card_sub = card.add_subparsers(dest="scorecard_command")
    track = card_sub.add_parser("track", help="track a thesis against a ticker")
    track.add_argument("thesis", help="thesis id")
    track.add_argument("ticker", nargs="?", help="default: the subject's ticker")
    track.add_argument("--entry-price", type=float)
    track.add_argument("--entry-date", help="YYYY-MM-DD (default: when it was recorded)")
    track.add_argument("--benchmark", help="default: SPY")
    track.set_defaults(func=_cmd_scorecard_track)
    outcome = card_sub.add_parser("outcome", help="set whether a thesis was right")
    outcome.add_argument("thesis", help="thesis id")
    outcome.add_argument("outcome", choices=["right", "wrong", "unclear", "clear"])
    outcome.add_argument("--note")
    outcome.set_defaults(func=_cmd_scorecard_outcome)
    imp_prices = card_sub.add_parser("import", help="import daily closes from a CSV")
    imp_prices.add_argument("symbol")
    imp_prices.add_argument("file", help="CSV with date and close columns")
    imp_prices.set_defaults(func=_cmd_scorecard_import)

    config = sub.add_parser("config", help="get or set a Torus setting")
    config.add_argument("action", choices=["get", "set"])
    config.add_argument("key", help=", ".join(f"{k} ({v})" for k, v in CONFIG_KEYS.items()))
    config.add_argument("value", nargs="?", help="new value (set); omit to unset")
    config.set_defaults(func=_cmd_config)

    graph = sub.add_parser("graph", help="starter packs and graph queries")
    graph_sub = graph.add_subparsers(dest="graph_command", required=True)
    packs = graph_sub.add_parser("packs", help="list starter graph packs")
    packs.set_defaults(func=_cmd_graph_packs)
    seed = graph_sub.add_parser("seed", help="add a starter pack's companies and links")
    seed.add_argument("pack", nargs="?", default="semis")
    seed.set_defaults(func=_cmd_graph_seed)
    nb = graph_sub.add_parser("neighbors", help="links around a company")
    nb.add_argument("ref")
    nb.add_argument("--hops", type=int, default=1)
    nb.set_defaults(func=_cmd_graph_neighbors)
    gp = graph_sub.add_parser("paths", help="how two companies are connected")
    gp.add_argument("source")
    gp.add_argument("target")
    gp.add_argument("--max-hops", type=int, default=3)
    gp.set_defaults(func=_cmd_graph_paths)

    explore = sub.add_parser("explore", help="open the visual graph explorer in a browser")
    explore.add_argument(
        "focus", nargs="*", help="companies to centre on (default: the whole graph)"
    )
    explore.add_argument("--hops", type=int, default=2, help="links around the focus (1-4)")
    explore.add_argument("--all", action="store_true", help="include companies with no links")
    explore.add_argument("--out", help="where to write the HTML (default TORUS_HOME/explorer.html)")
    explore.add_argument("--no-open", action="store_true", help="write the file only")
    explore.set_defaults(func=_cmd_explore)

    demo = sub.add_parser(
        "demo", help="build a labelled demo graph in its own home and open the explorer"
    )
    demo.add_argument("--home", help="where to build it (default TORUS_HOME/demo)")
    demo.add_argument("--no-pack", action="store_true", help="skip the semis starter pack")
    demo.add_argument("--no-open", action="store_true", help="don't open a browser")
    demo.set_defaults(func=_cmd_demo)

    doctor = sub.add_parser("doctor", help="check the installation and database")
    doctor.add_argument(
        "--repair",
        action="store_true",
        help="rebuild the search index and close duplicate links first",
    )
    doctor.add_argument(
        "--fix",
        action="store_true",
        help="fix what the checks flag: search index, duplicate links, SEC email and ticker"
        " list, watcher schedule, Claude Desktop and Codex entries",
    )
    doctor.add_argument("--contact", help="email for SEC EDGAR requests (with --fix)")
    doctor.set_defaults(func=_cmd_doctor)

    export = sub.add_parser("export", help="back up the whole graph as JSON lines")
    export.add_argument("--out", help="file to write (default TORUS_HOME/exports/...)")
    export.set_defaults(func=_cmd_export)

    imp = sub.add_parser("import", help="restore a graph exported with `torus export`")
    imp.add_argument("file")
    imp.add_argument("--force", action="store_true", help="allow a non-empty database")
    imp.set_defaults(func=_cmd_import)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
