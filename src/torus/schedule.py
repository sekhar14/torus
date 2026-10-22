"""Run ``torus watch run`` on a schedule with the platform's own scheduler.

* Windows: a Task Scheduler task (``schtasks``) named "Torus watch"
* macOS:   a launchd agent, ``~/Library/LaunchAgents/dev.torus.watch.plist``
* Linux:   a crontab line tagged ``# torus-watch``

Output goes to ``TORUS_HOME/logs/watch.log``. Commands run through an injectable
runner so tests never touch the real scheduler.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

TASK_NAME = "Torus watch"
LAUNCHD_LABEL = "dev.torus.watch"
CRON_TAG = "# torus-watch"


@dataclass(frozen=True)
class Result:
    code: int
    out: str


Runner = Callable[[Sequence[str], str | None], Result]


def run_command(args: Sequence[str], stdin: str | None = None) -> Result:
    try:
        proc = subprocess.run(
            list(args), input=stdin, capture_output=True, text=True, check=False, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Result(1, str(exc))
    return Result(proc.returncode, (proc.stdout or "") + (proc.stderr or ""))


def torus_command() -> list[str]:
    """How the scheduler should start Torus: the installed script, else this Python."""
    found = shutil.which("torus")
    return [found] if found else [sys.executable, "-m", "torus"]


@dataclass
class Scheduler:
    home: Path
    platform: str = sys.platform
    runner: Runner = run_command
    launch_agents: Path | None = None  # macOS only; default ~/Library/LaunchAgents

    @property
    def log(self) -> Path:
        return self.home / "logs" / "watch.log"

    def _plist(self) -> Path:
        base = self.launch_agents or Path.home() / "Library" / "LaunchAgents"
        return base / f"{LAUNCHD_LABEL}.plist"

    def install(self, every_hours: int = 1) -> str:
        if not 1 <= every_hours <= 24:
            raise ValueError("every_hours must be from 1 to 24")
        command = [*torus_command(), "watch", "run"]
        self.log.parent.mkdir(parents=True, exist_ok=True)
        if self.platform.startswith("win"):
            line = subprocess.list2cmdline(command) + f' >> "{self.log}" 2>&1'
            result = self.runner(
                [
                    "schtasks",
                    "/Create",
                    "/F",
                    "/SC",
                    "HOURLY",
                    "/MO",
                    str(every_hours),
                    "/TN",
                    TASK_NAME,
                    "/TR",
                    f'cmd /c "{line}"',
                ],
                None,
            )
            self._check(result, "schtasks")
            return f"Task Scheduler task '{TASK_NAME}' runs every {every_hours}h"
        if self.platform == "darwin":
            plist = self._plist()
            plist.parent.mkdir(parents=True, exist_ok=True)
            plist.write_text(_plist_xml(command, every_hours * 3600, self.log), "utf-8")
            self.runner(["launchctl", "unload", str(plist)], None)
            self._check(self.runner(["launchctl", "load", str(plist)], None), "launchctl")
            return f"launchd agent {plist} runs every {every_hours}h"
        current = self._crontab()
        minute_hour = "0 *" if every_hours == 1 else f"0 */{every_hours}"
        entry = (
            f"{minute_hour} * * * {shlex.join(command)} >> {shlex.quote(str(self.log))} 2>&1"
            f" {CRON_TAG}"
        )
        lines = [line for line in current.splitlines() if CRON_TAG not in line] + [entry]
        self._check(self.runner(["crontab", "-"], "\n".join(lines) + "\n"), "crontab")
        return f"crontab entry runs every {every_hours}h"

    def remove(self) -> bool:
        if self.platform.startswith("win"):
            return self.runner(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], None).code == 0
        if self.platform == "darwin":
            plist = self._plist()
            if not plist.exists():
                return False
            self.runner(["launchctl", "unload", str(plist)], None)
            plist.unlink()
            return True
        current = self._crontab()
        if CRON_TAG not in current:
            return False
        kept = [line for line in current.splitlines() if CRON_TAG not in line]
        self._check(self.runner(["crontab", "-"], "\n".join(kept) + "\n"), "crontab")
        return True

    def status(self) -> str | None:
        """A description of the installed schedule, or None."""
        if self.platform.startswith("win"):
            result = self.runner(["schtasks", "/Query", "/TN", TASK_NAME], None)
            return f"Task Scheduler task '{TASK_NAME}'" if result.code == 0 else None
        if self.platform == "darwin":
            plist = self._plist()
            return f"launchd agent {plist}" if plist.exists() else None
        for line in self._crontab().splitlines():
            if CRON_TAG in line:
                return f"crontab: {line.replace(CRON_TAG, '').strip()}"
        return None

    def _crontab(self) -> str:
        result = self.runner(["crontab", "-l"], None)
        return result.out if result.code == 0 else ""  # no crontab yet

    @staticmethod
    def _check(result: Result, what: str) -> None:
        if result.code != 0:
            raise RuntimeError(f"{what} failed: {result.out.strip()}")


def _plist_xml(command: Sequence[str], interval: int, log: Path) -> str:
    args = "\n".join(f"    <string>{_xml(a)}</string>" for a in command)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{args}
  </array>
  <key>StartInterval</key>
  <integer>{interval}</integer>
  <key>StandardOutPath</key>
  <string>{_xml(str(log))}</string>
  <key>StandardErrorPath</key>
  <string>{_xml(str(log))}</string>
</dict>
</plist>
"""


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
