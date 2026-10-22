"""Tests for unified workflows, harness-aware defaults, ``torus setup``, and MCPB build."""

from __future__ import annotations

import json
import sys
import tomllib
import zipfile
from datetime import date
from pathlib import Path

import pytest

from conftest import GraphFactory
from torus.cli import main
from torus.config import (
    ENV_CAPTURE_MODE,
    ENV_TOOLSET,
    CaptureMode,
    Paths,
    Toolset,
    capture_mode,
    has_curator,
    toolset,
)
from torus.core.audit import audit
from torus.core.clock import ManualClock
from torus.core.scorecard import scorecard
from torus.core.store import SQLiteBackend
from torus.maintenance import Level, doctor
from torus.mcp.server import ANALYST_HIDDEN_TOOLS
from torus.protocol import instructions
from torus.setup import _MANAGED_COMMENT as _MANAGED
from torus.setup import (
    claude_desktop_config_path,
    configure_claude_desktop,
    configure_codex,
    inspect_claude_desktop,
    inspect_codex,
)
from torus.workflows import (
    agent,
    agent_names,
    body,
    for_target,
    render,
    target_for,
    write_claude_code_plugin,
    write_codex_skills,
)

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = ROOT / "plugins" / "claude-code"


def test_claude_code_plugin_matches_workflow_sources_byte_for_byte() -> None:
    for w in for_target("code"):
        skill_file = PLUGIN_DIR / "skills" / w.name / "SKILL.md"
        assert skill_file.exists(), f"Missing {skill_file}"
        assert render(w.name, "code") == skill_file.read_text(encoding="utf-8")

    for agent_name in agent_names():
        agent_file = PLUGIN_DIR / "agents" / f"{agent_name}.md"
        assert agent_file.exists(), f"Missing {agent_file}"
        assert agent(agent_name) == agent_file.read_text(encoding="utf-8")


def test_desktop_and_codex_renders_have_no_template_markers_or_hidden_tools() -> None:
    for target in ("code", "desktop", "codex"):
        for w in for_target(target):
            rendered = render(w.name, target)
            assert "<!-- if " not in rendered
            assert "<!-- else -->" not in rendered
            assert "<!-- end -->" not in rendered
            assert "{{" not in rendered and "}}" not in rendered
            if target in ("desktop", "codex"):
                text = body(w.name, target)
                assert "/torus:" not in text
                for hidden in (
                    "next_mapping",
                    "complete_mapping",
                    "next_distillation",
                    "complete_distillation",
                ):
                    assert hidden not in text, f"{w.name} ({target}) mentions hidden tool {hidden}"
                assert hidden in ANALYST_HIDDEN_TOOLS


def test_write_codex_skills_and_claude_code_plugin(tmp_path: Path) -> None:
    skills_dir = tmp_path / "codex_skills"
    written = write_codex_skills(skills_dir)
    codex_workflows = for_target("codex")
    assert len(written) == len(codex_workflows)
    for w in codex_workflows:
        skill_md = skills_dir / w.codex_name / "SKILL.md"
        assert skill_md.exists()
        content = skill_md.read_text(encoding="utf-8")
        assert content.startswith(f"---\nname: {w.codex_name}\ndescription: ")

    plugin_out = tmp_path / "claude_plugin"
    write_claude_code_plugin(plugin_out)
    assert (plugin_out / "skills" / "onboard" / "SKILL.md").exists()
    assert (plugin_out / "agents" / "company-mapper.md").exists()


def test_harness_aware_capture_mode_toolset_and_instructions() -> None:
    assert has_curator("claude-code")
    assert has_curator("test")
    assert not has_curator("claude-desktop")
    assert not has_curator("codex")
    assert not has_curator("mcp")

    assert capture_mode({}, harness="claude-code") is CaptureMode.BALANCED
    assert capture_mode({}, harness="claude-desktop") is CaptureMode.LIVE
    assert capture_mode({}, harness="codex") is CaptureMode.LIVE
    balanced = {ENV_CAPTURE_MODE: "balanced"}
    assert capture_mode(balanced, harness="claude-desktop") is CaptureMode.BALANCED

    assert toolset({}, harness="claude-code") is Toolset.FULL
    assert toolset({}, harness="mcp") is Toolset.FULL
    assert toolset({}, harness="claude-desktop") is Toolset.ANALYST
    assert toolset({}, harness="codex") is Toolset.ANALYST
    assert toolset({ENV_TOOLSET: "full"}, harness="claude-desktop") is Toolset.FULL
    assert toolset({ENV_TOOLSET: "analyst"}, harness="claude-code") is Toolset.ANALYST

    code_inst = instructions(CaptureMode.BALANCED, harness="claude-code")
    desktop_inst = instructions(CaptureMode.LIVE, harness="claude-desktop")
    assert "Torus recorded" not in code_inst
    assert "Torus recorded" in desktop_inst
    assert "brief" in desktop_inst

    assert target_for("claude-code") == "code"
    assert target_for("codex") == "codex"
    assert target_for("claude-desktop") == "desktop"
    assert target_for("mcp") == "desktop"


def test_claude_desktop_config_paths() -> None:
    # Built from APPDATA with the host's Path, so compare the same way on any OS.
    appdata = r"C:\Users\alice\AppData\Roaming"
    win = claude_desktop_config_path({"APPDATA": appdata}, "win32")
    assert win == Path(appdata) / "Claude" / "claude_desktop_config.json"

    mac = claude_desktop_config_path({}, "darwin")
    assert mac.parts[-3:] == ("Application Support", "Claude", "claude_desktop_config.json")

    linux = claude_desktop_config_path({"XDG_CONFIG_HOME": "/home/alice/.config"}, "linux")
    assert linux == Path("/home/alice/.config/Claude/claude_desktop_config.json")


def test_configure_claude_desktop_preserves_existing_and_backs_up(tmp_path: Path) -> None:
    cfg = tmp_path / "Claude" / "claude_desktop_config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        json.dumps(
            {
                "globalShortcut": "Ctrl+Space",
                "mcpServers": {"other": {"command": "npx", "args": ["other-mcp"]}},
            }
        ),
        encoding="utf-8",
    )

    configure_claude_desktop(
        cfg,
        command=r"C:\Users\alice\.local\bin\torus.exe",
        args=["serve"],
        home=tmp_path / "torus_home",
    )
    assert cfg.with_name("claude_desktop_config.json.bak").exists()
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["globalShortcut"] == "Ctrl+Space"
    assert data["mcpServers"]["other"]["command"] == "npx"
    assert data["mcpServers"]["torus"] == {
        "command": r"C:\Users\alice\.local\bin\torus.exe",
        "args": ["serve"],
        "env": {
            "TORUS_HARNESS": "claude-desktop",
            "TORUS_HOME": str(tmp_path / "torus_home"),
        },
    }

    # Invalid JSON is rejected without overwriting the file.
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        configure_claude_desktop(bad, command="torus", args=["serve"])
    assert bad.read_text(encoding="utf-8") == "{not valid json"


def test_configure_codex_preserves_existing_toml_and_installs_skills(tmp_path: Path) -> None:
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        'model = "gpt-5-codex"\n\n[mcp_servers.other]\ncommand = "other"\nargs = ["serve"]\n',
        encoding="utf-8",
    )

    win_cmd = r"C:\Users\alice\AppData\Local\Programs\Python\Scripts\torus.exe"
    first = configure_codex(
        cfg,
        command=win_cmd,
        args=["serve"],
        home=tmp_path / "torus_home",
    )
    assert first.updated
    assert cfg.with_name("config.toml.bak").exists()
    assert len(first.skills) == len(for_target("codex"))
    assert tomllib.loads(cfg.read_text(encoding="utf-8"))["mcp_servers"]["torus"]["command"] == (
        win_cmd
    )

    # Re-running is idempotent and replaces the torus section cleanly.
    configure_codex(cfg, command=sys.executable, args=["-m", "torus", "serve"])
    raw = cfg.read_text(encoding="utf-8")
    assert raw.count("[mcp_servers.torus]") == 1
    parsed = tomllib.loads(raw)
    assert parsed["model"] == "gpt-5-codex"
    assert parsed["mcp_servers"]["other"]["command"] == "other"
    assert parsed["mcp_servers"]["torus"]["command"] == sys.executable
    assert parsed["mcp_servers"]["torus"]["env"]["TORUS_HARNESS"] == "codex"


def test_configure_codex_leaves_a_hand_written_torus_entry_alone(tmp_path: Path) -> None:
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True)
    own = '[mcp_servers.torus]\ncommand = "/opt/torus/bin/torus"\nargs = ["serve"]\n'
    cfg.write_text(own, encoding="utf-8")

    result = configure_codex(cfg, command=sys.executable, args=["-m", "torus", "serve"])
    assert not result.updated
    assert cfg.read_text(encoding="utf-8") == own
    assert not cfg.with_name("config.toml.bak").exists()
    assert len(result.skills) == len(for_target("codex"))  # skills still installed

    # An inline table under [mcp_servers] can't be merged as text: refuse, don't corrupt it.
    inline = tmp_path / "inline.toml"
    inline.write_text(_MANAGED + '\n[mcp_servers]\ntorus = { command = "x" }\n', encoding="utf-8")
    with pytest.raises(ValueError, match="edit it by hand"):
        configure_codex(inline, command="torus", args=["serve"], install_skills=False)


def test_configure_claude_desktop_keeps_env_the_user_added(tmp_path: Path) -> None:
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "torus": {
                        "command": "torus",
                        "args": ["serve"],
                        "env": {"TORUS_CAPTURE_MODE": "curator"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    configure_claude_desktop(cfg, command=sys.executable, args=["-m", "torus", "serve"])
    configure_claude_desktop(cfg, command=sys.executable, args=["-m", "torus", "serve"])
    entry = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["torus"]
    assert entry["command"] == sys.executable
    assert entry["env"] == {"TORUS_CAPTURE_MODE": "curator", "TORUS_HARNESS": "claude-desktop"}


def test_doctor_checks_claude_desktop_and_codex_configs(tmp_path: Path) -> None:
    p = Paths(tmp_path / "home")
    claude_cfg = tmp_path / "claude" / "claude_desktop_config.json"
    codex_cfg = tmp_path / "codex" / "config.toml"

    # Unconfigured -> Level.INFO
    checks = {c.name: c for c in doctor(p, claude_config=claude_cfg, codex_config=codex_cfg)}
    assert checks["Claude Desktop"].level is Level.INFO
    assert checks["Codex"].level is Level.INFO

    # Configured with valid command (sys.executable) -> Level.OK
    configure_claude_desktop(claude_cfg, command=sys.executable, args=["-m", "torus", "serve"])
    configure_codex(codex_cfg, command=sys.executable, args=["-m", "torus", "serve"])
    checks = {c.name: c for c in doctor(p, claude_config=claude_cfg, codex_config=codex_cfg)}
    assert checks["Claude Desktop"].level is Level.OK
    assert checks["Codex"].level is Level.OK

    # A command that exists but fails to start -> warn
    failing = ["-c", "import sys; sys.exit(3)"]
    configure_claude_desktop(claude_cfg, command=sys.executable, args=failing)
    level, detail = inspect_claude_desktop(claude_cfg)
    assert level == "warn" and "didn't start" in detail
    assert inspect_claude_desktop(claude_cfg, probe=False)[0] == "ok"

    # Configured with non-existent absolute command -> warn
    missing = str(tmp_path / "missing" / "torus")
    configure_claude_desktop(claude_cfg, command=missing, args=["serve"])
    assert inspect_claude_desktop(claude_cfg)[0] == "warn"
    configure_codex(codex_cfg, command=missing, args=["serve"])
    assert inspect_codex(codex_cfg)[0] == "warn"


def test_cli_init_and_setup_persist_contact(
    torus_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    (tmp_path / "codex").mkdir()  # Codex is installed; Claude Desktop isn't

    assert main(["init", "--contact", "analyst@example.com"]) == 0
    with SQLiteBackend.open(torus_home / "torus.db") as store:
        assert store.get_setting("sec_contact") == "analyst@example.com"

    assert (
        main(
            [
                "setup",
                "--contact",
                "pm@example.com",
                "--no-refresh-reference",
                "--no-schedule",
            ]
        )
        == 0
    )
    with SQLiteBackend.open(torus_home / "torus.db") as store:
        assert store.get_setting("sec_contact") == "pm@example.com"
    assert (tmp_path / "codex" / "config.toml").exists()
    assert (tmp_path / "codex" / "skills" / "torus-research" / "SKILL.md").exists()
    assert not claude_desktop_config_path().exists()
    out = capsys.readouterr().out
    assert "Claude Desktop: not found" in out
    assert "/plugin install torus@torus" in out

    # Asking for an app explicitly configures it even when it isn't detected.
    assert main(["setup", "--claude-desktop", "--no-refresh-reference", "--no-schedule"]) == 0
    assert claude_desktop_config_path().exists()


def test_audit_and_scorecard_include_markdown_tables(
    store: SQLiteBackend, clock: ManualClock
) -> None:
    g = GraphFactory(store)
    nvda = g.company("NVDA")
    claim = g.fact("Single source fact", nvda)
    thesis = g.thesis("NVDA thesis", nvda, claim.id)
    store.set_thesis_tracking(thesis.id, "NVDA", 100.0, clock.now(), "SPY")
    store.upsert_prices("NVDA", [(date(2026, 1, 1), 100.0), (date(2026, 1, 2), 110.0)], "test")
    store.upsert_prices("SPY", [(date(2026, 1, 1), 500.0), (date(2026, 1, 2), 505.0)], "test")

    clock.advance(days=100)
    audit_report = audit(store, clock.now())
    assert "| Severity | Kind | Finding | Suggestion |" in audit_report.markdown
    assert "Unchanged for 100 days: NVDA thesis" in audit_report.markdown

    card = scorecard(store, clock.now())
    assert "| Thesis | Ticker | Stance |" in card.markdown
    assert "NVDA thesis" in card.markdown


def test_build_mcpb_creates_valid_bundle(tmp_path: Path) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    from build_mcpb import build_mcpb

    out = build_mcpb(tmp_path / "torus.mcpb")
    assert out.exists()
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        assert {"manifest.json", "pyproject.toml", "README.md", "src/torus/__main__.py"} <= names
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["name"] == "torus"
        assert manifest["server"]["mcp_config"]["env"]["TORUS_HARNESS"] == "claude-desktop"
        assert "sec_contact" in manifest["user_config"]
