from __future__ import annotations

import io
import json
import smtplib
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest

from conftest import GraphFactory
from torus.config import Paths
from torus.core.clock import ManualClock
from torus.core.digest import (
    Digest,
    build_digest,
    digest_due,
    latest_slot,
    to_text,
    truncate_markdown,
)
from torus.core.models import EventKind
from torus.core.store import SQLiteBackend
from torus.deliver import (
    DIGEST,
    EMAIL_TO,
    LAST_ERROR,
    LAST_SENT,
    SMTP_HOST,
    SMTP_USER,
    WEBHOOK,
    WEBHOOK_FORMAT,
    Delivery,
    deliver,
    destinations,
    mail,
    send_scheduled,
    webhook,
)

SLACK = "https://hooks.slack.com/services/T000/B000/SECRETxyzAbCd"


class FakePoster:
    def __init__(self, status: int = 200, raise_: Exception | None = None) -> None:
        self.status, self.raise_ = status, raise_
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> int:
        self.requests.append(request)
        if self.raise_:
            raise self.raise_
        return self.status


class FakeSMTP:
    def __init__(self, starttls: bool = True, refuse_login: bool = False) -> None:
        self.offers_starttls, self.refuse_login = starttls, refuse_login
        self.calls: list[str] = []
        self.sent: list[EmailMessage] = []
        self.opened: tuple[str, int, bool] | None = None

    def __call__(self, host: str, port: int, implicit_tls: bool) -> FakeSMTP:
        self.opened = (host, port, implicit_tls)
        return self

    def ehlo(self) -> None:
        self.calls.append("ehlo")

    def has_extn(self, name: str) -> bool:
        return self.offers_starttls

    def starttls(self, *, context: object) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        if self.refuse_login:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
        self.calls.append(f"login {user}")

    def send_message(self, msg: EmailMessage) -> None:
        self.sent.append(msg)

    def quit(self) -> None:
        self.calls.append("quit")


def _digest(store: SQLiteBackend, clock: ManualClock, score: float = 1.4) -> Digest:
    g = GraphFactory(store)
    thesis = g.thesis("NVDA supply-bound", g.company("NVDA"))
    event, _ = store.add_event(EventKind.NEWS, "TSMC delays CoWoS", clock.now())
    store.upsert_alert(event.id, thesis.id, score, [])
    return build_digest(store, clock.now() - timedelta(hours=1), clock.now())


# -- text and size limits -------------------------------------------------------------


def test_truncate_keeps_whole_sections() -> None:
    md = "# T\n\n## A\n\n" + "a" * 50 + "\n\n## B\n\n" + "b" * 50 + "\n"
    assert truncate_markdown(md, 1000, "(cut)") == md
    cut = truncate_markdown(md, 90, "(cut)")
    assert "## A" in cut and "## B" not in cut and cut.endswith("(cut)") and len(cut) <= 90
    tiny = truncate_markdown(md, 30, "(cut)")
    assert len(tiny) <= 30 and tiny.endswith("(cut)")


def test_to_text() -> None:
    md = (
        "# Title\n\n## New alerts\n\n- strong: [TSMC 8-K](https://e.com/x) *open*\n\n"
        "**NVDA**\n---\n"
    )
    assert to_text(md) == (
        "Title\n\nNew alerts\n\n- strong: TSMC 8-K <https://e.com/x> (open)\n\nNVDA\n"
    )


# -- schedule -------------------------------------------------------------------------


def test_latest_slot() -> None:
    wed_9 = datetime(2026, 10, 21, 9, 30)  # a Wednesday
    assert latest_slot("daily", 8, "mon", wed_9) == datetime(2026, 10, 21, 8)
    assert latest_slot("daily", 10, "mon", wed_9) == datetime(2026, 10, 20, 10)
    assert latest_slot("weekly", 8, "mon", wed_9) == datetime(2026, 10, 19, 8)
    assert latest_slot("weekly", 8, "wed", wed_9) == datetime(2026, 10, 21, 8)
    assert latest_slot("weekly", 10, "wed", wed_9) == datetime(2026, 10, 14, 10)


def test_digest_due(clock: ManualClock) -> None:
    now = clock.now()
    assert not digest_due("off", 8, "mon", None, now)
    assert digest_due("daily", 8, "mon", None, now)  # never sent
    assert digest_due("daily", 8, "mon", now + timedelta(days=3), now)  # clock went back
    assert not digest_due("daily", 8, "mon", now, now)  # just sent
    assert digest_due("daily", 8, "mon", now - timedelta(days=1, minutes=1), now)
    assert digest_due("weekly", 8, "mon", now - timedelta(days=8), now)


# -- webhook --------------------------------------------------------------------------


def test_webhook_urls() -> None:
    assert webhook.check_url(SLACK) is None
    assert webhook.check_url("http://localhost:8080/hook") is None
    assert webhook.check_url("http://example.com/hook") is not None
    assert webhook.check_url("ftp://x") is not None
    assert webhook.mask_url(SLACK) == "https://hooks.slack.com/.../AbCd"
    assert webhook.resolve_format(SLACK, "auto") == "slack"
    assert webhook.resolve_format("https://discord.com/api/webhooks/1/x", "auto") == "discord"
    assert webhook.resolve_format("https://ntfy.sh/my-topic", "auto") == "ntfy"
    assert webhook.resolve_format("https://example.com/h", "auto") == "json"
    assert webhook.resolve_format("https://example.com/h", "ntfy") == "ntfy"


def test_webhook_payloads(store: SQLiteBackend, clock: ManualClock) -> None:
    d = _digest(store, clock)
    slack = webhook.build_request(SLACK, "auto", d)
    assert json.loads(slack.data)["text"].startswith("Torus digest")  # type: ignore[arg-type]
    discord = webhook.build_request("https://discord.com/api/webhooks/1/x", "auto", d)
    assert json.loads(discord.data)["content"].startswith("# Torus digest")  # type: ignore[arg-type]
    ntfy = webhook.build_request("https://ntfy.sh/t", "auto", d)
    assert ntfy.get_header("Markdown") == "yes" and ntfy.get_header("Priority") == "high"
    assert ntfy.get_header("Title") == f"Torus digest {d.until:%Y-%m-%d}"
    raw = webhook.build_request("https://example.com/h", "auto", d)
    assert json.loads(raw.data)["new_alerts"][0]["alerts"][0]["strength"] == "strong"  # type: ignore[arg-type]
    assert all(r.get_method() == "POST" for r in (slack, discord, ntfy, raw))

    long = d.model_copy(update={"markdown": d.markdown + "\n## More\n\n" + "x" * 5000})
    body = json.loads(webhook.build_request(SLACK, "slack", long, "/home/d.html").data)  # type: ignore[arg-type]
    assert len(body["text"]) <= webhook.LIMITS["slack"]
    assert "full digest is at /home/d.html" in body["text"]


def test_webhook_errors_are_masked(store: SQLiteBackend, clock: ManualClock) -> None:
    d = _digest(store, clock)
    webhook.send(SLACK, "auto", d, poster=FakePoster())
    with pytest.raises(webhook.WebhookError, match="HTTP 500") as err:
        webhook.send(SLACK, "auto", d, poster=FakePoster(status=500))
    assert "SECRET" not in str(err.value) and "AbCd" in str(err.value)
    http = urllib.error.HTTPError(SLACK, 404, "Not Found", {}, io.BytesIO(b"no_service"))  # type: ignore[arg-type]
    with pytest.raises(webhook.WebhookError, match="HTTP 404 no_service"):
        webhook.send(SLACK, "auto", d, poster=FakePoster(raise_=http))
    down = urllib.error.URLError("connection refused")
    with pytest.raises(webhook.WebhookError, match="connection refused") as err:
        webhook.send(SLACK, "auto", d, poster=FakePoster(raise_=down))
    assert "SECRET" not in str(err.value)


# -- email ----------------------------------------------------------------------------


def test_email_message(store: SQLiteBackend, clock: ManualClock) -> None:
    d = _digest(store, clock)
    msg = mail.build_message(d, "me@example.com", ["a@example.com", "b@example.com"])
    assert msg["Subject"] == f"Torus digest {d.until:%Y-%m-%d}: 1 new alert(s)"
    assert msg["To"] == "a@example.com, b@example.com"
    types = [part.get_content_type() for part in msg.iter_parts()]
    assert types == ["text/plain", "text/html"]


def test_email_requires_starttls(store: SQLiteBackend, clock: ManualClock) -> None:
    d = _digest(store, clock)
    smtp = FakeSMTP()
    mail.send(d, "smtp.x", 587, "me@x", "pw", "me@x", ["to@x"], smtp)
    assert smtp.opened == ("smtp.x", 587, False)
    assert smtp.calls == ["ehlo", "starttls", "ehlo", "login me@x", "quit"] and smtp.sent

    plain = FakeSMTP(starttls=False)
    with pytest.raises(mail.EmailError, match="doesn't offer STARTTLS"):
        mail.send(d, "smtp.x", 587, "me@x", "pw", "me@x", ["to@x"], plain)
    assert not plain.sent and "login me@x" not in plain.calls

    tls = FakeSMTP()
    mail.send(d, "smtp.x", 465, "me@x", "pw", "me@x", ["to@x"], tls)
    assert tls.opened == ("smtp.x", 465, True) and "starttls" not in tls.calls

    with pytest.raises(mail.EmailError, match="login refused for me@x"):
        mail.send(d, "smtp.x", 587, "me@x", "pw", "me@x", ["to@x"], FakeSMTP(refuse_login=True))
    with pytest.raises(mail.EmailError, match="no SMTP password"):
        mail.send(d, "smtp.x", 587, "me@x", None, "me@x", ["to@x"], FakeSMTP())


def test_password_sources(tmp_path: Path) -> None:
    assert mail.read_password(tmp_path, {}) is None
    path = mail.password_path(tmp_path)
    path.parent.mkdir()
    path.write_text("from-file\n", "utf-8")
    assert mail.read_password(tmp_path, {}) == "from-file"
    assert mail.read_password(tmp_path, {mail.ENV_PASSWORD: "from-env"}) == "from-env"


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX permissions")
def test_password_file_is_private_on_posix(tmp_path: Path) -> None:
    path = mail.write_password(tmp_path, "s3cret", platform="linux")
    assert path.read_text("utf-8") == "s3cret"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_password_file_on_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USERNAME", "sekhar")
    calls: list[list[str]] = []
    path = mail.write_password(tmp_path, "s3cret", "win32", lambda a: calls.append(a) or 0)
    assert calls == [["icacls", str(path), "/inheritance:r", "/grant:r", "sekhar:F"]]
    with pytest.raises(mail.EmailError, match="icacls failed"):
        mail.write_password(tmp_path, "s3cret", "win32", lambda a: 5)


# -- sending to destinations ----------------------------------------------------------


def _configure(store: SQLiteBackend) -> None:
    store.set_setting(WEBHOOK, SLACK)
    store.set_setting(EMAIL_TO, "to@x, other@x")
    store.set_setting(SMTP_HOST, "smtp.x")
    store.set_setting(SMTP_USER, "me@x")


def test_deliver_to_every_destination(
    store: SQLiteBackend, clock: ManualClock, tmp_path: Path
) -> None:
    d = _digest(store, clock)
    assert deliver(store, d, tmp_path, None, Delivery()).skipped is not None
    _configure(store)
    slack = "webhook https://hooks.slack.com/.../AbCd"
    assert destinations(store) == [slack, "email to@x, other@x"]
    smtp = FakeSMTP()
    env = {mail.ENV_PASSWORD: "pw"}
    result = deliver(store, d, tmp_path, None, Delivery(FakePoster(), smtp, env))
    assert result.sent == ["webhook https://hooks.slack.com/.../AbCd", "email to@x, other@x"]
    assert smtp.sent[0]["To"] == "to@x, other@x" and smtp.sent[0]["From"] == "me@x"
    # One failing destination doesn't stop the other.
    result = deliver(store, d, tmp_path, None, Delivery(FakePoster(status=500), FakeSMTP(), env))
    assert result.sent == ["email to@x, other@x"] and result.errors[0].startswith("webhook:")


def test_send_scheduled(store: SQLiteBackend, clock: ManualClock, tmp_path: Path) -> None:
    _configure(store)
    env = {mail.ENV_PASSWORD: "pw"}
    poster = FakePoster()
    delivery = Delivery(poster, FakeSMTP(), env)
    assert send_scheduled(store, tmp_path, clock.now(), delivery) is None  # digest is off

    store.set_setting(DIGEST, "daily")
    result = send_scheduled(store, tmp_path, clock.now(), delivery)
    assert result is not None and result.skipped == "nothing new" and not poster.requests
    assert [f.suffix for f in result.files] == [".md", ".html"]
    assert store.get_setting(LAST_SENT) is not None
    assert send_scheduled(store, tmp_path, clock.now(), delivery) is None  # not due again

    clock.advance(days=1, hours=1)
    _digest(store, clock)
    failing = Delivery(FakePoster(status=500), FakeSMTP(), env)
    result = send_scheduled(store, tmp_path, clock.now(), failing)
    assert result is not None and result.sent == ["email to@x, other@x"]
    assert "HTTP 500" in (store.get_setting(LAST_ERROR) or "")

    clock.advance(days=1, hours=1)
    store.set_setting("digest_send_empty", "on")
    result = send_scheduled(store, tmp_path, clock.now(), delivery)
    assert result is not None and len(result.sent) == 2
    assert store.get_setting(LAST_ERROR) is None  # cleared by a clean send


def test_watch_run_sends_a_due_digest(
    store: SQLiteBackend, clock: ManualClock, tmp_path: Path
) -> None:
    from torus.watchers import run_watchers

    store.set_setting(WEBHOOK, "https://ntfy.sh/t")
    store.set_setting(WEBHOOK_FORMAT, "ntfy")
    store.set_setting(DIGEST, "daily")
    _digest(store, clock)
    poster = FakePoster()
    kwargs = {"clock": clock, "edgar": False, "rss": False, "prices": False, "home": tmp_path}
    report = run_watchers(store, delivery=Delivery(poster), **kwargs)  # type: ignore[arg-type]
    assert report.digest is not None and report.digest.sent == ["webhook https://ntfy.sh/.../t"]
    assert len(poster.requests) == 1
    assert run_watchers(store, delivery=Delivery(poster), **kwargs).digest is None  # type: ignore[arg-type]


# -- export, CLI and doctor -----------------------------------------------------------


def test_export_leaves_out_the_webhook_url(store: SQLiteBackend) -> None:
    from torus.maintenance import export_graph

    _configure(store)
    out = io.StringIO()
    export_graph(store, out)
    assert "SECRET" not in out.getvalue() and "smtp.x" in out.getvalue()


def test_cli_config_and_digest_send(
    torus_home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from torus import deliver as deliver_mod
    from torus.cli import main

    main(["init"])
    capsys.readouterr()
    for key, bad in [
        ("digest", "hourly"),
        ("digest_hour", "25"),
        ("digest_weekday", "monday"),
        ("digest_webhook", "http://example.com/h"),
        ("digest_webhook_format", "teams"),
        ("digest_email_to", "nobody"),
        ("smtp_port", "0"),
    ]:
        assert main(["config", "set", key, bad]) == 2, key
    assert main(["config", "set", "digest_webhook", SLACK]) == 0
    out = capsys.readouterr().out
    assert "SECRET" not in out and "AbCd" in out
    assert main(["config", "get", "digest_webhook"]) == 0
    assert "SECRET" not in capsys.readouterr().out

    poster = FakePoster()
    monkeypatch.setattr(deliver_mod, "Delivery", lambda: Delivery(poster, FakeSMTP(), {}))
    assert main(["digest", "--send"]) == 0
    assert "Sent to webhook https://hooks.slack.com/.../AbCd" in capsys.readouterr().out
    assert main(["digest", "test-send"]) == 0
    assert b"Torus digest test" in poster.requests[-1].data  # type: ignore[operator]
    with SQLiteBackend.open(torus_home / "torus.db") as store:
        assert store.get_setting(LAST_SENT) is not None


def test_cli_email_password(
    torus_home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from torus.cli import main

    main(["init"])
    monkeypatch.setattr("getpass.getpass", lambda prompt: "")
    assert main(["digest", "email-password"]) == 2
    monkeypatch.setattr("getpass.getpass", lambda prompt: "s3cret")
    monkeypatch.setattr(
        mail, "write_password", lambda home, pw: Path(home) / "secrets" / "smtp_password"
    )
    assert main(["digest", "email-password"]) == 0
    assert "readable by you only" in capsys.readouterr().out


def test_doctor_digest_check(tmp_path: Path) -> None:
    from test_automation import FakeRunner

    from torus.maintenance import Level, doctor
    from torus.schedule import Scheduler

    p = Paths(tmp_path / "home").ensure()
    configs = {
        "claude_config": tmp_path / "c" / "cd.json",
        "codex_config": tmp_path / "x" / "config.toml",
        "scheduler": Scheduler(p.home, platform="win32", runner=FakeRunner()),
    }

    def check() -> tuple[Level, str]:
        c = {c.name: c for c in doctor(p, **configs)}["digest"]  # type: ignore[arg-type]
        return c.level, c.detail

    assert check()[0] is Level.INFO
    with SQLiteBackend.open(p.db) as store:
        store.set_setting(DIGEST, "weekly")
    level, detail = check()
    assert level is Level.WARN and "nowhere" in detail
    with SQLiteBackend.open(p.db) as store:
        store.set_setting(WEBHOOK, SLACK)
    level, detail = check()
    assert level is Level.WARN and "nothing runs the watchers" in detail and "SECRET" not in detail
    configs["scheduler"].install(1)  # type: ignore[union-attr]
    assert check()[0] is Level.OK
    with SQLiteBackend.open(p.db) as store:
        store.set_setting(LAST_ERROR, "webhook: HTTP 500")
    level, detail = check()
    assert level is Level.WARN and "HTTP 500" in detail
