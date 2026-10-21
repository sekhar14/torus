"""Condense a harness transcript into what a curator needs to distil it.

Claude Code writes one JSON object per line. Only ``user`` and ``assistant``
records of the main thread matter here: user prompts, assistant answers, research
tool calls (web fetches and searches, file reads) with excerpts of their results,
and calls to Torus's own tools (so the curator knows what was already recorded).
Thinking blocks, sub-agent side chains, meta records and the harness's internal
bookkeeping records are skipped.

The digest is plain markdown kept under a character budget. Tool-result
excerpts are trimmed first, then the oldest turns, because the assistant's
conclusions (with cited URLs) are what a curator needs most.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_MAX_CHARS = 60_000
MAX_FILE_BYTES = 50_000_000
RESULT_EXCERPT_CHARS = 1_500
ASSISTANT_TEXT_CHARS = 6_000
RESEARCH_TOOLS = {"WebFetch", "WebSearch", "Read"}


@dataclass(frozen=True)
class Event:
    kind: str  # user | assistant | tool | result
    text: str
    tool: str | None = None


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p)
    return ""


def _short_name(tool: str) -> str:
    """mcp__plugin_torus_torus__record_claim -> torus:record_claim"""
    if tool.startswith("mcp__") and "torus" in tool:
        return "torus:" + tool.rsplit("__", 1)[-1]
    return tool


def _tool_summary(name: str, args: dict[str, Any]) -> str | None:
    if name == "WebFetch":
        return f"WebFetch {args.get('url', '')}"
    if name == "WebSearch":
        return f"WebSearch {args.get('query', '')!r}"
    if name == "Read":
        return f"Read {args.get('file_path', '')}"
    if name.startswith("mcp__") and "torus" in name:
        detail = args.get("text") or args.get("title") or args.get("uri") or args.get("reason")
        return f"{_short_name(name)}: {detail}" if detail else _short_name(name)
    return None  # harness/internal tools are noise for a curator


def read_events(path: Path) -> list[Event]:
    """Main-thread events from a Claude Code transcript; unreadable lines are skipped."""
    events: list[Event] = []
    tools_by_id: dict[str, str] = {}
    read = 0
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            read += len(line)
            if read > MAX_FILE_BYTES:
                events.append(Event("user", "[transcript truncated: file too large]"))
                break
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict) or record.get("type") not in ("user", "assistant"):
                continue
            if record.get("isSidechain") or record.get("isMeta"):
                continue
            message = record.get("message") or {}
            content = message.get("content")
            if record["type"] == "user":
                if isinstance(content, str):
                    if content.strip():
                        events.append(Event("user", content.strip()))
                    continue
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and block.get("text", "").strip():
                        events.append(Event("user", block["text"].strip()))
                    elif block.get("type") == "tool_result":
                        tool = tools_by_id.get(str(block.get("tool_use_id")))
                        if tool in RESEARCH_TOOLS:
                            text = _text_of(block.get("content")).strip()
                            if text:
                                events.append(Event("result", text, tool))
            else:
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and block.get("text", "").strip():
                        events.append(Event("assistant", block["text"].strip()))
                    elif block.get("type") == "tool_use":
                        name = str(block.get("name", ""))
                        tools_by_id[str(block.get("id"))] = name
                        args = block.get("input") if isinstance(block.get("input"), dict) else {}
                        summary = _tool_summary(name, args or {})
                        if summary:
                            events.append(Event("tool", summary, name))
    return events


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f" […{len(text) - limit} chars cut]"


def _render(events: list[Event], result_chars: int) -> list[str]:
    lines = []
    for e in events:
        if e.kind == "user":
            lines.append(f"\n**User:** {_clip(e.text, ASSISTANT_TEXT_CHARS)}")
        elif e.kind == "assistant":
            lines.append(f"**Assistant:** {_clip(e.text, ASSISTANT_TEXT_CHARS)}")
        elif e.kind == "tool":
            lines.append(f"- tool {e.text}")
        elif e.kind == "result" and result_chars > 0:
            lines.append(f"  > result ({e.tool}): {_clip(e.text, result_chars)}")
    return lines


def digest(path: str | Path | None, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Markdown digest of a transcript within ``max_chars`` (empty notice if missing)."""
    if not path:
        return "(no transcript recorded for this session)"
    file = Path(path)
    if not file.is_file():
        return f"(transcript not found: {file})"
    events = read_events(file)
    if not events:
        return "(transcript has no user or assistant messages)"
    # Shrink tool-result excerpts first, then drop the oldest turns.
    for result_chars in (RESULT_EXCERPT_CHARS, 600, 200, 0):
        text = "\n".join(_render(events, result_chars)).strip()
        if len(text) <= max_chars:
            return text
    marker = "[earlier part of the transcript omitted]\n"
    return marker + text[-(max_chars - len(marker)) :]
