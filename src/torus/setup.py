"""Configure Claude Desktop and Codex desktop to run the Torus MCP server."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

from torus.config import ENV_HARNESS, ENV_HOME
from torus.workflows import for_target, write_codex_skills

_TOML_TABLE_RE = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")
_MANAGED_COMMENT = "# Managed by torus setup"
PROBE_TIMEOUT_SECONDS = 30

CLAUDE_CODE_COMMANDS = (
    "/plugin marketplace add sekhar14/torus",
    "/plugin install torus@torus",
)


class CodexSetup(NamedTuple):
    config: Path
    skills: list[Path]
    updated: bool  # False: a torus entry the user wrote was left as it is


def resolve_torus_command() -> tuple[str, list[str]]:
    """Absolute command and args for GUI apps that do not inherit shell PATH.

    Prefers the ``torus`` script installed next to the running Python, so the apps
    run the same install as ``torus setup`` even when another one is first on PATH."""
    script_name = "torus.exe" if sys.platform.startswith("win") else "torus"
    sibling = Path(sys.executable).parent / script_name
    if sibling.exists():
        return str(sibling), ["serve"]
    found = shutil.which("torus")
    if found:
        return str(Path(found).resolve()), ["serve"]
    return str(Path(sys.executable).resolve()), ["-m", "torus", "serve"]


def claude_desktop_config_path(
    env: Mapping[str, str] | None = None,
    platform: str = sys.platform,
) -> Path:
    """Platform-specific path to ``claude_desktop_config.json``."""
    e = os.environ if env is None else env
    if platform.startswith("win"):
        appdata = e.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "Claude" / "claude_desktop_config.json"
    if platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    xdg = e.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "Claude" / "claude_desktop_config.json"


def codex_home(env: Mapping[str, str] | None = None) -> Path:
    """Directory where Codex stores ``config.toml`` and ``skills/``."""
    e = os.environ if env is None else env
    raw = e.get("CODEX_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".codex"


def codex_config_path(env: Mapping[str, str] | None = None) -> Path:
    """Path to ``~/.codex/config.toml`` (or ``$CODEX_HOME/config.toml``)."""
    return codex_home(env) / "config.toml"


def claude_desktop_installed(config_path: Path | None = None) -> bool:
    """Whether Claude Desktop looks installed (its settings folder exists)."""
    return (config_path or claude_desktop_config_path()).parent.is_dir()


def codex_installed(config_path: Path | None = None) -> bool:
    """Whether Codex looks installed (its home folder exists or ``codex`` is on PATH)."""
    folder = config_path.parent if config_path else codex_home()
    return folder.is_dir() or shutil.which("codex") is not None


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_name(path.name + ".bak")
    shutil.copyfile(path, backup)
    return backup


def configure_claude_desktop(
    config_path: Path,
    command: str | None = None,
    args: Sequence[str] | None = None,
    home: Path | None = None,
) -> Path:
    """Add or update ``mcpServers.torus`` in ``claude_desktop_config.json``."""
    resolved_cmd, resolved_args = resolve_torus_command()
    cmd = command if command is not None else resolved_cmd
    cmd_args = list(args) if args is not None else resolved_args

    data: dict[str, Any] = {}
    if config_path.exists():
        raw = config_path.read_text(encoding="utf-8").strip()
        if raw:
            try:
                loaded = json.loads(raw)
            except ValueError as exc:
                raise ValueError(f"{config_path} is not valid JSON: {exc}") from exc
            if not isinstance(loaded, dict):
                raise ValueError(f"{config_path} must contain a JSON object")
            data = loaded
        _backup(config_path)

    servers = data.get("mcpServers")
    if servers is None:
        servers = {}
        data["mcpServers"] = servers
    elif not isinstance(servers, dict):
        raise ValueError(f"{config_path}: mcpServers must be a JSON object")

    # Keep env vars the user added to an earlier entry (e.g. TORUS_CAPTURE_MODE).
    previous = servers.get("torus")
    old_env = previous.get("env") if isinstance(previous, dict) else None
    server_env: dict[str, str] = dict(old_env) if isinstance(old_env, dict) else {}
    server_env[ENV_HARNESS] = "claude-desktop"
    if home is not None:
        server_env[ENV_HOME] = str(home)

    servers["torus"] = {
        "command": cmd,
        "args": cmd_args,
        "env": server_env,
    }

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
    return config_path


def _toml_str(value: str) -> str:
    # JSON string escaping is valid TOML basic string escaping for file paths and args.
    return json.dumps(value)


def _strip_torus_toml_sections(text: str) -> str:
    lines = text.splitlines()
    kept: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped == _MANAGED_COMMENT:
            continue
        match = _TOML_TABLE_RE.match(line)
        if match:
            header = match.group(1).strip()
            skipping = header == "mcp_servers.torus" or header.startswith("mcp_servers.torus.")
        elif stripped.startswith("[["):
            skipping = False
        if not skipping:
            kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept)


def configure_codex(
    config_path: Path,
    command: str | None = None,
    args: Sequence[str] | None = None,
    home: Path | None = None,
    install_skills: bool = True,
) -> CodexSetup:
    """Add or update ``[mcp_servers.torus]`` in Codex ``config.toml`` and install skills.

    A torus entry that ``torus setup`` didn't write is left untouched."""
    resolved_cmd, resolved_args = resolve_torus_command()
    cmd = command if command is not None else resolved_cmd
    cmd_args = list(args) if args is not None else resolved_args

    def skills() -> list[Path]:
        return write_codex_skills(config_path.parent / "skills") if install_skills else []

    existing = ""
    if config_path.exists():
        existing = config_path.read_text(encoding="utf-8")
        parsed: dict[str, Any] = {}
        if existing.strip():
            try:
                parsed = tomllib.loads(existing)
            except tomllib.TOMLDecodeError as exc:
                raise ValueError(f"{config_path} is not valid TOML: {exc}") from exc
        servers = parsed.get("mcp_servers")
        if isinstance(servers, dict) and "torus" in servers and _MANAGED_COMMENT not in existing:
            return CodexSetup(config_path, skills(), updated=False)
        _backup(config_path)

    base = _strip_torus_toml_sections(existing)
    args_toml = ", ".join(_toml_str(a) for a in cmd_args)
    section_lines = [
        _MANAGED_COMMENT,
        "[mcp_servers.torus]",
        f"command = {_toml_str(cmd)}",
        f"args = [{args_toml}]",
        "",
        "[mcp_servers.torus.env]",
        f'{ENV_HARNESS} = "codex"',
    ]
    if home is not None:
        section_lines.append(f"{ENV_HOME} = {_toml_str(str(home))}")

    section = "\n".join(section_lines)
    new_text = f"{base}\n\n{section}\n" if base else f"{section}\n"

    # Verify the merged TOML parses cleanly before writing.
    try:
        tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(
            f"{config_path}: couldn't add [mcp_servers.torus] ({exc}); edit it by hand"
        ) from exc

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(new_text, encoding="utf-8", newline="\n")
    return CodexSetup(config_path, skills(), updated=True)


def _command_exists(cmd: str) -> bool:
    p = Path(cmd)
    if p.is_absolute():
        return p.exists()
    return shutil.which(cmd) is not None


def server_starts(cmd: str, args: Sequence[str]) -> str | None:
    """Run the configured command with ``--help``: ``None`` if it works, else why not."""
    try:
        result = subprocess.run(
            [cmd, *args, "--help"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    if result.returncode != 0:
        lines = (result.stderr or result.stdout).strip().splitlines()
        return lines[-1] if lines else f"exit code {result.returncode}"
    return None


def _check_entry(
    config_path: Path, key: str, entry: dict[str, Any], probe: bool
) -> tuple[str, str] | None:
    """A problem with an app's torus entry, or ``None`` if it looks right."""
    cmd = entry.get("command")
    if not isinstance(cmd, str) or not cmd:
        return ("warn", f"{config_path}: {key}.command is missing; run `torus setup`")
    if not _command_exists(cmd):
        return ("warn", f"{config_path}: command {cmd!r} not found; re-run `torus setup`")
    args = entry.get("args") or []
    if probe and isinstance(args, list):
        problem = server_starts(cmd, [str(a) for a in args])
        if problem:
            return ("warn", f"{config_path}: the server didn't start ({problem})")
    return None


def inspect_claude_desktop(config_path: Path, probe: bool = True) -> tuple[str, str]:
    """Return ``(level, detail)`` for ``torus doctor``."""
    if not config_path.exists():
        return ("info", "not configured (run `torus setup` to add Claude Desktop)")
    try:
        data = json.loads(config_path.read_text(encoding="utf-8") or "{}")
    except ValueError as exc:
        return ("warn", f"{config_path}: invalid JSON ({exc})")
    if not isinstance(data, dict):
        return ("warn", f"{config_path}: top-level JSON is not an object")
    servers = data.get("mcpServers")
    entry = servers.get("torus") if isinstance(servers, dict) else None
    if not isinstance(entry, dict):
        return ("info", f"{config_path} (torus not configured; run `torus setup`)")
    problem = _check_entry(config_path, "mcpServers.torus", entry, probe)
    if problem:
        return problem
    return ("ok", f"{config_path} ({entry['command']})")


def inspect_codex(config_path: Path, probe: bool = True) -> tuple[str, str]:
    """Return ``(level, detail)`` for ``torus doctor``."""
    if not config_path.exists():
        return ("info", "not configured (run `torus setup` to add Codex)")
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        return ("warn", f"{config_path}: invalid TOML ({exc})")
    servers = data.get("mcp_servers")
    entry = servers.get("torus") if isinstance(servers, dict) else None
    if not isinstance(entry, dict):
        return ("info", f"{config_path} (torus not configured; run `torus setup`)")
    problem = _check_entry(config_path, "mcp_servers.torus", entry, probe)
    if problem:
        return problem
    cmd = entry["command"]
    skills_dir = config_path.parent / "skills"
    codex_workflows = for_target("codex")
    installed = sum(1 for w in codex_workflows if (skills_dir / w.codex_name / "SKILL.md").exists())
    if installed < len(codex_workflows):
        return (
            "warn",
            f"{config_path}: {installed}/{len(codex_workflows)} Codex skills installed;"
            " re-run `torus setup`",
        )
    return ("ok", f"{config_path} ({cmd}, {installed} skills)")
