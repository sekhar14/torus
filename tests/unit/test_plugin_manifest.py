"""Static checks on the Claude Code plugin, standing in for `claude plugin validate`
(which needs the Claude Code CLI) and guarding the contract between the plugin
files and the `torus` command they call."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from torus.config import CaptureMode
from torus.hooks import EVENTS
from torus.protocol import instructions

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "claude-code"

# Hook events this plugin may register (subset of Claude Code's documented events).
CLAUDE_CODE_EVENTS = {
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "SubagentStop",
    "PreCompact",
    "SessionEnd",
    "Notification",
}
EVENT_ARG = {
    "SessionStart": "session-start",
    "UserPromptSubmit": "user-prompt-submit",
    "PostToolUse": "post-tool-use",
    "Stop": "stop",
    "PreCompact": "pre-compact",
    "SessionEnd": "session-end",
}


def _json(path: Path) -> Any:
    return json.loads(path.read_text("utf-8"))


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text("utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, f"{path} has no frontmatter"
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    data: dict[str, Any] = _json(PLUGIN / ".claude-plugin" / "plugin.json")
    return data


def test_manifest_basics(manifest: dict[str, Any]) -> None:
    assert manifest["name"] == "torus"
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", manifest["name"])
    for key in ("version", "description", "author"):
        assert manifest[key], key
    assert manifest["author"]["name"]


def test_capture_mode_option_matches_code(manifest: dict[str, Any]) -> None:
    option = manifest["userConfig"]["capture_mode"]
    assert set(option) <= {"type", "title", "description", "options", "default", "required"}
    assert option["type"] == "string"
    assert set(option["options"]) == {m.value for m in CaptureMode}
    assert option["default"] == CaptureMode.BALANCED.value


def test_hooks_call_known_events() -> None:
    config = _json(PLUGIN / "hooks" / "hooks.json")
    assert set(config) <= {"description", "hooks"}
    registered = config["hooks"]
    assert set(registered) <= CLAUDE_CODE_EVENTS
    assert set(registered) == set(EVENT_ARG)
    for event, matchers in registered.items():
        for matcher in matchers:
            for h in matcher["hooks"]:
                assert h["type"] == "command"
                assert h["command"] == "torus"
                assert h["args"] == ["hook", EVENT_ARG[event]]
                assert h["args"][1] in EVENTS
                assert isinstance(h["timeout"], int) and 0 < h["timeout"] <= 60


def test_prompt_hook_is_synchronous_and_capture_hooks_async() -> None:
    hooks = _json(PLUGIN / "hooks" / "hooks.json")["hooks"]
    assert not hooks["UserPromptSubmit"][0]["hooks"][0].get("async")
    assert hooks["PostToolUse"][0]["hooks"][0]["async"] is True
    matcher = re.compile(hooks["PostToolUse"][0]["matcher"])
    for tool in ("WebFetch", "WebSearch", "mcp__plugin_torus_torus__record_claim"):
        assert matcher.fullmatch(tool), tool
    assert not matcher.fullmatch("Bash")


def test_mcp_server_config() -> None:
    server = _json(PLUGIN / ".mcp.json")["mcpServers"]["torus"]
    assert (server["command"], server["args"]) == ("torus", ["serve"])
    assert server["env"]["TORUS_CAPTURE_MODE"] == "${user_config.capture_mode}"


def test_user_config_references_are_declared(manifest: dict[str, Any]) -> None:
    declared = set(manifest["userConfig"])
    for path in PLUGIN.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".md"}:
            for key in re.findall(r"\$\{user_config\.(\w+)\}", path.read_text("utf-8")):
                assert key in declared, f"{path}: undeclared user_config.{key}"


def test_skills_have_frontmatter() -> None:
    skills = sorted((PLUGIN / "skills").iterdir())
    assert [s.name for s in skills] == [
        "distill",
        "explore",
        "research-protocol",
        "review",
        "status",
    ]
    for skill in skills:
        fields = _frontmatter(skill / "SKILL.md")
        assert fields["name"] == skill.name
        assert len(fields["description"]) > 40


def test_no_files_inside_dot_claude_plugin_except_manifest() -> None:
    assert [p.name for p in (PLUGIN / ".claude-plugin").iterdir()] == ["plugin.json"]


def test_marketplace_points_at_plugin(manifest: dict[str, Any]) -> None:
    market = _json(ROOT / ".claude-plugin" / "marketplace.json")
    assert market["name"] and market["owner"]["name"]
    [entry] = market["plugins"]
    assert entry["name"] == manifest["name"]
    assert entry["source"].startswith("./") and ".." not in entry["source"]
    assert (ROOT / entry["source"] / ".claude-plugin" / "plugin.json").exists()


@pytest.mark.parametrize("mode", list(CaptureMode))
def test_instructions_per_mode(mode: CaptureMode) -> None:
    text = instructions(mode)
    assert "recall" in text and "record_thesis" in text
    assert "record_rejected_hypothesis" in text
    assert f"Capture mode: {mode.value}" in text
    if mode is CaptureMode.LIVE:
        assert "record_evidence" in text
    else:
        assert "curator" in text


def test_curator_agent_runs_on_haiku_without_research_tools() -> None:
    agent = PLUGIN / "agents" / "graph-curator.md"
    fields = _frontmatter(agent)
    assert fields["name"] == "graph-curator"
    assert fields["model"] == "haiku"
    blocked = {t.strip() for t in fields["disallowedTools"].split(",")}
    assert {"Edit", "Write", "Bash", "WebFetch", "WebSearch"} <= blocked
    body = agent.read_text("utf-8")
    for tool in ("next_distillation", "complete_distillation", "record_claim", "update_thesis"):
        assert tool in body
    # Plugin agents ignore these fields; using them would silently do nothing.
    assert not {"hooks", "mcpServers", "permissionMode"} & set(fields)


def test_distill_skill_delegates_to_curator() -> None:
    body = (PLUGIN / "skills" / "distill" / "SKILL.md").read_text("utf-8")
    assert "torus:graph-curator" in body


def test_review_skill_uses_alert_tools() -> None:
    body = (PLUGIN / "skills" / "review" / "SKILL.md").read_text("utf-8")
    for tool in ("list_alerts", "resolve_alert", "recall"):
        assert tool in body


def test_explore_skill_runs_the_cli() -> None:
    body = (PLUGIN / "skills" / "explore" / "SKILL.md").read_text("utf-8")
    assert "torus explore" in body and "--no-open" in body
