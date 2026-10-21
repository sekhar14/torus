"""What each hook event does. See ``torus.hooks`` for the shared rules."""

from __future__ import annotations

import json
from typing import Any

from torus.config import CaptureMode
from torus.core.detect import EntityDetector
from torus.core.models import AlertStatus, QueueStatus, ThesisStatus
from torus.core.recall import recall, render_context
from torus.hooks import HookContext, context_output

DISTILL_KIND = "distill_session"
PROMPT_BUDGET_TOKENS = 1200
SESSION_START_MAX_THESES = 5

# Torus MCP tools whose results are objects the agent recorded, by tool suffix.
RECORDING_TOOLS = {
    "record_claim": "claim",
    "supersede_claim": "claim",
    "record_evidence": "evidence",
    "record_thesis": "thesis",
    "update_thesis": "thesis",
    "record_relation": "relation",
    "record_rejected_hypothesis": "hypothesis",
}


def session_start(ctx: HookContext) -> str | None:
    """Tell the agent about open alerts, theses at risk and research waiting to be
    distilled."""
    session = ctx.session()
    lines = []
    open_alerts = ctx.store.alerts(statuses=[AlertStatus.OPEN])
    if open_alerts:
        lines.append(
            f"- {len(open_alerts)} open alert(s): new events may affect theses."
            " The user can run /torus:review to judge them."
        )
        for alert in open_alerts[:SESSION_START_MAX_THESES]:
            event = ctx.store.get_event(alert.event_id)
            thesis = ctx.store.get_thesis(alert.thesis_id)
            lines.append(f"  - {event.title} -> {thesis.title} (alert {alert.id})")
    at_risk = ctx.store.theses(statuses=[ThesisStatus.AT_RISK])
    if at_risk:
        lines.append(f"- {len(at_risk)} thesis(es) marked at risk:")
        lines += [
            f"  - {t.title} (thesis {t.id}, conf {t.confidence:.2f})"
            for t in at_risk[:SESSION_START_MAX_THESES]
        ]
    pending = [
        item
        for item in ctx.store.queue_items(kind=DISTILL_KIND, statuses=[QueueStatus.PENDING])
        if item.payload.get("session_id") != session.id
    ]
    if pending:
        lines.append(
            f"- {len(pending)} earlier research session(s) are queued for distillation."
            " The user can run /torus:distill to have the curator add them to the graph."
        )
    if not lines:
        return None
    text = "## Torus research memory\n" + "\n".join(lines)
    return context_output("SessionStart", text)


def user_prompt_submit(ctx: HookContext) -> str | None:
    """Inject prior research on entities the prompt mentions (graph entities only)."""
    prompt = str(ctx.payload.get("prompt") or "")
    if not prompt.strip():
        return None
    # Only entities already in the graph can have prior research, so the SEC
    # reference list (slower to load) isn't needed here.
    detected = EntityDetector(ctx.store).detect(prompt)
    entity_ids = detected.entity_ids
    if not entity_ids:
        return None
    session = ctx.session()
    ctx.store.note_session_entities(session.id, entity_ids)
    result = recall(ctx.store, ctx.clock, entity_ids=entity_ids, limit=10)
    if result.is_empty:
        return None
    text = render_context(result, now=ctx.clock.now(), budget_tokens=PROMPT_BUDGET_TOKENS)
    text += "\n(Torus research memory: build on this rather than starting over.)"
    return context_output("UserPromptSubmit", text)


def post_tool_use(ctx: HookContext) -> str | None:
    """Log sources the agent looked at, and objects it recorded through Torus."""
    tool = str(ctx.payload.get("tool_name") or "")
    tool_input = ctx.payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None
    if tool == "WebFetch" and tool_input.get("url"):
        session = ctx.session()
        ctx.store.add_source_candidate(session.id, tool, uri=str(tool_input["url"]))
    elif tool == "WebSearch" and tool_input.get("query"):
        session = ctx.session()
        ctx.store.add_source_candidate(session.id, tool, query=str(tool_input["query"]))
    elif tool.startswith("mcp__") and "torus" in tool:
        object_type = RECORDING_TOOLS.get(tool.rsplit("__", 1)[-1])
        object_id = _result_id(ctx.payload.get("tool_response"))
        if object_type and object_id:
            session_id = ctx.session().id
            # The MCP server can't see the harness session id, so claim the object
            # here - unless it belongs to another session (the curator distilling
            # an earlier session writes on that session's behalf).
            if ctx.store.adopt_into_session(session_id, object_type, object_id) != "other":
                ctx.store.note_session_record(session_id, object_type, object_id)
    return None


def stop(ctx: HookContext) -> str | None:
    """Queue the session for distillation if it was research (Stop, PreCompact)."""
    _maybe_enqueue(ctx)
    return None


def session_end(ctx: HookContext) -> str | None:
    _maybe_enqueue(ctx)
    ctx.store.end_session(ctx.session().id)
    return None


def _maybe_enqueue(ctx: HookContext) -> None:
    session = ctx.session()
    activity = ctx.store.session_activity(session.id)
    if not activity.is_research:
        return
    # In live mode the agent records as it goes; only distil if it recorded nothing.
    if ctx.mode is CaptureMode.LIVE and activity.recorded_total > 0:
        return
    ctx.store.enqueue(
        DISTILL_KIND,
        dedupe_key=f"distill:{session.id}",
        payload={
            "session_id": session.id,
            "harness": session.harness,
            "external_session_id": session.external_session_id,
            "transcript_path": session.transcript_path,
            "entity_ids": list(activity.entity_ids),
            "recorded": activity.records,
            "source_candidates": activity.source_candidates,
            "capture_mode": ctx.mode.value,
            "trigger": ctx.event,
        },
    )


def _result_id(response: Any, depth: int = 0) -> str | None:
    """Find the recorded object's id in an MCP tool response, whatever its shape."""
    if depth > 4 or response is None:
        return None
    if isinstance(response, str):
        try:
            return _result_id(json.loads(response), depth + 1)
        except ValueError:
            return None
    if isinstance(response, dict):
        if isinstance(response.get("id"), str):
            return str(response["id"])
        for key in ("structuredContent", "structured_content", "result", "content", "text"):
            if key in response:
                found = _result_id(response[key], depth + 1)
                if found:
                    return found
        return None
    if isinstance(response, list):
        for item in response:
            found = _result_id(item, depth + 1)
            if found:
                return found
    return None
