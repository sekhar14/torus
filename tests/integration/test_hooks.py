"""Hook contract tests: payload in on stdin, context out on stdout, never a failure."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import FIXTURES, T0, GraphFactory
from torus.core.clock import ManualClock
from torus.core.models import QueueStatus, ThesisStatus
from torus.core.store import SQLiteBackend
from torus.hooks import EVENTS, handlers, run_hook

HOOK_FIXTURES = FIXTURES / "hooks"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


@pytest.fixture
def env(home: Path) -> dict[str, str]:
    return {"TORUS_HOME": str(home)}


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(T0)


@pytest.fixture
def db(home: Path, env: dict[str, str], clock: ManualClock) -> Iterator[SQLiteBackend]:
    """The same database file the hooks will open."""
    run_hook("session-start", "{}", env=env)  # creates TORUS_HOME and the schema
    store = SQLiteBackend.open(home / "torus.db", clock=clock)
    yield store
    store.close()


def payload(name: str, **overrides: Any) -> str:
    data = json.loads((HOOK_FIXTURES / f"{name}.json").read_text("utf-8"))
    data.update(overrides)
    return json.dumps(data)


def hook(event: str, raw: str, env: dict[str, str], clock: ManualClock, **kw: Any) -> str:
    return run_hook(event, raw, env=env, clock=clock, **kw)


def context_text(output: str, event_name: str) -> str:
    data = json.loads(output)
    assert data["hookSpecificOutput"]["hookEventName"] == event_name
    text: str = data["hookSpecificOutput"]["additionalContext"]
    return text


def _log(home: Path) -> str:
    return (home / "logs" / "hooks.log").read_text("utf-8")


# -- UserPromptSubmit -------------------------------------------------------------


def test_user_prompt_submit_injects_context_for_known_entity(
    db: SQLiteBackend, env: dict[str, str], clock: ManualClock
) -> None:
    g = GraphFactory(db)
    nvda = g.company("NVDA", name="NVIDIA Corp")
    claim = g.fact("TSMC CoWoS capacity constrains NVDA shipments", nvda)
    g.thesis("NVDA supply-bound through 2027", nvda, claim.id)

    out = hook("user-prompt-submit", payload("user_prompt_submit"), env, clock)

    text = context_text(out, "UserPromptSubmit")
    assert "NVDA supply-bound through 2027" in text
    assert "TSMC CoWoS capacity constrains NVDA shipments" in text
    assert len(text) < 10_000  # Claude Code's inline limit
    session = db.start_session("claude-code", "sess-123")
    assert db.session_activity(session.id).entity_ids == (nvda.id,)


@pytest.mark.parametrize(
    "prompt", ["Fix the failing unit test in parser.py", "", "What about $XYZQ?"]
)
def test_user_prompt_submit_silent_without_known_entities(
    db: SQLiteBackend, env: dict[str, str], clock: ManualClock, prompt: str
) -> None:
    GraphFactory(db).company("NVDA")
    out = hook("user-prompt-submit", payload("user_prompt_submit", prompt=prompt), env, clock)
    assert out == ""


def test_user_prompt_submit_silent_when_entity_has_no_research(
    db: SQLiteBackend, env: dict[str, str], clock: ManualClock
) -> None:
    GraphFactory(db).company("NVDA")
    assert hook("user-prompt-submit", payload("user_prompt_submit"), env, clock) == ""


# -- PostToolUse ------------------------------------------------------------------


def test_post_tool_use_logs_webfetch_url_and_search_query(
    db: SQLiteBackend, env: dict[str, str], clock: ManualClock
) -> None:
    assert hook("post-tool-use", payload("post_tool_use_webfetch"), env, clock) == ""
    hook("post-tool-use", payload("post_tool_use_webfetch"), env, clock)  # duplicate
    clock.advance(seconds=1)
    search = json.dumps(
        {
            "session_id": "sess-123",
            "hook_event_name": "PostToolUse",
            "tool_name": "WebSearch",
            "tool_input": {"query": "TSMC CoWoS capacity 2026"},
            "tool_response": {},
        }
    )
    hook("post-tool-use", search, env, clock)
    session = db.start_session("claude-code", "sess-123")
    candidates = db.source_candidates(session.id)
    assert [(c.tool, c.uri, c.query) for c in candidates] == [
        ("WebFetch", "https://investor.nvidia.com/news/q3-fy26", None),
        ("WebSearch", None, "TSMC CoWoS capacity 2026"),
    ]


def test_post_tool_use_ignores_other_tools(
    db: SQLiteBackend, env: dict[str, str], clock: ManualClock
) -> None:
    bash = json.dumps(
        {"session_id": "sess-123", "tool_name": "Bash", "tool_input": {"command": "ls"}}
    )
    hook("post-tool-use", bash, env, clock)
    assert db.counts()["source_candidates"] == 0


@pytest.mark.parametrize(
    "response",
    [
        {"id": "01CLAIM", "text": "x"},
        [{"type": "text", "text": json.dumps({"id": "01CLAIM"})}],
        json.dumps({"id": "01CLAIM"}),
        {"structuredContent": {"id": "01CLAIM"}, "content": []},
    ],
)
def test_post_tool_use_notes_torus_records(
    db: SQLiteBackend, env: dict[str, str], clock: ManualClock, response: Any
) -> None:
    raw = payload("post_tool_use_torus_record_claim", tool_response=response)
    hook("post-tool-use", raw, env, clock)
    session = db.start_session("claude-code", "sess-123")
    assert db.session_activity(session.id).records == {"claim": 1}


# -- Stop / PreCompact / SessionEnd -------------------------------------------------


def _research_session(db: SQLiteBackend, env: dict[str, str], clock: ManualClock) -> str:
    g = GraphFactory(db)
    nvda = g.company("NVDA")
    g.thesis("t", nvda, g.fact("x", nvda).id)
    hook("user-prompt-submit", payload("user_prompt_submit"), env, clock)
    return db.start_session("claude-code", "sess-123").id


def test_stop_enqueues_research_session_once(
    db: SQLiteBackend, env: dict[str, str], clock: ManualClock
) -> None:
    session_id = _research_session(db, env, clock)
    hook("stop", payload("stop"), env, clock)
    clock.advance(minutes=5)
    hook("stop", payload("stop"), env, clock)
    [item] = db.queue_items(kind=handlers.DISTILL_KIND)
    assert item.status is QueueStatus.PENDING
    assert item.payload["session_id"] == session_id
    assert item.payload["transcript_path"].endswith("sess-123.jsonl")
    assert item.payload["capture_mode"] == "balanced"
    assert item.updated_at > item.created_at  # refreshed, not duplicated


def test_stop_skips_non_research_session(
    db: SQLiteBackend, env: dict[str, str], clock: ManualClock
) -> None:
    hook("stop", payload("stop"), env, clock)
    assert db.queue_items() == []


@pytest.mark.parametrize(
    ("mode", "agent_recorded", "queued"),
    [
        ("live", True, False),
        ("live", False, True),
        ("balanced", True, True),
        ("curator", True, True),
    ],
)
def test_capture_mode_decides_distillation(
    db: SQLiteBackend,
    env: dict[str, str],
    clock: ManualClock,
    mode: str,
    agent_recorded: bool,
    queued: bool,
) -> None:
    env = {**env, "CLAUDE_PLUGIN_OPTION_CAPTURE_MODE": mode}
    _research_session(db, env, clock)
    if agent_recorded:
        hook("post-tool-use", payload("post_tool_use_torus_record_claim"), env, clock)
    hook("stop", payload("stop"), env, clock)
    assert bool(db.queue_items()) is queued


def test_session_end_enqueues_and_closes_session(
    db: SQLiteBackend, env: dict[str, str], clock: ManualClock
) -> None:
    session_id = _research_session(db, env, clock)
    hook("session-end", payload("session_end"), env, clock)
    assert db.get_session(session_id).ended_at is not None
    assert len(db.queue_items()) == 1


# -- SessionStart -----------------------------------------------------------------


def test_session_start_digest_lists_at_risk_and_pending(
    db: SQLiteBackend, env: dict[str, str], clock: ManualClock
) -> None:
    _research_session(db, env, clock)
    hook("stop", payload("stop"), env, clock)  # queued for sess-123
    thesis = db.theses()[0]
    db.update_thesis(thesis.id, reason="8-K", status=ThesisStatus.AT_RISK, confidence=0.4)

    out = hook("session-start", payload("session_start", session_id="sess-456"), env, clock)
    text = context_text(out, "SessionStart")
    assert "1 thesis(es) marked at risk" in text
    assert thesis.id in text
    assert "1 earlier research session(s) are queued" in text


def test_session_start_silent_on_empty_graph(env: dict[str, str], clock: ManualClock) -> None:
    assert hook("session-start", payload("session_start"), env, clock) == ""


# -- never break the session ------------------------------------------------------


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", '"text"', "{"])
def test_hook_never_fails_on_bad_stdin(
    home: Path, env: dict[str, str], clock: ManualClock, raw: str
) -> None:
    assert hook("user-prompt-submit", raw, env, clock) == ""
    assert "failed" in _log(home)


def test_unknown_event_is_logged_not_raised(
    home: Path, env: dict[str, str], clock: ManualClock
) -> None:
    assert hook("no-such-event", "{}", env, clock) == ""
    assert "unknown hook event" in _log(home)


def test_missing_home_is_created(home: Path, env: dict[str, str], clock: ManualClock) -> None:
    assert not home.exists()
    hook("session-start", payload("session_start"), env, clock)
    assert (home / "torus.db").exists()


def test_unusable_home_returns_nothing(tmp_path: Path, clock: ManualClock) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory")
    assert hook("session-start", "{}", {"TORUS_HOME": str(blocker)}, clock) == ""


def test_locked_database_times_out_quietly(
    db: SQLiteBackend, home: Path, env: dict[str, str], clock: ManualClock
) -> None:
    locker = sqlite3.connect(home / "torus.db", isolation_level=None)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        started = time.perf_counter()
        out = hook("post-tool-use", payload("post_tool_use_webfetch"), env, clock, budget=0.3)
        assert out == ""
        assert time.perf_counter() - started < 2
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    assert "timed out" in _log(home)


def test_hook_timeout_guard(
    home: Path, env: dict[str, str], clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow(ctx: object) -> str:
        time.sleep(2)
        return "too late"

    monkeypatch.setattr(handlers, "user_prompt_submit", slow)
    started = time.perf_counter()
    assert hook("user-prompt-submit", "{}", env, clock, budget=0.2) == ""
    assert time.perf_counter() - started < 1
    assert "timed out" in _log(home)


def test_every_declared_event_runs_cleanly(
    home: Path, env: dict[str, str], clock: ManualClock
) -> None:
    for event in EVENTS:
        assert hook(event, "{}", env, clock) == ""
    log = _log(home)
    assert "failed" not in log and "timed out" not in log


# -- real process -----------------------------------------------------------------


def _run(event: str, stdin: str, home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "torus", "hook", event],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "TORUS_HOME": str(home)},
        timeout=60,
        check=False,
    )


def test_cli_hook_prints_context_and_exits_zero(db: SQLiteBackend, home: Path) -> None:
    g = GraphFactory(db)
    nvda = g.company("NVDA")
    g.thesis("NVDA supply-bound", nvda, g.fact("x", nvda).id)
    result = _run("user-prompt-submit", payload("user_prompt_submit"), home)
    assert result.returncode == 0, result.stderr
    assert "NVDA supply-bound" in context_text(result.stdout, "UserPromptSubmit")
    assert result.stderr == ""


def test_cli_hook_bad_input_exits_zero_silently(home: Path) -> None:
    result = _run("user-prompt-submit", "garbage", home)
    assert (result.returncode, result.stdout) == (0, "")


def test_records_owned_by_another_session_are_not_counted(
    db: SQLiteBackend, env: dict[str, str], clock: ManualClock
) -> None:
    """The curator, running inside today's session, writes for an earlier session;
    today's session must not be marked as research because of it."""
    from torus.core.models import ClaimType

    nvda = GraphFactory(db).company("NVDA")
    earlier = db.start_session("claude-code", "sess-earlier")
    ev = db.record_evidence("https://e.com/x")
    claim = db.record_claim("x", ClaimType.FACT, 0.8, [nvda.id], [ev.id], session_id=earlier.id)
    raw = payload("post_tool_use_torus_record_claim", tool_response={"id": claim.id})
    hook("post-tool-use", raw, env, clock)
    today = db.start_session("claude-code", "sess-123")
    assert db.session_activity(today.id).records == {}
    assert db.get_claim(claim.id).session_id == earlier.id
    hook("stop", payload("stop"), env, clock)
    assert db.queue_items() == []


def test_session_start_lists_open_alerts(
    db: SQLiteBackend, env: dict[str, str], clock: ManualClock
) -> None:
    from torus.core.impact import ingest_event
    from torus.core.models import EventKind, RelationType

    g = GraphFactory(db)
    nvda, tsm = g.company("NVDA"), g.company("TSM")
    db.record_relation(tsm.id, RelationType.SUPPLIES, nvda.id, 0.9)
    g.thesis("NVDA supply-bound", nvda)
    [alert] = ingest_event(db, EventKind.FILING, "TSMC 8-K", clock.now(), [tsm.id]).alerts

    text = context_text(hook("session-start", payload("session_start"), env, clock), "SessionStart")
    assert "1 open alert(s)" in text and "/torus:review" in text
    assert f"TSMC 8-K -> NVDA supply-bound (alert {alert.id})" in text
