from __future__ import annotations

import json
import re

from conftest import GraphFactory
from torus.core.clock import ManualClock
from torus.core.impact import ingest_event
from torus.core.models import EventKind, RelationType
from torus.core.store import SQLiteBackend
from torus.explorer import PLACEHOLDER, build_data, render


def _graph(store: SQLiteBackend, graph: GraphFactory, clock: ManualClock) -> dict[str, str]:
    tsm, nvda, amd = graph.company("TSM"), graph.company("NVDA"), graph.company("AMD")
    store.record_relation(tsm.id, RelationType.SUPPLIES, nvda.id, 0.9)
    store.record_relation(
        nvda.id, RelationType.COMPETES_WITH, amd.id, 0.8, source="starter:semis@1"
    )
    claim = graph.fact("CoWoS constrains supply", nvda, tsm)
    thesis = graph.thesis("NVDA supply-bound", nvda, claim.id)
    ingest_event(
        store, EventKind.FILING, "TSMC 6-K", clock.now(), [tsm.id], uri="https://sec.gov/x"
    )
    return {"tsm": tsm.id, "nvda": nvda.id, "amd": amd.id, "thesis": thesis.id}


def _embedded(html: str) -> dict:  # type: ignore[type-arg]
    match = re.search(
        r'<script id="torus-data" type="application/json">(.*?)</script>', html, re.DOTALL
    )
    assert match
    return json.loads(match.group(1))  # type: ignore[no-any-return]


def test_build_data_has_every_node_type_and_details(
    store: SQLiteBackend, graph: GraphFactory, clock: ManualClock
) -> None:
    ids = _graph(store, graph, clock)
    data = build_data(store, clock=clock)
    kinds = {n["kind"] for n in data["nodes"]}
    assert kinds == {"entity", "thesis", "event"}
    assert data["meta"]["counts"] == {"entity": 3, "thesis": 1, "event": 1}
    assert data["meta"]["links"] == 2 and data["meta"]["open_alerts"] == 1
    thesis = data["details"][ids["thesis"]]
    assert thesis["claims"][0]["role"] == "assumption"
    assert thesis["claims"][0]["evidence"][0]["uri"].startswith("https://example.com/")
    assert thesis["alerts"][0]["status"] == "open" and thesis["alerts"][0]["paths"]
    nvda = data["details"][ids["nvda"]]
    assert nvda["claims"][0]["text"] == "CoWoS constrains supply"
    provenance = {e["kind"]: e["provenance"] for e in data["edges"] if e["kind"].isupper()}
    assert provenance == {"SUPPLIES": "agent", "COMPETES_WITH": "starter:semis@1"}


def test_focus_view_limits_to_neighbourhood(
    store: SQLiteBackend, graph: GraphFactory, clock: ManualClock
) -> None:
    ids = _graph(store, graph, clock)
    data = build_data(store, focus=[ids["tsm"]], hops=1, clock=clock)
    labels = {n["label"] for n in data["nodes"] if n["kind"] == "entity"}
    assert labels == {"TSM Inc", "NVDA Inc"}
    assert data["meta"]["focus"] == ["TSM Inc"]
    assert any(n["kind"] == "event" for n in data["nodes"])  # events on focus entities


def test_render_embeds_data_safely(
    store: SQLiteBackend, graph: GraphFactory, clock: ManualClock
) -> None:
    hostile = graph.company("EVIL", name='</script><script>alert("x")</script> & Co')
    graph.fact("<img src=x onerror=alert(1)>", hostile)
    store.record_relation(hostile.id, RelationType.SUPPLIES, graph.company("NVDA").id, 0.5)
    html = render(build_data(store, clock=clock))
    assert PLACEHOLDER not in html
    assert html.count("</script>") == 2  # only the page's own two script elements
    data = _embedded(html)
    assert any(n["label"].startswith("</script>") for n in data["nodes"])
    assert "<img" not in html.split('id="torus-data"', 1)[1].split("</script>", 1)[0]


def test_template_has_no_external_resources() -> None:
    from importlib import resources

    template = resources.files("torus.explorer").joinpath("template.html").read_text("utf-8")
    assert template.count(PLACEHOLDER) == 1
    assert not re.search(r'<(script|link)[^>]+(src|href)="https?://', template)
    assert "innerHTML" not in template  # all text goes through textContent


def test_empty_graph_renders(store: SQLiteBackend, clock: ManualClock) -> None:
    data = build_data(store, clock=clock)
    assert data["nodes"] == [] and data["edges"] == []
    assert "The graph is empty" in render(data)
