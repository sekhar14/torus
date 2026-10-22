"""Harness hook entry points: ``torus hook <event>``.

The harness (Claude Code) runs one process per hook event and passes a JSON
payload on stdin. Rules every hook follows:

* never break the user's session: always exit 0, print nothing on failure, and
  log the error to ``TORUS_HOME/logs/hooks.log``
* stay within a time budget: work runs in a worker thread, and if it overruns
  the hook returns with no output (SQLite rolls back anything half-written)
* print context for the model only as a ``hookSpecificOutput`` JSON object
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from torus.config import CaptureMode, Paths, capture_mode, paths
from torus.core.clock import Clock, SystemClock
from torus.core.models import Session
from torus.core.store.sqlite import SQLiteBackend

DEFAULT_HARNESS = "claude-code"
LOG_MAX_BYTES = 1_000_000

# Seconds of work before a hook gives up. The harness has its own (larger)
# timeouts; these keep a prompt from ever waiting on Torus for long.
BUDGETS = {
    "session-start": 2.0,
    "user-prompt-submit": 1.5,
    "post-tool-use": 3.0,
    "stop": 3.0,
    "pre-compact": 3.0,
    "session-end": 1.0,
}


@dataclass
class HookContext:
    event: str
    payload: dict[str, Any]
    store: SQLiteBackend
    clock: Clock
    paths: Paths
    mode: CaptureMode
    harness: str = DEFAULT_HARNESS
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    spawner: Callable[..., None] | None = field(default=None, repr=False)  # tests
    _session: Session | None = field(default=None, repr=False)

    def session(self) -> Session:
        """The Torus session for the harness session in the payload."""
        if self._session is None:
            external = str(self.payload.get("session_id") or "") or None
            transcript = self.payload.get("transcript_path")
            self._session = self.store.start_session(
                self.harness, external, str(transcript) if transcript else None
            )
        return self._session


Handler = Callable[[HookContext], str | None]


def _handlers() -> dict[str, Handler]:
    from torus.hooks import handlers

    return {
        "session-start": handlers.session_start,
        "user-prompt-submit": handlers.user_prompt_submit,
        "post-tool-use": handlers.post_tool_use,
        "stop": handlers.stop,
        "pre-compact": handlers.pre_compact,
        "session-end": handlers.session_end,
    }


EVENTS = tuple(BUDGETS)


def _logger(p: Paths) -> logging.Logger:
    """Logger writing to this TORUS_HOME's hooks.log (re-pointed if the home changes)."""
    log = logging.getLogger("torus.hooks")
    log_file = p.logs / "hooks.log"
    current = [h for h in log.handlers if isinstance(h, logging.FileHandler)]
    if current and current[0].baseFilename == str(log_file.resolve()):
        return log
    for handler in current:
        log.removeHandler(handler)
        handler.close()
    if log_file.exists() and log_file.stat().st_size > LOG_MAX_BYTES:
        log_file.replace(log_file.with_suffix(".log.1"))
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    return log


def run_hook(
    event: str,
    raw_stdin: str,
    env: Mapping[str, str] | None = None,
    clock: Clock | None = None,
    budget: float | None = None,
    spawner: Callable[..., None] | None = None,
) -> str:
    """Run one hook and return what to print on stdout. Never raises.

    Does nothing inside a background curator session (``TORUS_CURATOR=1``): that
    session distils others and must not be captured or queued itself."""
    environ = dict(env) if env is not None else dict(os.environ)
    if environ.get("TORUS_CURATOR") == "1":
        return ""
    started = time.perf_counter()
    try:
        p = paths(dict(env) if env is not None else None).ensure()
    except Exception:  # unusable TORUS_HOME: nothing we can even log to
        return ""
    log = _logger(p)
    result: list[str | None] = []
    failure: list[BaseException] = []

    def work() -> None:
        try:
            payload = json.loads(raw_stdin) if raw_stdin.strip() else {}
            if not isinstance(payload, dict):
                raise ValueError(f"hook payload must be a JSON object, got {type(payload)}")
            handler = _handlers().get(event)
            if handler is None:
                raise ValueError(f"unknown hook event {event!r}")
            with SQLiteBackend.open(p.db, clock=clock) as store:
                ctx = HookContext(
                    event=event,
                    payload=payload,
                    store=store,
                    clock=clock or SystemClock(),
                    paths=p,
                    mode=capture_mode(dict(env) if env is not None else None),
                    harness=(env or {}).get("TORUS_HARNESS", DEFAULT_HARNESS),
                    env=environ,
                    spawner=spawner,
                )
                result.append(handler(ctx))
        except BaseException as exc:
            failure.append(exc)

    # A daemon thread: if it overruns, the process can still exit on time.
    thread = threading.Thread(target=work, name=f"torus-hook-{event}", daemon=True)
    thread.start()
    thread.join(budget if budget is not None else BUDGETS.get(event, 3.0))
    elapsed_ms = (time.perf_counter() - started) * 1000

    if thread.is_alive():
        log.warning("%s timed out after %.0f ms; no output", event, elapsed_ms)
        return ""
    if failure:
        exc = failure[0]
        log.error(
            "%s failed after %.0f ms: %s\n%s",
            event,
            elapsed_ms,
            exc,
            "".join(traceback.format_exception(exc)),
        )
        return ""
    output = result[0] if result else None
    log.info("%s ok in %.0f ms%s", event, elapsed_ms, " (context)" if output else "")
    return output or ""


def context_output(hook_event_name: str, text: str) -> str:
    """JSON that adds ``text`` to the model's context for this event."""
    return json.dumps(
        {"hookSpecificOutput": {"hookEventName": hook_event_name, "additionalContext": text}}
    )
