"""Background distillation: when a session ends, start the curator in a headless
Claude Code run so queued sessions reach the graph without anyone asking.

Opt-in (setting ``auto_distill = on``), because it runs on the user's
subscription. Guards:

* the child runs with ``TORUS_CURATOR=1``, and every hook does nothing in such a
  session, so the curator's own session is never queued or distilled again
* a lock file keeps it to one curator at a time (stale after an hour)
* only the Torus MCP tools are pre-approved; anything else is denied, not asked
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path

from torus.core.store.base import Backend

ENV_CURATOR = "TORUS_CURATOR"
SETTING = "auto_distill"
LOCK_STALE_SECONDS = 3600
PROMPT = "Process the Torus distillation queue."
ALLOWED_TOOLS = "mcp__plugin_torus_torus__*"
MAX_TURNS = 80


class AutoDistill(StrEnum):
    OFF = "off"  # never mention or start it
    ASK = "ask"  # Claude offers to run /torus:distill when sessions are queued (default)
    ON = "on"  # start the curator in the background when a session ends


def mode(store: Backend) -> AutoDistill:
    value = (store.get_setting(SETTING) or "").strip().lower()
    return AutoDistill(value) if value in AutoDistill._value2member_map_ else AutoDistill.ASK


def is_curator_session(env: Mapping[str, str]) -> bool:
    return env.get(ENV_CURATOR) == "1"


def command(claude: str) -> list[str]:
    return [
        claude,
        "-p",
        "--agent",
        "torus:graph-curator",
        "--model",
        "haiku",
        "--allowedTools",
        ALLOWED_TOOLS,
        "--permission-mode",
        "dontAsk",
        "--max-turns",
        str(MAX_TURNS),
        PROMPT,
    ]


Spawner = Callable[[Sequence[str], Mapping[str, str], Path], None]


def spawn_detached(args: Sequence[str], env: Mapping[str, str], log: Path) -> None:
    """Start ``args`` fully detached from this hook process, output to ``log``."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as out:
        if sys.platform == "win32":
            subprocess.Popen(
                list(args),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                env=dict(env),
                close_fds=True,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            subprocess.Popen(
                list(args),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                env=dict(env),
                close_fds=True,
                start_new_session=True,
            )


def maybe_start(
    store: Backend,
    home: Path,
    env: Mapping[str, str],
    pending: int,
    spawner: Spawner = spawn_detached,
    which: Callable[[str], str | None] | None = None,
    now: Callable[[], float] = time.time,
) -> str:
    """Start the curator if enabled and needed. Returns what happened (for the log)."""
    if mode(store) is not AutoDistill.ON:
        return "auto_distill is not on"
    if pending == 0:
        return "nothing queued"
    if is_curator_session(env):
        return "inside a curator session"
    claude = (which or shutil.which)("claude")
    if claude is None:
        return "claude CLI not found on PATH"
    lock = home / "run" / "curator.lock"
    if lock.exists():
        try:
            started = float(json.loads(lock.read_text("utf-8"))["started"])
        except (ValueError, KeyError, OSError):
            started = 0.0
        if now() - started < LOCK_STALE_SECONDS:
            return "a curator is already running"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"started": now(), "pending": pending}), "utf-8")
    child_env = {**env, ENV_CURATOR: "1", "TORUS_HOME": str(home)}
    spawner(command(claude), child_env, home / "logs" / "curator.log")
    return f"started the curator for {pending} queued session(s)"


def release_lock(home: Path) -> None:
    """Called when the queue is empty, so the next session can start a curator."""
    (home / "run" / "curator.lock").unlink(missing_ok=True)
