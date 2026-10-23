"""Email a digest through the user's own SMTP server (standard library only).

The password is never stored in the database: it comes from ``TORUS_SMTP_PASSWORD``
or from ``TORUS_HOME/secrets/smtp_password``, a file only the user can read (the
platform schedulers don't see variables set in a shell, hence the file). Port 465
means TLS from the start; any other port must offer STARTTLS, with no plain-text
fallback.
"""

from __future__ import annotations

import contextlib
import os
import smtplib
import ssl
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Protocol

from torus.core.digest import Digest, render_html, to_text

ENV_PASSWORD = "TORUS_SMTP_PASSWORD"
PASSWORD_FILE = Path("secrets") / "smtp_password"
DEFAULT_PORT = 587
TIMEOUT = 20.0


class EmailError(Exception):
    pass


class SMTPLike(Protocol):
    def ehlo(self) -> object: ...
    def has_extn(self, name: str) -> bool: ...
    def starttls(self, *, context: ssl.SSLContext) -> object: ...
    def login(self, user: str, password: str) -> object: ...
    def send_message(self, msg: EmailMessage) -> object: ...
    def quit(self) -> object: ...


SMTPFactory = Callable[[str, int, bool], SMTPLike]


def _connect(host: str, port: int, implicit_tls: bool) -> SMTPLike:
    if implicit_tls:
        return smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=ssl.create_default_context())
    return smtplib.SMTP(host, port, timeout=TIMEOUT)


def password_path(home: Path) -> Path:
    return home / PASSWORD_FILE


def read_password(home: Path, env: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    if env.get(ENV_PASSWORD):
        return env[ENV_PASSWORD]
    path = password_path(home)
    if path.exists():
        return path.read_text("utf-8").strip() or None
    return None


def write_password(
    home: Path,
    password: str,
    platform: str = sys.platform,
    run: Callable[[list[str]], int] | None = None,
) -> Path:
    """Write the password file readable by the user only, and return its path."""
    path = password_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    if platform.startswith("win"):
        path.write_text(password, "utf-8")
        user = os.environ.get("USERNAME", "")
        runner = run or (lambda args: subprocess.run(args, capture_output=True).returncode)
        # Drop inherited permissions; grant the current user alone.
        if not user or runner(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"]):
            raise EmailError(f"couldn't restrict {path} to the current user (icacls failed)")
        return path
    os.chmod(path.parent, stat.S_IRWXU)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(password)
    os.chmod(path, 0o600)
    return path


def build_message(d: Digest, sender: str, to: list[str]) -> EmailMessage:
    alerts = sum(len(g.alerts) for g in d.new_alerts)
    subject = f"Torus digest {d.until:%Y-%m-%d}"
    subject += f": {alerts} new alert(s)" if alerts else ": nothing new" if d.empty else ""
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, sender, ", ".join(to)
    msg["Date"], msg["Message-ID"] = formatdate(localtime=True), make_msgid(domain="torus")
    msg.set_content(to_text(d.markdown))
    msg.add_alternative(render_html(d), subtype="html")
    return msg


def send(
    d: Digest,
    host: str,
    port: int,
    user: str | None,
    password: str | None,
    sender: str,
    to: list[str],
    connect: SMTPFactory = _connect,
) -> None:
    if user and not password:
        raise EmailError(
            f"no SMTP password: set {ENV_PASSWORD} or run `torus digest email-password`"
        )
    msg = build_message(d, sender, to)
    try:
        server = connect(host, port, port == 465)
        try:
            server.ehlo()
            if port != 465:
                if not server.has_extn("starttls"):
                    raise EmailError(f"{host}:{port} doesn't offer STARTTLS; not sending")
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            if user and password:
                server.login(user, password)
            server.send_message(msg)
        finally:
            with contextlib.suppress(smtplib.SMTPException, OSError):
                server.quit()
    except EmailError:
        raise
    except smtplib.SMTPAuthenticationError as exc:
        raise EmailError(f"{host}: login refused for {user} ({exc.smtp_code})") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailError(f"{host}:{port}: {exc}") from exc
