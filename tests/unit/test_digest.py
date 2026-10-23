from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from conftest import T0, GraphFactory
from torus.core.clock import ManualClock
from torus.core.digest import build_digest, parse_since, render_html, save_digest
from torus.core.models import EventKind, Stance, ThesisStatus
from torus.core.scorecard import stance_move
from torus.core.store import SQLiteBackend


def _thesis(store: SQLiteBackend, title: str, horizon: str = "12m") -> str:
    g = GraphFactory(store)
    return store.record_thesis(
        title=title,
        statement=f"{title} (statement)",
        stance=Stance.BULLISH,
        confidence=0.7,
        horizon=horizon,
        subject_entity_id=g.company(title.split()[0]).id,
    ).id


def _alert(store: SQLiteBackend, clock: ManualClock, thesis_id: str, title: str, score: float):
    event, _ = store.add_event(EventKind.NEWS, title, clock.now(), uri="https://e.com/" + title)
    alert, _ = store.upsert_alert(event.id, thesis_id, score, [])
    return alert


def test_parse_since() -> None:
    now = datetime(2026, 10, 23, 12, tzinfo=UTC)
    assert parse_since("12h", now) == now - timedelta(hours=12)
    assert parse_since("1d", now) == now - timedelta(days=1)
    assert parse_since(" 2W ", now) == now - timedelta(weeks=2)
    assert parse_since("2026-10-01", now) == datetime(2026, 10, 1, tzinfo=UTC)
    for bad in ("", "yesterday", "1y", "2026-13-01"):
        with pytest.raises(ValueError, match="use 12h, 1d, 2w or YYYY-MM-DD"):
            parse_since(bad, now)


def test_empty_digest(store: SQLiteBackend, clock: ManualClock) -> None:
    d = build_digest(store, clock.now() - timedelta(days=1), clock.now())
    assert d.empty and "Nothing needs your attention" in d.markdown
    assert "## New alerts" not in d.markdown


def test_digest_sections(store: SQLiteBackend, clock: ManualClock) -> None:
    nvda = _thesis(store, "NVDA supply-bound")
    amd = _thesis(store, "AMD wins share")
    tsm = _thesis(store, "TSM margins hold")
    _thesis(store, "MU due soon", horizon="2026-01-20")  # due in the window
    _thesis(store, "INTC past due", horizon="2025-12-31")
    broken = _thesis(store, "AVGO due but broken", horizon="2026-01-10")
    store.update_thesis(broken, "broke", status=ThesisStatus.BROKEN)
    _alert(store, clock, nvda, "old-open", 1.2)  # before the window, still open

    clock.advance(days=2)
    since = clock.now()
    clock.advance(hours=1)
    _alert(store, clock, nvda, "TSMC 8-K: CoWoS delayed", 1.4)
    _alert(store, clock, nvda, "Minor news", 0.3)
    _alert(store, clock, amd, "AMD 10-Q", 0.6)
    store.update_thesis(amd, "orders slipped", status=ThesisStatus.AT_RISK, confidence=0.45)
    store.update_thesis(tsm, "small tweak", confidence=0.66)  # under the 0.1 step
    _thesis(store, "QCOM new thesis")  # created in the window: activity only
    clock.advance(hours=1)

    d = build_digest(store, since, clock.now())
    assert not d.empty
    assert [g.thesis_title for g in d.new_alerts] == ["NVDA supply-bound", "AMD wins share"]
    assert [a.strength for a in d.new_alerts[0].alerts] == ["strong", "weak"]
    assert d.older_open_alerts == 1
    (change,) = d.thesis_changes
    assert (change.title, change.status_to, change.confidence_to) == (
        "AMD wins share",
        ThesisStatus.AT_RISK,
        0.45,
    )
    assert [due.title for due in d.coming_due] == ["INTC past due", "MU due soon"]
    assert d.coming_due[0].days < 0 <= d.coming_due[1].days
    assert d.activity["thesis_created"] == 1 and d.activity["event_added"] == 3

    md = d.markdown
    for heading in ("## New alerts", "## Thesis changes", "## Coming due", "## Activity"):
        assert heading in md
    assert "- strong (1.40), 2026-01-03: [TSMC 8-K: CoWoS delayed](" in md
    assert "active to at risk, confidence 70% to 45% (orders slipped)" in md
    assert "1 older alert(s) are still open." in md
    assert "days ago)" in md and "(in " in md


def test_html_and_files(store: SQLiteBackend, clock: ManualClock, tmp_path: Path) -> None:
    nvda = _thesis(store, "NVDA supply-bound")
    _alert(store, clock, nvda, "Filing <script>alert(1)</script>", 1.4)
    d = build_digest(store, clock.now() - timedelta(hours=1), clock.now() + timedelta(hours=1))
    page = render_html(d)
    assert "<h2>New alerts</h2>" in page and "<script>alert" not in page
    markdown, html_path = save_digest(d, tmp_path / "digests")
    assert markdown.name == f"digest-{d.until:%Y-%m-%d}.md"
    assert markdown.read_text("utf-8") == d.markdown
    assert html_path.read_text("utf-8") == page


def test_stance_move(store: SQLiteBackend) -> None:
    thesis_id = _thesis(store, "NVDA supply-bound")
    day = T0.date()
    store.upsert_prices("NVDA", [(day, 100.0), (day + timedelta(days=5), 110.0)], "test")
    store.upsert_prices("SPY", [(day, 400.0), (day + timedelta(days=5), 404.0)], "test")
    thesis = store.get_thesis(thesis_id)
    assert stance_move(store, thesis, day, day + timedelta(days=5)) is None  # not tracked
    thesis = store.set_thesis_tracking(thesis_id, "NVDA")
    assert stance_move(store, thesis, day, day + timedelta(days=5)) == pytest.approx(0.09)
    # Before entry counts from entry; no later close means no move.
    assert stance_move(store, thesis, date(2025, 1, 1), day + timedelta(days=5)) == (
        pytest.approx(0.09)
    )
    assert stance_move(store, thesis, day + timedelta(days=5), day + timedelta(days=9)) is None
    bearish_id = store.record_thesis(
        title="NVDA bear",
        statement="bear",
        stance=Stance.BEARISH,
        confidence=0.6,
        horizon="12m",
        subject_entity_id=thesis.subject_entity_id,
    ).id
    bearish = store.set_thesis_tracking(bearish_id, "NVDA")
    assert stance_move(store, bearish, day, day + timedelta(days=5)) == pytest.approx(-0.09)


def test_digest_scorecard_section(store: SQLiteBackend, clock: ManualClock) -> None:
    thesis_id = _thesis(store, "NVDA supply-bound")
    store.set_thesis_tracking(thesis_id, "NVDA")
    day = T0.date()
    store.upsert_prices("NVDA", [(day, 100.0), (day + timedelta(days=3), 95.0)], "test")
    store.upsert_prices("SPY", [(day, 400.0), (day + timedelta(days=3), 400.0)], "test")
    clock.advance(days=3)
    d = build_digest(store, T0, clock.now())
    (move,) = d.moves
    assert (move.symbol, move.benchmark, move.move) == ("NVDA", "SPY", pytest.approx(-0.05))
    assert "(NVDA against SPY): -5.0% in the thesis's direction" in d.markdown


def test_cli_digest(
    torus_home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from torus.cli import main

    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    main(["init"])
    capsys.readouterr()
    assert main(["digest", "--since", "nope"]) == 2
    assert main(["digest", "--open"]) == 0
    out = capsys.readouterr().out
    assert "nothing needs your attention" in out
    files = sorted((torus_home / "digests").iterdir())
    assert [f.suffix for f in files] == [".html", ".md"] and opened == [files[0].as_uri()]
    assert main(["digest", "--json", "--since", "2w"]) == 0
    assert json.loads(capsys.readouterr().out)["empty"] is True
