"""Forward-only SQL migrations.

Migrations are ``NNNN_name.sql`` files in ``torus.core.store.migrations``. Each one
runs in its own transaction together with the ``meta.schema_version`` bump, so a
failed migration leaves the database at the previous version.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from importlib import resources

from torus.core.text import name_key

_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def available() -> list[Migration]:
    found = []
    for entry in resources.files("torus.core.store.migrations").iterdir():
        match = _NAME.match(entry.name)
        if match:
            found.append(Migration(int(match.group(1)), entry.name, entry.read_text("utf-8")))
    found.sort(key=lambda m: m.version)
    versions = [m.version for m in found]
    if versions != list(range(1, len(found) + 1)):
        raise RuntimeError(f"migration versions must be contiguous from 1, got {versions}")
    return found


def current_version(conn: sqlite3.Connection) -> int:
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    if not has_meta:
        return 0
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return int(row[0]) if row else 0


def register_functions(conn: sqlite3.Connection) -> None:
    """SQL functions that migrations (and queries) may use."""
    conn.create_function("torus_name_key", 1, name_key, deterministic=True)


def latest_version() -> int:
    migrations = available()
    return migrations[-1].version if migrations else 0


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations. Returns the resulting schema version."""
    register_functions(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    version = current_version(conn)
    for migration in available():
        if migration.version <= version:
            continue
        script = (
            "BEGIN;\n"
            f"{migration.sql}\n"
            "INSERT INTO meta (key, value) VALUES ('schema_version', "
            f"'{migration.version}') "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value;\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        version = migration.version
    return version
