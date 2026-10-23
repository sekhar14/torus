"""Show notifications on the user's desktop with the platform's own tools.

* Windows: a toast through PowerShell and the WinRT notification API
* macOS:   ``osascript`` (``display notification``)
* Linux:   ``notify-send``

Notification text can come from RSS feeds, so it is never spliced into a script: it
reaches PowerShell through environment variables and the other two as separate
arguments. Commands run through an injectable runner so tests never show anything.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from torus.core.notify import Notification, NotifyError
from torus.schedule import Result

EnvRunner = Callable[[Sequence[str], Mapping[str, str]], Result]

ENV_TITLE = "TORUS_NOTIFY_TITLE"
ENV_BODY = "TORUS_NOTIFY_BODY"
# Toasts need a registered app id; PowerShell's own is present on every Windows 10/11.
POWERSHELL_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
WINDOWS_SCRIPT = "\n".join(
    [
        "$ErrorActionPreference = 'Stop'",
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
        " ContentType = WindowsRuntime] > $null",
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument,"
        " ContentType = WindowsRuntime] > $null",
        f"$title = [System.Security.SecurityElement]::Escape($env:{ENV_TITLE})",
        f"$body = [System.Security.SecurityElement]::Escape($env:{ENV_BODY})",
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument",
        "$xml.LoadXml(\"<toast><visual><binding template='ToastGeneric'>"
        '<text>$title</text><text>$body</text></binding></visual></toast>")',
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)",
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        f"'{POWERSHELL_APP_ID}').Show($toast)",
    ]
)
MACOS_SCRIPT = [
    "on run argv",
    "display notification (item 2 of argv) with title (item 1 of argv)",
    "end run",
]


def run_with_env(args: Sequence[str], extra_env: Mapping[str, str]) -> Result:
    try:
        proc = subprocess.run(
            list(args),
            env={**os.environ, **extra_env},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Result(1, str(exc))
    return Result(proc.returncode, (proc.stdout or "") + (proc.stderr or ""))


def _user_run_dir() -> Path:
    return Path(f"/run/user/{getattr(os, 'getuid', lambda: 0)()}")


def _session_bus(env: Mapping[str, str], run_dir: Path) -> str | None:
    """cron jobs have no DBUS_SESSION_BUS_ADDRESS; the user's bus socket still exists."""
    if env.get("DBUS_SESSION_BUS_ADDRESS"):
        return None
    bus = run_dir / "bus"
    return f"unix:path={bus}" if bus.exists() else None


@dataclass
class DesktopSender:
    platform: str = sys.platform
    runner: EnvRunner = run_with_env
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    run_dir: Path | None = None  # Linux: /run/user/<uid>

    def command(self, note: Notification) -> tuple[list[str], dict[str, str]]:
        if self.platform.startswith("win"):
            encoded = base64.b64encode(WINDOWS_SCRIPT.encode("utf-16-le")).decode("ascii")
            args = ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
            return args, {ENV_TITLE: note.title, ENV_BODY: note.body}
        if self.platform == "darwin":
            script = [part for line in MACOS_SCRIPT for part in ("-e", line)]
            return ["osascript", *script, note.title, note.body], {}
        extra: dict[str, str] = {}
        bus = _session_bus(self.env, self.run_dir or _user_run_dir())
        if bus:
            extra["DBUS_SESSION_BUS_ADDRESS"] = bus
        return ["notify-send", "--app-name=Torus", "--", note.title, note.body], extra

    def send(self, notification: Notification) -> None:
        args, extra = self.command(notification)
        result = self.runner(args, extra)
        if result.code != 0:
            detail = " ".join(result.out.split())[:200] or f"exit code {result.code}"
            raise NotifyError(f"{args[0]} failed: {detail}")

    def check(self) -> tuple[bool, str]:
        """Whether this platform's tool is there, for ``torus doctor``."""
        tool = self.command(Notification("", ""))[0][0]
        if shutil.which(tool) is None:
            return False, f"{tool} not found"
        if (
            tool == "notify-send"
            and not self.env.get("DBUS_SESSION_BUS_ADDRESS")
            and _session_bus(self.env, self.run_dir or _user_run_dir()) is None
        ):
            return False, "no desktop session bus; notifications need a logged-in desktop"
        return True, tool
