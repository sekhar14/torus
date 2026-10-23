from __future__ import annotations

import base64
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path

import pytest

from conftest import GraphFactory
from torus.core.clock import ManualClock
from torus.core.models import EventKind, Thesis
from torus.core.notify import (
    NOTIFY_SETTING,
    Notification,
    NotifyError,
    notify_pending,
    pending,
)
from torus.core.store import SQLiteBackend
from torus.core.store import migrate as m
from torus.notify import ENV_BODY, ENV_TITLE, WINDOWS_SCRIPT, DesktopSender
from torus.schedule import Result


class FakeSender:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[Notification] = []

    def send(self, notification: Notification) -> None:
        if self.fail:
            raise NotifyError("no desktop")
        self.sent.append(notification)


class FakeRunner:
    def __init__(self, code: int = 0, out: str = "") -> None:
        self.code, self.out = code, out
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, args: Sequence[str], extra_env: Mapping[str, str]) -> Result:
        self.calls.append((list(args), dict(extra_env)))
        return Result(self.code, self.out)


def _alert(
    store: SQLiteBackend, clock: ManualClock, thesis: Thesis, title: str, score: float
) -> str:
    event, _ = store.add_event(EventKind.NEWS, title, clock.now())
    alert, _ = store.upsert_alert(event.id, thesis.id, score, [])
    return alert.id


def _theses(store: SQLiteBackend, n: int) -> list[Thesis]:
    g = GraphFactory(store)
    return [g.thesis(f"Thesis {i}", g.company(f"T{i}")) for i in range(n)]


# -- migration ------------------------------------------------------------------------


def test_0013_marks_existing_alerts_notified(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    all_migrations = m.available()
    monkeypatch.setattr(m, "available", lambda: all_migrations[:12])
    m.migrate(conn)
    conn.execute(
        "INSERT INTO alerts (id, event_id, thesis_id, score, paths_json, status, created_at)"
        " VALUES ('a1', 'e1', 't1', 1.2, '[]', 'open', '2026-01-01T00:00:00.000000Z')"
    )
    monkeypatch.setattr(m, "available", lambda: all_migrations)
    m.migrate(conn)
    row = conn.execute("SELECT notified_at FROM alerts WHERE id = 'a1'").fetchone()
    assert row[0] == "2026-01-01T00:00:00.000000Z"


# -- what to notify -------------------------------------------------------------------


def test_one_notification_per_thesis_with_the_top_event(
    store: SQLiteBackend, clock: ManualClock
) -> None:
    (t,) = _theses(store, 1)
    _alert(store, clock, t, "Minor news", 1.1)
    _alert(store, clock, t, "TSMC 8-K: CoWoS expansion delayed", 1.6)
    _alert(store, clock, t, "Weak item", 0.3)  # below the strong floor
    notes, ids = pending(store, clock.now())
    assert len(ids) == 2
    assert notes == [
        Notification(
            "Torus: strong alert on Thesis 0",
            "TSMC 8-K: CoWoS expansion delayed (and 1 more)",
            t.id,
        )
    ]


def test_at_most_three_notifications(store: SQLiteBackend, clock: ManualClock) -> None:
    theses = _theses(store, 5)
    for i, t in enumerate(theses):
        _alert(store, clock, t, f"Event {i}", 2.0 - i / 10)
    notes, ids = pending(store, clock.now())
    assert len(ids) == 5
    assert [n.thesis_id for n in notes[:2]] == [theses[0].id, theses[1].id]
    assert notes[2].title == "Torus: 3 more theses have new alerts"
    assert notes[2].body.startswith("3 alert(s).")


def test_levels(store: SQLiteBackend, clock: ManualClock) -> None:
    (t,) = _theses(store, 1)
    _alert(store, clock, t, "Moderate", 0.6)
    assert pending(store, clock.now()) == ([], [])  # strong by default
    store.set_setting(NOTIFY_SETTING, "moderate")
    notes, _ = pending(store, clock.now())
    assert notes[0].title == "Torus: moderate alert on Thesis 0"
    store.set_setting(NOTIFY_SETTING, "off")
    assert pending(store, clock.now()) == ([], [])


def test_only_the_last_day_counts(store: SQLiteBackend, clock: ManualClock) -> None:
    (t,) = _theses(store, 1)
    _alert(store, clock, t, "Old", 1.5)
    clock.advance(hours=25)
    assert pending(store, clock.now()) == ([], [])


def test_long_text_is_clipped(store: SQLiteBackend, clock: ManualClock) -> None:
    g = GraphFactory(store)
    t = g.thesis("A " * 80, g.company("NVDA"))
    _alert(store, clock, t, "word " * 100, 1.5)
    (note,), _ = pending(store, clock.now())
    assert len(note.title) <= 90 and note.title.endswith("…")
    assert len(note.body) <= 200


def test_notify_pending_marks_alerts_even_when_sending_fails(
    store: SQLiteBackend, clock: ManualClock
) -> None:
    (t,) = _theses(store, 1)
    alert_id = _alert(store, clock, t, "Event", 1.5)
    result = notify_pending(store, FakeSender(fail=True), clock.now())
    assert (result.alerts, result.sent, result.errors) == (1, 0, ["no desktop"])
    assert store.get_alert(alert_id).notified_at == clock.now()
    sender = FakeSender()
    assert notify_pending(store, sender, clock.now()).alerts == 0
    assert sender.sent == []


def test_watch_run_notifies_once(store: SQLiteBackend, clock: ManualClock) -> None:
    from torus.watchers import run_watchers

    (t,) = _theses(store, 1)
    _alert(store, clock, t, "Event", 1.5)
    sender = FakeSender()
    kwargs = {"clock": clock, "edgar": False, "rss": False, "prices": False}
    report = run_watchers(store, notifier=sender, **kwargs)  # type: ignore[arg-type]
    assert report.notify is not None and report.notify.sent == 1
    report = run_watchers(store, notifier=sender, **kwargs)  # type: ignore[arg-type]
    assert report.notify is not None and report.notify.sent == 0
    assert run_watchers(store, **kwargs).notify is None  # type: ignore[arg-type]


# -- desktop senders ------------------------------------------------------------------

HOSTILE = Notification('Torus: strong alert on "X" $(rm -rf ~)', "-n `id` <b>&</b> 'q'")


def test_windows_text_goes_through_env_not_the_script() -> None:
    runner = FakeRunner()
    DesktopSender(platform="win32", runner=runner).send(HOSTILE)
    ((args, env),) = runner.calls
    assert args[:4] == ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand"]
    assert base64.b64decode(args[4]).decode("utf-16-le") == WINDOWS_SCRIPT
    assert env == {ENV_TITLE: HOSTILE.title, ENV_BODY: HOSTILE.body}
    assert "SecurityElement]::Escape" in WINDOWS_SCRIPT


def test_macos_passes_text_as_arguments() -> None:
    runner = FakeRunner()
    DesktopSender(platform="darwin", runner=runner).send(HOSTILE)
    ((args, env),) = runner.calls
    assert args[0] == "osascript" and args[-2:] == [HOSTILE.title, HOSTILE.body]
    assert all(HOSTILE.body not in a for a in args[:-1]) and env == {}


def test_linux_ends_options_and_finds_the_session_bus(tmp_path: Path) -> None:
    (tmp_path / "bus").touch()
    runner = FakeRunner()
    DesktopSender(platform="linux", runner=runner, env={}, run_dir=tmp_path).send(HOSTILE)
    ((args, env),) = runner.calls
    assert args == ["notify-send", "--app-name=Torus", "--", HOSTILE.title, HOSTILE.body]
    assert env == {"DBUS_SESSION_BUS_ADDRESS": f"unix:path={tmp_path / 'bus'}"}

    runner = FakeRunner()
    session = {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/elsewhere"}
    DesktopSender(platform="linux", runner=runner, env=session, run_dir=tmp_path).send(HOSTILE)
    assert runner.calls[0][1] == {}


def test_a_failed_command_raises() -> None:
    sender = DesktopSender(platform="win32", runner=FakeRunner(1, "Access denied.\r\n"))
    with pytest.raises(NotifyError, match=r"powershell failed: Access denied."):
        sender.send(HOSTILE)


def test_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda tool: None)
    assert DesktopSender(platform="darwin").check() == (False, "osascript not found")
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")
    assert DesktopSender(platform="darwin").check() == (True, "osascript")
    ok, detail = DesktopSender(platform="linux", env={}, run_dir=tmp_path).check()
    assert not ok and "session bus" in detail
    (tmp_path / "bus").touch()
    assert DesktopSender(platform="linux", env={}, run_dir=tmp_path).check() == (
        True,
        "notify-send",
    )


# -- CLI and doctor -------------------------------------------------------------------


def test_cli_notify_test_and_config(
    torus_home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from torus.cli import main

    sent: list[Notification] = []
    monkeypatch.setattr(DesktopSender, "send", lambda self, n: sent.append(n))
    main(["init"])
    assert main(["notify", "test"]) == 0
    assert sent[0].title == "Torus: test notification"

    def broken(self: DesktopSender, n: Notification) -> None:
        raise NotifyError("powershell failed: nope")

    monkeypatch.setattr(DesktopSender, "send", broken)
    assert main(["notify", "test"]) == 1
    assert "nope" in capsys.readouterr().err
    assert main(["config", "set", "notify", "loud"]) == 2
    assert main(["config", "set", "notify", "moderate"]) == 0


def test_doctor_reports_notifications(tmp_path: Path) -> None:
    from torus.config import Paths
    from torus.maintenance import Level, doctor

    p = Paths(tmp_path / "home").ensure()
    configs = {
        "claude_config": tmp_path / "c" / "cd.json",
        "codex_config": tmp_path / "x" / "config.toml",
    }
    missing = DesktopSender(platform="linux", env={}, run_dir=tmp_path)
    check = {c.name: c for c in doctor(p, notifier=missing, **configs)}["notifications"]
    assert check.level is Level.WARN and check.detail.startswith("strong alerts, but")
    with SQLiteBackend.open(p.db) as store:
        store.set_setting(NOTIFY_SETTING, "off")
    check = {c.name: c for c in doctor(p, notifier=missing, **configs)}["notifications"]
    assert check.level is Level.INFO


def test_new_alerts_start_unnotified(store: SQLiteBackend, clock: ManualClock) -> None:
    (t,) = _theses(store, 1)
    alert_id = _alert(store, clock, t, "Event", 1.5)
    assert store.get_alert(alert_id).notified_at is None
    clock.advance(timedelta(minutes=5))
    store.mark_notified([alert_id])
    first = store.get_alert(alert_id).notified_at
    clock.advance(timedelta(minutes=5))
    store.mark_notified([alert_id])  # already marked: unchanged
    assert store.get_alert(alert_id).notified_at == first
