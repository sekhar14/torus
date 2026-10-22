"""Background distillation and watcher scheduling (no real processes or schedulers)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from torus.core.store import SQLiteBackend
from torus.hooks import autodistill, run_hook
from torus.hooks.autodistill import AutoDistill
from torus.schedule import CRON_TAG, Result, Scheduler


class Spawns:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str], Path]] = []

    def __call__(self, args: Sequence[str], env: Mapping[str, str], log: Path) -> None:
        self.calls.append((list(args), dict(env), log))


def _start(
    store: SQLiteBackend, home: Path, spawns: Spawns, now: float = 1000.0, **env: str
) -> str:
    return autodistill.maybe_start(
        store, home, env, pending=2, spawner=spawns, which=lambda _: "/bin/claude", now=lambda: now
    )


def test_mode_defaults_to_ask(store: SQLiteBackend) -> None:
    assert autodistill.mode(store) is AutoDistill.ASK
    store.set_setting("auto_distill", "ON")
    assert autodistill.mode(store) is AutoDistill.ON
    store.set_setting("auto_distill", "bogus")
    assert autodistill.mode(store) is AutoDistill.ASK


def test_starts_one_curator_with_guards(store: SQLiteBackend, tmp_path: Path) -> None:
    spawns = Spawns()
    assert _start(store, tmp_path, spawns) == "auto_distill is not on"
    store.set_setting("auto_distill", "on")
    assert autodistill.maybe_start(store, tmp_path, {}, 0, spawner=spawns) == "nothing queued"
    assert _start(store, tmp_path, spawns, TORUS_CURATOR="1") == "inside a curator session"

    assert _start(store, tmp_path, spawns, PATH="x").startswith("started the curator")
    [(args, env, log)] = spawns.calls
    assert args[:4] == ["/bin/claude", "-p", "--agent", "torus:graph-curator"]
    assert "mcp__plugin_torus_torus__*" in args and args[-1] == autodistill.PROMPT
    assert env["TORUS_CURATOR"] == "1" and env["PATH"] == "x"
    assert env["TORUS_HOME"] == str(tmp_path)
    assert log == tmp_path / "logs" / "curator.log"

    assert _start(store, tmp_path, spawns, now=1500.0) == "a curator is already running"
    assert len(_start(store, tmp_path, spawns, now=1000.0 + 3601)) > 0 and len(spawns.calls) == 2
    autodistill.release_lock(tmp_path)
    autodistill.release_lock(tmp_path)  # idempotent
    assert not (tmp_path / "run" / "curator.lock").exists()


def test_missing_claude_cli(store: SQLiteBackend, tmp_path: Path) -> None:
    store.set_setting("auto_distill", "on")
    result = autodistill.maybe_start(store, tmp_path, {}, 1, spawner=Spawns(), which=lambda _: None)
    assert result == "claude CLI not found on PATH"


def test_hooks_do_nothing_in_curator_sessions(torus_home: Path) -> None:
    payload = json.dumps({"session_id": "s", "prompt": "NVDA?"})
    env = {"TORUS_HOME": str(torus_home), "TORUS_CURATOR": "1"}
    assert run_hook("user-prompt-submit", payload, env=env) == ""
    assert not (torus_home / "torus.db").exists()


def test_session_end_hook_starts_curator(torus_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autodistill.shutil, "which", lambda _: "/bin/claude")
    env = {"TORUS_HOME": str(torus_home)}
    run_hook("session-start", json.dumps({"session_id": "s0"}), env=env)
    with SQLiteBackend.open(torus_home / "torus.db") as store:
        store.set_setting("auto_distill", "on")
        store.enqueue("distill_session", "distill:x", {"session_id": "x"})
    spawns = Spawns()
    run_hook("session-end", json.dumps({"session_id": "s1"}), env=env, spawner=spawns)
    [(args, child_env, _)] = spawns.calls
    assert args[0] == "/bin/claude" and child_env["TORUS_CURATOR"] == "1"


# -- scheduling ---------------------------------------------------------------------


class FakeRunner:
    def __init__(self, crontab: str = "", fail: bool = False) -> None:
        self.crontab = crontab
        self.fail = fail
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, args: Sequence[str], stdin: str | None) -> Result:
        self.calls.append((list(args), stdin))
        if self.fail:
            return Result(1, "access denied")
        if list(args) == ["crontab", "-l"]:
            return Result(0, self.crontab) if self.crontab else Result(1, "no crontab")
        if list(args) == ["crontab", "-"]:
            self.crontab = stdin or ""
        if args[:2] == ["schtasks", "/Query"]:
            return Result(
                0 if any(c[0][:2] == ["schtasks", "/Create"] for c in self.calls) else 1, ""
            )
        return Result(0, "")


def test_cron_install_status_remove(tmp_path: Path) -> None:
    runner = FakeRunner(crontab="5 4 * * * backup.sh\n")
    scheduler = Scheduler(tmp_path, platform="linux", runner=runner)
    assert scheduler.status() is None
    scheduler.install(every_hours=2)
    assert "backup.sh" in runner.crontab and CRON_TAG in runner.crontab
    assert "0 */2 * * *" in runner.crontab and "watch run" in runner.crontab
    scheduler.install()  # replaces, never duplicates
    assert runner.crontab.count(CRON_TAG) == 1 and "0 * * * *" in runner.crontab
    assert (scheduler.status() or "").startswith("crontab:")
    assert scheduler.remove() and CRON_TAG not in runner.crontab
    assert "backup.sh" in runner.crontab
    assert not scheduler.remove()


def test_windows_task(tmp_path: Path) -> None:
    runner = FakeRunner()
    scheduler = Scheduler(tmp_path, platform="win32", runner=runner)
    assert scheduler.status() is None
    assert "every 3h" in scheduler.install(every_hours=3)
    create = runner.calls[-1][0]
    assert create[:2] == ["schtasks", "/Create"] and create[create.index("/MO") + 1] == "3"
    assert "watch run" in create[-1] and "watch.log" in create[-1]
    assert scheduler.status() == "Task Scheduler task 'Torus watch'"
    assert scheduler.remove()


def test_launchd_agent(tmp_path: Path) -> None:
    runner = FakeRunner()
    agents = tmp_path / "LaunchAgents"
    scheduler = Scheduler(tmp_path, platform="darwin", runner=runner, launch_agents=agents)
    scheduler.install(every_hours=6)
    plist = (agents / "dev.torus.watch.plist").read_text("utf-8")
    assert "<integer>21600</integer>" in plist and "<string>watch</string>" in plist
    assert scheduler.status() is not None
    assert scheduler.remove() and scheduler.status() is None


def test_install_failure_and_bounds(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path, platform="win32", runner=FakeRunner(fail=True))
    with pytest.raises(RuntimeError, match="access denied"):
        scheduler.install()
    with pytest.raises(ValueError):
        scheduler.install(every_hours=0)
