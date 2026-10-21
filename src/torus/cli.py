"""Command line interface: ``torus init | status | resolve | detect | serve | hook |
watch | feed | config | graph | explore | doctor | export | import``."""

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


def _reference(p: Paths) -> ReferenceData:
    return ReferenceData.load(p.reference / SEC_TICKERS_FILENAME)


def _require_db(p: Paths) -> bool:
    if not p.db.exists():
        print(f"No database at {p.db}. Run `torus init` first.", file=sys.stderr)
        return False
    return True


def _cmd_init(args: argparse.Namespace) -> int:
    p = paths().ensure()
    with SQLiteBackend.open(p.db) as store:
        version = store.schema_version()
    print(f"Torus home: {p.home}")
    print(f"Database:   {p.db} (schema v{version})")
    if args.refresh_reference:
        contact = args.contact or os.environ.get(ENV_CONTACT, "")
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


CONFIG_KEYS = {"sec_contact": "contact email sent to the SEC in the User-Agent"}


def _open_store(p: Paths) -> SQLiteBackend:
    return SQLiteBackend.open(p.ensure().db)


def _cmd_watch_run(args: argparse.Namespace) -> int:
    from torus.watchers import run_watchers

    p = paths()
    with _open_store(p) as store:
        report = run_watchers(
            store, reference=_reference(p), edgar=not args.no_edgar, rss=not args.no_rss
        )
    if args.json:
        print(json.dumps([asdict(s) for s in report.sources], indent=2))
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
        print(f"Total: {report.new_events} new event(s), {report.alerts} alert(s)")
    return 1 if report.errors else 0


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


def _cmd_config(args: argparse.Namespace) -> int:
    if args.key not in CONFIG_KEYS:
        print(f"Unknown key {args.key!r}. Known: {', '.join(CONFIG_KEYS)}", file=sys.stderr)
        return 2
    with _open_store(paths()) as store:
        if args.action == "set":
            if args.key == "sec_contact" and args.value and "@" not in args.value:
                print("sec_contact must be an email address.", file=sys.stderr)
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


def _cmd_doctor(args: argparse.Namespace) -> int:
    from torus.maintenance import Level, doctor, repair_duplicate_links, repair_search_index

    p = paths()
    if args.repair:
        repair_search_index(p)
        closed = repair_duplicate_links(p)
        print(f"Search index rebuilt; {closed} duplicate link(s) closed.")
    checks = doctor(p)
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

    feed = sub.add_parser("feed", help="manage RSS/Atom feeds")
    feed_sub = feed.add_subparsers(dest="feed_command", required=True)
    feed_add = feed_sub.add_parser("add")
    feed_add.add_argument("url")
    feed_add.add_argument("--name")
    feed_add.set_defaults(func=_cmd_feed_add)
    feed_remove = feed_sub.add_parser("remove")
    feed_remove.add_argument("url")
    feed_remove.set_defaults(func=_cmd_feed_remove)

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

    doctor = sub.add_parser("doctor", help="check the installation and database")
    doctor.add_argument(
        "--repair",
        action="store_true",
        help="rebuild the search index and close duplicate links first",
    )
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
