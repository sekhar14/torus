"""Operational tools: ``torus doctor`` (health check, ``--fix``), ``torus export`` and
``torus import`` (lossless JSON-lines backup and restore of a whole graph)."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, TextIO

from torus import __version__
from torus.config import Paths
from torus.core.clock import SystemClock, to_ts
from torus.core.models import AlertStatus, QueueStatus
from torus.core.reference import SEC_TICKERS_FILENAME, ReferenceData, fetch_sec_tickers
from torus.core.store.migrate import latest_version
from torus.core.store.sqlite import SQLiteBackend
from torus.schedule import Scheduler

EXPORT_FORMAT = 1
REFERENCE_MAX_AGE_DAYS = 30  # new listings and ticker changes make an older list miss companies


class Level(StrEnum):
    OK = "ok"
    INFO = "info"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class Check:
    name: str
    level: Level
    detail: str


# -- doctor -------------------------------------------------------------------------


def doctor(
    p: Paths,
    scheduler: Scheduler | None = None,
    claude_config: Path | None = None,
    codex_config: Path | None = None,
) -> list[Check]:
    from torus.setup import (
        claude_desktop_config_path,
        codex_config_path,
        inspect_claude_desktop,
        inspect_codex,
    )

    checks: list[Check] = []

    def add(name: str, level: Level, detail: str) -> None:
        checks.append(Check(name, level, detail))

    version = sys.version_info
    add(
        "python",
        Level.OK if version >= (3, 11) else Level.FAIL,
        f"{version.major}.{version.minor}.{version.micro}",
    )
    executable = shutil.which("torus")
    add(
        "torus on PATH",
        Level.OK if executable else Level.WARN,
        executable or "not found: Claude Code hooks and the MCP server run `torus`",
    )

    try:
        p.ensure()
        probe = p.home / ".write-test"
        probe.write_text("ok", "utf-8")
        probe.unlink()
        add("TORUS_HOME", Level.OK, str(p.home))
    except OSError as exc:
        add("TORUS_HOME", Level.FAIL, f"{p.home}: {exc}")
        return checks

    try:
        store = SQLiteBackend.open(p.db)
    except sqlite3.Error as exc:
        add("database", Level.FAIL, f"{p.db}: {exc}")
        return checks
    with store:
        conn = store._conn
        schema = store.schema_version()
        add(
            "schema",
            Level.OK if schema == latest_version() else Level.FAIL,
            f"v{schema} (latest v{latest_version()})",
        )
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        add("integrity", Level.OK if integrity == "ok" else Level.FAIL, integrity)
        bad_fts = []
        for table in ("claims_fts", "evidence_fts", "theses_fts", "events_fts"):
            try:
                conn.execute(f"INSERT INTO {table} ({table}) VALUES ('integrity-check')")
            except sqlite3.DatabaseError:
                bad_fts.append(table)
        add(
            "search index",
            Level.FAIL if bad_fts else Level.OK,
            f"corrupt: {', '.join(bad_fts)}; run `torus doctor --repair`" if bad_fts else "ok",
        )
        duplicates = duplicate_links(store)
        add(
            "duplicate links",
            Level.WARN if duplicates else Level.OK,
            f"{len(duplicates)} link(s) recorded more than once; run `torus doctor --repair`"
            if duplicates
            else "none",
        )
        counts = store.counts()
        add(
            "graph",
            Level.INFO,
            f"{counts['entities']} entities, {counts['relations']} relation rows,"
            f" {counts['claims']} claim rows, {counts['theses']} theses, {counts['events']} events",
        )

        reference_file = p.reference / SEC_TICKERS_FILENAME
        reference = ReferenceData.load(reference_file)
        if not len(reference):
            add(
                "SEC reference",
                Level.WARN,
                "missing: run `torus init --refresh-reference --contact EMAIL`",
            )
        else:
            age = int((time.time() - reference_file.stat().st_mtime) // 86400)
            stale = age > REFERENCE_MAX_AGE_DAYS
            add(
                "SEC reference",
                Level.WARN if stale else Level.OK,
                f"{len(reference)} companies, downloaded {age} days ago"
                + ("; refresh with `torus doctor --fix`" if stale else ""),
            )
        targets = [e for e in store.watch_targets() if e.kind.value == "company"]
        contact = store.get_setting("sec_contact") or os.environ.get("TORUS_SEC_CONTACT")
        if targets and not contact:
            add(
                "watchers",
                Level.WARN,
                f"{len(targets)} companies watched but no sec_contact:"
                " `torus config set sec_contact EMAIL`",
            )
        else:
            add(
                "watchers",
                Level.OK,
                f"{len(targets)} companies, {len(store.feeds())} feeds",
            )
        if targets or store.feeds():
            schedule = (scheduler or Scheduler(p.home)).status()
            add(
                "schedule",
                Level.OK if schedule else Level.WARN,
                schedule or "nothing runs the watchers; set it up with `torus schedule install`",
            )

        parked = store.queue_items(statuses=[QueueStatus.PARKED])
        pending = store.queue_items(statuses=[QueueStatus.PENDING])
        add(
            "distillation queue",
            Level.WARN if parked else Level.INFO,
            f"{len(pending)} pending, {len(parked)} parked after repeated failures",
        )
        open_alerts = store.alerts(statuses=[AlertStatus.OPEN])
        add("alerts", Level.INFO, f"{len(open_alerts)} open (review with /torus:review)")

    log = p.logs / "hooks.log"
    if log.exists():
        tail = log.read_text("utf-8", errors="replace").splitlines()[-200:]
        failures = [line for line in tail if " failed after " in line or " timed out " in line]
        add(
            "hooks",
            Level.WARN if failures else Level.OK,
            f"{len(failures)} failures/timeouts in the last {len(tail)} runs"
            + (f"; latest: {failures[-1][:120]}" if failures else ""),
        )
    else:
        add("hooks", Level.INFO, "no hook runs logged yet")

    cd_level, cd_detail = inspect_claude_desktop(claude_config or claude_desktop_config_path())
    add("Claude Desktop", Level(cd_level), cd_detail)

    cx_level, cx_detail = inspect_codex(codex_config or codex_config_path())
    add("Codex", Level(cx_level), cx_detail)
    return checks


def fix(
    p: Paths,
    checks: list[Check],
    contact: str | None = None,
    scheduler: Scheduler | None = None,
    claude_config: Path | None = None,
    codex_config: Path | None = None,
    home: Path | None = None,
    fetch: Callable[[Path, str], ReferenceData] | None = None,
) -> list[str]:
    """Fix what ``doctor`` flagged that Torus can fix by itself.

    Returns one line per fix tried: what was done, or why it was skipped. ``home``
    is written into app configs, as ``torus setup`` does when TORUS_HOME is set.
    """
    from torus.setup import (
        claude_desktop_config_path,
        codex_config_path,
        configure_claude_desktop,
        configure_codex,
    )

    flagged = {c.name for c in checks if c.level in (Level.WARN, Level.FAIL)}
    done: list[str] = []
    if "search index" in flagged:
        repair_search_index(p)
        done.append("search index: rebuilt")
    if "duplicate links" in flagged:
        done.append(f"duplicate links: {repair_duplicate_links(p)} closed")

    with SQLiteBackend.open(p.db) as store:
        stored = store.get_setting("sec_contact")
        if contact and contact != stored:
            store.set_setting("sec_contact", contact)
            done.append(f"sec_contact: set to {contact}")
        contact = contact or stored or os.environ.get("TORUS_SEC_CONTACT")
    needs_email = "skipped, needs an email (pass --contact EMAIL)"
    if "watchers" in flagged and not contact:
        done.append(f"watchers: {needs_email}")
    if "SEC reference" in flagged:
        if not contact:
            done.append(f"SEC reference: {needs_email}")
        else:
            try:
                data = (fetch or fetch_sec_tickers)(p.reference / SEC_TICKERS_FILENAME, contact)
                done.append(f"SEC reference: downloaded {len(data)} companies")
            except (OSError, ValueError) as exc:
                done.append(f"SEC reference: download failed ({exc})")
    if "schedule" in flagged:
        try:
            done.append(f"schedule: {(scheduler or Scheduler(p.home)).install(1)}")
        except (RuntimeError, ValueError) as exc:
            done.append(f"schedule: not installed ({exc})")
    if "Claude Desktop" in flagged:
        path = claude_config or claude_desktop_config_path()
        try:
            configure_claude_desktop(path, home=home)
            done.append(f"Claude Desktop: rewrote the torus entry in {path} (backup kept)")
        except ValueError as exc:
            done.append(f"Claude Desktop: {exc}; fix the file by hand")
    if "Codex" in flagged:
        path = codex_config or codex_config_path()
        try:
            codex = configure_codex(path, home=home)
            done.append(
                f"Codex: rewrote the torus entry and {len(codex.skills)} skills in {path.parent}"
                if codex.updated
                else f"Codex: reinstalled {len(codex.skills)} skills; kept your own"
                " [mcp_servers.torus] entry"
            )
        except ValueError as exc:
            done.append(f"Codex: {exc}; fix the file by hand")
    return done


# Each full-text index: its definition and how to refill it from its source table.
SEARCH_INDEXES = {
    "claims_fts": (
        "claim_id UNINDEXED, text",
        "SELECT id, text FROM claims WHERE superseded_at IS NULL",
    ),
    "evidence_fts": (
        "evidence_id UNINDEXED, title, quote",
        "SELECT id, coalesce(title, ''), coalesce(quote, '') FROM evidence",
    ),
    "theses_fts": (
        "thesis_id UNINDEXED, title, statement",
        "SELECT id, title, statement FROM theses",
    ),
    "events_fts": (
        "event_id UNINDEXED, title, summary",
        "SELECT id, title, coalesce(summary, '') FROM events",
    ),
}


def duplicate_links(store: SQLiteBackend) -> list[list[str]]:
    """Groups of relation ids that are current copies of the same link (from
    databases written before record_relation deduplicated). Newest first."""
    rows = store._conn.execute(
        "SELECT src_id, rel, dst_id, group_concat(id) AS ids FROM ("
        "  SELECT * FROM relations WHERE superseded_at IS NULL ORDER BY recorded_at DESC, id DESC"
        ") GROUP BY src_id, rel, dst_id HAVING count(*) > 1"
    ).fetchall()
    return [str(r["ids"]).split(",") for r in rows]


def repair_duplicate_links(p: Paths) -> int:
    """Close all but the newest copy of each duplicated link (kept in history)."""
    with SQLiteBackend.open(p.db) as store, store.transaction():
        closed = 0
        now = to_ts(SystemClock().now())
        for ids in duplicate_links(store):
            for old in ids[1:]:
                store._conn.execute(
                    "UPDATE relations SET superseded_at = ? WHERE id = ?", (now, old)
                )
                closed += 1
        return closed


def repair_search_index(p: Paths) -> None:
    """Drop and rebuild the full-text indexes from the tables they mirror.

    Dropping (rather than deleting rows) also recovers from a corrupted index.
    The triggers that maintain them live on the source tables and are unaffected.
    """
    with SQLiteBackend.open(p.db) as store, store.transaction():
        conn = store._conn
        for table, (columns, fill) in SEARCH_INDEXES.items():
            conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.execute(f"CREATE VIRTUAL TABLE {table} USING fts5 ({columns})")
            conn.execute(f"INSERT INTO {table} {fill}")


# -- export / import ----------------------------------------------------------------


def _data_tables(conn: sqlite3.Connection) -> list[str]:
    """Real tables in creation order (so parents come before children), without
    full-text indexes (rebuilt by triggers on import) or bookkeeping."""
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY rowid"
    ).fetchall()
    tables = []
    for name, sql in rows:
        if name.startswith("sqlite_") or name == "meta" or "_fts" in name:
            continue
        if (sql or "").upper().startswith("CREATE VIRTUAL"):
            continue
        tables.append(name)
    return tables


def export_graph(store: SQLiteBackend, out: TextIO) -> dict[str, int]:
    """Write every row as JSON lines; returns rows per table."""
    conn = store._conn
    header = {
        "torus_export": EXPORT_FORMAT,
        "torus_version": __version__,
        "schema_version": store.schema_version(),
        "exported_at": to_ts(SystemClock().now()),
    }
    out.write(json.dumps(header) + "\n")
    counts = {}
    for table in _data_tables(conn):
        cursor = conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
        columns = [d[0] for d in cursor.description]
        n = 0
        for row in cursor:
            out.write(json.dumps({"t": table, "r": dict(zip(columns, row, strict=True))}) + "\n")
            n += 1
        counts[table] = n
    return counts


def _records(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    for number, line in enumerate(lines, start=2):
        if line.strip():
            try:
                record = json.loads(line)
            except ValueError as exc:
                raise ValueError(f"line {number}: not JSON ({exc})") from exc
            if not isinstance(record, dict) or "t" not in record or "r" not in record:
                raise ValueError(f"line {number}: not a Torus export row")
            yield record


def import_graph(store: SQLiteBackend, source: TextIO, force: bool = False) -> dict[str, int]:
    """Load an export into this (normally empty) database, all or nothing."""
    conn = store._conn
    lines = iter(source)
    try:
        header = json.loads(next(lines))
    except (StopIteration, ValueError) as exc:
        raise ValueError("not a Torus export (missing header line)") from exc
    if not isinstance(header, dict) or header.get("torus_export") != EXPORT_FORMAT:
        raise ValueError("not a Torus export, or an unsupported export format")
    if int(header.get("schema_version", 0)) > store.schema_version():
        raise ValueError(
            f"export is from schema v{header['schema_version']}, newer than this"
            f" database (v{store.schema_version()}); upgrade Torus first"
        )
    if not force and any(store.counts()[t] for t in ("entities", "claims", "theses", "events")):
        raise ValueError("database is not empty; import into a fresh TORUS_HOME or pass --force")

    tables = set(_data_tables(conn))
    columns = {
        t: {row[1] for row in conn.execute(f"PRAGMA table_info({t})").fetchall()} for t in tables
    }
    counts: dict[str, int] = {}
    conn.execute("PRAGMA foreign_keys = OFF")  # rows arrive table by table
    try:
        with store.transaction():
            for record in _records(lines):
                table, row = record["t"], record["r"]
                if table not in tables:
                    raise ValueError(f"unknown table in export: {table}")
                unknown = set(row) - columns[table]
                if unknown:
                    raise ValueError(f"unknown columns for {table}: {sorted(unknown)}")
                names = list(row)
                conn.execute(
                    f"INSERT OR REPLACE INTO {table} ({', '.join(names)})"
                    f" VALUES ({', '.join('?' * len(names))})",
                    [row[n] for n in names],
                )
                counts[table] = counts.get(table, 0) + 1
            problems = conn.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise ValueError(f"export has dangling references, e.g. {tuple(problems[0])}")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return counts


def default_export_path(p: Paths) -> Path:
    stamp = to_ts(SystemClock().now())[:19].replace(":", "").replace("-", "")
    return p.home / "exports" / f"torus-{stamp}.jsonl"
