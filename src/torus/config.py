"""Locations of Torus data on disk.

Everything lives under TORUS_HOME (default ``~/.torus``):

    torus.db      SQLite graph
    sources/      cached source documents
    reference/    reference data (e.g. SEC ticker list)
    logs/         hook and watcher logs
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

ENV_HOME = "TORUS_HOME"
DB_FILENAME = "torus.db"
SUBDIRS = ("sources", "reference", "logs")


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def db(self) -> Path:
        return self.home / DB_FILENAME

    @property
    def sources(self) -> Path:
        return self.home / "sources"

    @property
    def reference(self) -> Path:
        return self.home / "reference"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    def ensure(self) -> Paths:
        """Create the home directory and its subdirectories if missing."""
        self.home.mkdir(parents=True, exist_ok=True)
        for name in SUBDIRS:
            (self.home / name).mkdir(exist_ok=True)
        return self


def torus_home(env: dict[str, str] | None = None) -> Path:
    """Resolve TORUS_HOME from the environment, falling back to ``~/.torus``."""
    environ = os.environ if env is None else env
    value = environ.get(ENV_HOME, "").strip()
    if value:
        return Path(value).expanduser().resolve()
    return (Path.home() / ".torus").resolve()


def paths(env: dict[str, str] | None = None) -> Paths:
    return Paths(torus_home(env))


class CaptureMode(StrEnum):
    """Who writes research into the graph.

    * live     - the research agent records everything as it goes
    * balanced - the agent records theses, key claims and dead ends; the curator
                 fills in the rest after the session (default)
    * curator  - the agent records only theses and dead ends; the curator extracts
                 everything else from the transcript
    """

    LIVE = "live"
    BALANCED = "balanced"
    CURATOR = "curator"


ENV_CAPTURE_MODE = "TORUS_CAPTURE_MODE"
# Exported by Claude Code to plugin hooks from the plugin's `capture_mode` userConfig.
ENV_PLUGIN_CAPTURE_MODE = "CLAUDE_PLUGIN_OPTION_CAPTURE_MODE"
ENV_HARNESS = "TORUS_HARNESS"
ENV_TOOLSET = "TORUS_TOOLSET"
CURATOR_HARNESSES = frozenset({"claude-code", "test"})
ANALYST_HARNESSES = frozenset({"claude-desktop", "codex"})


class Toolset(StrEnum):
    """Which MCP tools to expose.

    * analyst - the tools a researcher uses in chat (default in Claude Desktop
                and Codex; hides background queue and low-level maintenance tools)
    * full    - every tool, including subagent queue tools (default in Claude Code)
    """

    ANALYST = "analyst"
    FULL = "full"


def has_curator(harness: str) -> bool:
    """Whether ``harness`` runs the end-of-session transcript curator."""
    return harness.strip().lower() in CURATOR_HARNESSES


def capture_mode(env: dict[str, str] | None = None, harness: str = "claude-code") -> CaptureMode:
    environ = os.environ if env is None else env
    for name in (ENV_CAPTURE_MODE, ENV_PLUGIN_CAPTURE_MODE):
        value = environ.get(name, "").strip().lower()
        if value in CaptureMode._value2member_map_:
            return CaptureMode(value)
    active_harness = environ.get(ENV_HARNESS, "").strip().lower() or harness
    return CaptureMode.BALANCED if has_curator(active_harness) else CaptureMode.LIVE


def toolset(env: dict[str, str] | None = None, harness: str = "claude-code") -> Toolset:
    environ = os.environ if env is None else env
    value = environ.get(ENV_TOOLSET, "").strip().lower()
    if value in Toolset._value2member_map_:
        return Toolset(value)
    active_harness = environ.get(ENV_HARNESS, "").strip().lower() or harness.strip().lower()
    return Toolset.ANALYST if active_harness in ANALYST_HARNESSES else Toolset.FULL
