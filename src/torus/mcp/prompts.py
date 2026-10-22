"""MCP prompts: the workflow skills exposed for GUI MCP clients (Claude Desktop, Codex).

Each workflow in ``torus.workflows`` with target ``desktop`` becomes an MCP
prompt with the same instructions (rendered without subagents or slash-command
syntax). Clients show them in their "+" / prompts menu.
"""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from torus.workflows import BY_NAME, WORKFLOWS, body, description

PROMPT_NAMES: tuple[str, ...] = tuple(w.prompt_name for w in WORKFLOWS if "desktop" in w.targets)


def _render_prompt(name: str, arg_label: str | None = None, value: str = "") -> str:
    text = body(name, "desktop")
    cleaned = value.strip()
    if arg_label and cleaned:
        return f"{arg_label}: {cleaned}\n\n{text}"
    return text


def register_prompts(server: MCPServer) -> None:
    """Register all desktop workflow prompts on ``server``."""

    def add(name: str) -> tuple[str, str, str]:
        w = BY_NAME[name]
        return w.prompt_name, w.title, description(name, "desktop")

    pname, title, desc = add("status")

    @server.prompt(name=pname, title=title, description=desc)
    def status_prompt(
        company: Annotated[str, Field(description=BY_NAME["status"].arguments[0].description)] = "",
    ) -> str:
        return _render_prompt("status", "Company", company)

    pname, title, desc = add("review")

    @server.prompt(name=pname, title=title, description=desc)
    def review_alerts_prompt() -> str:
        return _render_prompt("review")

    pname, title, desc = add("onboard")

    @server.prompt(name=pname, title=title, description=desc)
    def onboard_prompt(
        tickers: Annotated[
            str, Field(description=BY_NAME["onboard"].arguments[0].description)
        ] = "",
    ) -> str:
        return _render_prompt("onboard", "Companies to onboard", tickers)

    pname, title, desc = add("ingest")

    @server.prompt(name=pname, title=title, description=desc)
    def ingest_prompt(
        url: Annotated[str, Field(description=BY_NAME["ingest"].arguments[0].description)] = "",
    ) -> str:
        return _render_prompt("ingest", "Document or filing", url)

    pname, title, desc = add("whatif")

    @server.prompt(name=pname, title=title, description=desc)
    def whatif_prompt(
        event: Annotated[str, Field(description=BY_NAME["whatif"].arguments[0].description)] = "",
    ) -> str:
        return _render_prompt("whatif", "Hypothetical scenario", event)

    pname, title, desc = add("challenge")

    @server.prompt(name=pname, title=title, description=desc)
    def challenge_prompt(
        thesis: Annotated[
            str, Field(description=BY_NAME["challenge"].arguments[0].description)
        ] = "",
    ) -> str:
        return _render_prompt("challenge", "Thesis to challenge", thesis)

    pname, title, desc = add("audit")

    @server.prompt(name=pname, title=title, description=desc)
    def audit_prompt() -> str:
        return _render_prompt("audit")

    pname, title, desc = add("scorecard")

    @server.prompt(name=pname, title=title, description=desc)
    def scorecard_prompt() -> str:
        return _render_prompt("scorecard")

    pname, title, desc = add("memo")

    @server.prompt(name=pname, title=title, description=desc)
    def memo_prompt(
        thesis: Annotated[str, Field(description=BY_NAME["memo"].arguments[0].description)] = "",
    ) -> str:
        return _render_prompt("memo", "Thesis", thesis)

    pname, title, desc = add("undo")

    @server.prompt(name=pname, title=title, description=desc)
    def undo_prompt() -> str:
        return _render_prompt("undo")

    pname, title, desc = add("explore")

    @server.prompt(name=pname, title=title, description=desc)
    def explore_prompt(
        focus: Annotated[str, Field(description=BY_NAME["explore"].arguments[0].description)] = "",
    ) -> str:
        return _render_prompt("explore", "Focus companies", focus)

    pname, title, desc = add("save-chat")

    @server.prompt(name=pname, title=title, description=desc)
    def save_this_chat_prompt() -> str:
        return _render_prompt("save-chat")
