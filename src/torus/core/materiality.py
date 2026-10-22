"""How much might an event matter? A 0-1 weight, no LLM.

Impact analysis only knows *who* an event is about. Materiality adds *what kind*
of event it is, so a routine 10-Q from a supplier doesn't alert every thesis
downstream while a bankruptcy or a material agreement does:

* SEC filings: by form, and for 8-Ks by item (the highest-weighted item wins)
* any text (headlines, summaries): keyword families raise the weight
  ("export controls", "guidance cut", "recall"...) or cap it for listicles
  ("3 stocks to watch")
* events recorded by the user or the agent default to 1.0: someone chose them

Alerts score ``graph score * materiality * (1 + trigger match)``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

MANUAL_SOURCES = frozenset({"manual", "agent", "user"})
DEFAULT_TEXT_WEIGHT = 0.5
AMENDMENT_WEIGHT = 0.3

ITEM_WEIGHTS = {
    "1.01": 0.8,  # material agreement
    "1.02": 0.8,  # agreement terminated
    "1.03": 1.0,  # bankruptcy
    "1.05": 0.8,  # cybersecurity incident
    "2.01": 0.8,  # acquisition or disposition
    "2.02": 0.9,  # results of operations
    "2.03": 0.5,  # new financial obligation
    "2.04": 0.7,  # triggering events (acceleration)
    "2.05": 0.8,  # restructuring
    "2.06": 0.8,  # impairment
    "3.01": 0.9,  # delisting
    "3.02": 0.4,  # unregistered equity sales
    "3.03": 0.5,  # holders' rights modified
    "4.01": 0.6,  # auditor change
    "4.02": 1.0,  # prior financials unreliable
    "5.01": 0.9,  # change in control
    "5.02": 0.6,  # executive or director change
    "5.03": 0.2,  # bylaws
    "5.07": 0.1,  # shareholder vote results
    "7.01": 0.4,  # Regulation FD
    "8.01": 0.4,  # other events
    "9.01": 0.1,  # exhibits only
}
FORM_WEIGHTS = {
    "8-K": 0.6,  # no items listed
    "10-K": 0.6,
    "20-F": 0.6,
    "40-F": 0.6,
    "10-Q": 0.4,
    "6-K": 0.4,
}
_FORM_IN_TEXT = re.compile(r"(?<![\w-])(10-K|10-Q|8-K|20-F|40-F|6-K)(/A)?(?![\w-])", re.IGNORECASE)

# (weight, why, alternatives). Matching raises the weight to at least ``weight``.
_RAISE: list[tuple[float, str, tuple[str, ...]]] = [
    (1.0, "bankruptcy", ("bankrupt", "chapter 11", "insolven", "going concern")),
    (1.0, "restatement or fraud", (r"restat(e|ement)", "accounting irregularit", "fraud")),
    (0.9, "delisting", ("delist",)),
    (
        0.9,
        "guidance cut",
        (
            r"(cuts?|lower(s|ed)?|slash(es|ed)?|withdraw(s|n)?)\W+(\w+\W+){0,3}"
            r"(guidance|outlook|forecast)",
            "profit warning",
        ),
    ),
    (
        0.9,
        "export controls or sanctions",
        (r"export (control|ban|curb|restriction|licen[cs]e)", "sanction", "entity list"),
    ),
    (0.9, "product recall", (r"\brecall(s|ed)?\b",)),
    (0.8, "acquisition or merger", (r"acqui(re|res|red|sition)", "merger", "takeover", "buyout")),
    (
        0.8,
        "regulatory action",
        ("antitrust", "subpoena", "probe", "investigation", "indict", r"penalt(y|ies)"),
    ),
    (
        0.8,
        "operational disruption",
        (
            "outage",
            r"\bfire\b",
            "explosion",
            "earthquake",
            "flood",
            "shutdown",
            r"halt(s|ed)? production",
            "cyberattack",
            "breach",
        ),
    ),
    (
        0.7,
        "guidance raised or results",
        (
            r"rais(es|ed)\W+(\w+\W+){0,3}(guidance|outlook|forecast)",
            r"beats? estimates",
            r"miss(es|ed)? estimates",
            "earnings",
            "quarterly results",
        ),
    ),
    (0.7, "delay or shortage", (r"delay(s|ed)?", "postpone", r"push(es|ed)? back", "shortage")),
    (0.7, "tariffs", ("tariff",)),
    (
        0.7,
        "leadership change",
        (
            r"(ceo|cfo|chief executive|chief financial).{0,40}"
            r"(resign|step(s|ping)? down|depart|ousted|fired|replace)",
            r"(resign|step(s|ping)? down).{0,40}(ceo|cfo|chief)",
        ),
    ),
    (0.7, "strike or labor action", (r"\bstrikes?\b", "walkout")),
    (0.7, "rating downgrade", ("downgrad",)),
    (
        0.6,
        "contract or partnership",
        (r"\bcontract", r"award(ed|s)?\b", "partnership", "supply agreement", r"orders? for"),
    ),
    (0.6, "litigation", ("lawsuit", r"\bsues?\b", r"\bsued\b", "litigation", "patent")),
    (0.6, "capacity or capex change", ("capacity", "capex", "capital expenditure", "new fab")),
]
_CAP: list[tuple[float, str, tuple[str, ...]]] = [
    (
        0.2,
        "listicle or opinion piece",
        (
            r"stocks? to (watch|buy)",
            r"\btop \d+\b",
            "should you buy",
            "is it time to",
            r"here'?s why",
            r"\bbest .{0,20}stocks?\b",
            "price target",
        ),
    ),
]
_RAISE_RE = [(w, why, re.compile("|".join(ps), re.IGNORECASE)) for w, why, ps in _RAISE]
_CAP_RE = [(w, why, re.compile("|".join(ps), re.IGNORECASE)) for w, why, ps in _CAP]


@dataclass(frozen=True)
class Materiality:
    value: float
    reason: str


def filing_weight(form: str, items: tuple[str, ...] = ()) -> Materiality:
    base = form.upper().removesuffix("/A")
    if form.upper().endswith("/A"):
        return Materiality(AMENDMENT_WEIGHT, f"{form} amendment")
    if base == "8-K" and items:
        weights = {i: ITEM_WEIGHTS.get(i, 0.5) for i in items}
        item = max(weights, key=lambda i: weights[i])
        return Materiality(weights[item], f"8-K item {item}")
    if base in FORM_WEIGHTS:
        return Materiality(FORM_WEIGHTS[base], f"{base} filing")
    return Materiality(DEFAULT_TEXT_WEIGHT, f"{form} filing")


def text_weight(text: str, base: Materiality) -> Materiality:
    """Raise ``base`` for high-signal wording, or cap it for listicles."""
    for weight, why, pattern in _CAP_RE:
        if pattern.search(text):
            return Materiality(min(base.value, weight), why)
    best = base
    for weight, why, pattern in _RAISE_RE:
        if weight > best.value and pattern.search(text):
            best = Materiality(weight, why)
    return best


def assess(
    source: str,
    title: str,
    summary: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> Materiality:
    """Materiality of an event from where it came from and what it says."""
    if source in MANUAL_SOURCES:
        return Materiality(1.0, "recorded by hand")
    payload = payload or {}
    form = str(payload.get("form") or "")
    items = tuple(str(i) for i in payload.get("items") or ())
    if not form:
        found = _FORM_IN_TEXT.search(title)
        if found:
            form = found.group(1).upper() + (found.group(2) or "").upper()
    base = filing_weight(form, items) if form else Materiality(DEFAULT_TEXT_WEIGHT, "news item")
    if form and base.reason.startswith("8-K item"):
        return base  # the item says what the 8-K is; headline words add little
    return text_weight(f"{title}. {summary or ''}", base)
