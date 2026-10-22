"""Event materiality and trigger matching: the two alert-precision signals."""

from __future__ import annotations

import pytest

from torus.core import triggers
from torus.core.materiality import assess, filing_weight


@pytest.mark.parametrize(
    ("form", "items", "value"),
    [
        ("8-K", ("1.03",), 1.0),
        ("8-K", ("2.02", "9.01"), 0.9),
        ("8-K", ("5.07",), 0.1),
        ("8-K", ("9.01",), 0.1),
        ("8-K", ("1.01", "9.01"), 0.8),
        ("8-K", (), 0.6),
        ("10-K", (), 0.6),
        ("10-Q", (), 0.4),
        ("10-Q/A", (), 0.3),
        ("S-1", (), 0.5),
    ],
)
def test_filing_weights(form: str, items: tuple[str, ...], value: float) -> None:
    assert filing_weight(form, items).value == value


def test_manual_events_count_fully() -> None:
    assert assess("manual", "Anything at all").value == 1.0
    assert assess("agent", "TSMC 10-Q: quarterly report").value == 1.0


def test_edgar_payload_and_title_forms() -> None:
    routine = assess("edgar", "TSMC 10-Q: quarterly report", payload={"form": "10-Q"})
    assert (routine.value, routine.reason) == (0.4, "10-Q filing")
    bankrupt = assess("edgar", "X 8-K", payload={"form": "8-K", "items": ["1.03"]})
    assert (bankrupt.value, bankrupt.reason) == (1.0, "8-K item 1.03")
    # No payload: the form is read from the title, and the wording can raise it.
    delayed = assess("edgar", "TSMC 8-K: CoWoS capacity expansion delayed")
    assert delayed.value == 0.7 and delayed.reason == "delay or shortage"


@pytest.mark.parametrize(
    ("title", "value", "reason"),
    [
        ("US tightens export controls on AI chips to China", 0.9, "export controls or sanctions"),
        ("Supplier cuts full-year guidance on weak demand", 0.9, "guidance cut"),
        ("Automaker recalls 200,000 vehicles", 0.9, "product recall"),
        ("Chipmaker to acquire design startup for $2bn", 0.8, "acquisition or merger"),
        ("CEO steps down effective immediately", 0.7, "leadership change"),
        ("3 AI stocks to buy before earnings", 0.2, "listicle or opinion piece"),
        ("Company hosts investor day", 0.5, "news item"),
    ],
)
def test_news_wording(title: str, value: float, reason: str) -> None:
    m = assess("rss:wire", title)
    assert (m.value, m.reason) == (value, reason)


def test_trigger_terms_fold_synonyms_and_drop_names() -> None:
    assert triggers.terms("TSMC CoWoS capacity slips into 2027", exclude=["TSMC"]) == {
        "cowos",
        "capacity",
        "delay",
        "2027",
    }
    assert "delay" in triggers.terms("expansion pushed back")
    assert "restrict" in triggers.terms("new export controls")


def test_trigger_match() -> None:
    trigger = "TSMC CoWoS capacity slips into 2027"
    event = "TSMC delays CoWoS capacity expansion. Packaging ramp pushed back"
    assert triggers.match(event, trigger, exclude=["TSMC"]) == 0.75
    # One shared word is not enough for a multi-term trigger...
    assert triggers.match("TSMC opens capacity in Arizona", trigger, ["TSMC"]) == 0.0
    # ...and the company name alone never counts.
    assert triggers.match("TSMC news", "TSMC", exclude=["TSMC"]) == 0.0
    assert triggers.match("Datacenter revenue falls", "revenue falls") == 1.0
