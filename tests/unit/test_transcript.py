from __future__ import annotations

import json
from pathlib import Path

from conftest import FIXTURES
from torus.core.transcript import digest, read_events

TRANSCRIPT = FIXTURES / "transcripts" / "research_session.jsonl"


def test_reads_main_thread_research_events() -> None:
    events = read_events(TRANSCRIPT)
    kinds = [(e.kind, e.tool) for e in events]
    assert kinds == [
        ("user", None),
        ("tool", "WebSearch"),
        ("result", "WebSearch"),
        ("tool", "WebFetch"),
        ("result", "WebFetch"),
        ("tool", "mcp__plugin_torus_torus__record_claim"),
        ("assistant", None),
    ]


def test_skips_noise_and_private_content() -> None:
    text = digest(TRANSCRIPT)
    for noise in (
        "private reasoning",  # thinking blocks
        "meta noise",  # isMeta records
        "side-chain chatter",  # subagent side chains
        "duplicate request copy",  # harness bookkeeping records
        "Plan shown",  # results of harness-internal tools
        "update_plan",
    ):
        assert noise not in text


def test_digest_keeps_answers_sources_and_recorded_items() -> None:
    text = digest(TRANSCRIPT)
    assert "**User:** Research whether TSMC's CoWoS expansion" in text
    assert "- tool WebFetch https://www.trendforce.com/news/cowos-gap" in text
    assert "result (WebFetch): TSMC monthly CoWoS capacity" in text
    assert "- tool torus:record_claim: CEO says demand far exceeds CoWoS supply" in text
    assert "Amkor is adding outsourced CoWoS-like capacity" in text


def test_budget_trims_tool_results_before_answers(tmp_path: Path) -> None:
    path = tmp_path / "big.jsonl"
    lines = [{"type": "user", "message": {"role": "user", "content": "go"}}]
    for i in range(40):
        lines.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"t{i}",
                            "name": "WebFetch",
                            "input": {"url": f"https://e.com/{i}"},
                        }
                    ]
                },
            }
        )
        lines.append(
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": f"t{i}", "content": "x" * 3000}
                    ]
                },
            }
        )
    lines.append(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "FINAL ANSWER"}]}}
    )
    path.write_text("\n".join(json.dumps(line) for line in lines), "utf-8")

    text = digest(path, max_chars=8_000)
    assert len(text) <= 8_000
    assert "FINAL ANSWER" in text
    assert "https://e.com/39" in text


def test_oldest_turns_dropped_when_answers_alone_overflow(tmp_path: Path) -> None:
    path = tmp_path / "long.jsonl"
    lines = [
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": f"answer {i} " + "y" * 900}]},
        }
        for i in range(30)
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines), "utf-8")
    text = digest(path, max_chars=5_000)
    assert len(text) <= 5_000
    assert text.startswith("[earlier part of the transcript omitted]")
    assert "answer 29" in text and "answer 0 " not in text


def test_missing_or_empty_transcripts(tmp_path: Path) -> None:
    assert digest(None) == "(no transcript recorded for this session)"
    assert digest(tmp_path / "nope.jsonl").startswith("(transcript not found")
    empty = tmp_path / "empty.jsonl"
    empty.write_text('{"type": "attachment"}\n', "utf-8")
    assert digest(empty) == "(transcript has no user or assistant messages)"
