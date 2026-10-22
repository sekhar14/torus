"""Does an event's text say what a thesis trigger warns about? No LLM.

A trigger is free text ("TSMC CoWoS capacity slips into 2027"). Both texts are
reduced to informative terms: lower-cased, light suffix stripping, common
synonyms folded ("slips", "delayed", "pushed back" -> ``delay``), stop-words and
the names of the companies involved removed (the graph already accounts for who
the event is about). The match is the share of the trigger's terms the event
contains, and counts only when at least two terms (or all of a one-term
trigger) match.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

MIN_TERM_CHARS = 3

_WORD = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)*")
_PHRASES = [
    (re.compile(r"push(es|ed)? back|pushed out"), " delay "),
    (re.compile(r"step(s|ped|ping)? down"), " depart "),
    (re.compile(r"export (control|ban|curb|restriction|licen[cs]e)s?"), " restrict export "),
    (re.compile(r"entity list"), " restrict "),
]
_SYNONYMS = {
    "delay": "delay",
    "delayed": "delay",
    "delays": "delay",
    "slip": "delay",
    "slips": "delay",
    "slipped": "delay",
    "postpone": "delay",
    "postponed": "delay",
    "late": "delay",
    "cut": "cut",
    "cuts": "cut",
    "lower": "cut",
    "lowered": "cut",
    "lowers": "cut",
    "reduce": "cut",
    "reduced": "cut",
    "reduces": "cut",
    "slash": "cut",
    "slashed": "cut",
    "decline": "fall",
    "declines": "fall",
    "declined": "fall",
    "drop": "fall",
    "drops": "fall",
    "dropped": "fall",
    "fall": "fall",
    "falls": "fall",
    "fell": "fall",
    "miss": "fall",
    "misses": "fall",
    "missed": "fall",
    "raise": "rise",
    "raised": "rise",
    "raises": "rise",
    "increase": "rise",
    "increased": "rise",
    "increases": "rise",
    "rise": "rise",
    "rises": "rise",
    "rose": "rise",
    "hike": "rise",
    "beat": "rise",
    "beats": "rise",
    "ban": "restrict",
    "banned": "restrict",
    "bans": "restrict",
    "restrict": "restrict",
    "restricted": "restrict",
    "restricts": "restrict",
    "restriction": "restrict",
    "restrictions": "restrict",
    "curb": "restrict",
    "curbs": "restrict",
    "sanction": "restrict",
    "sanctions": "restrict",
    "resign": "depart",
    "resigns": "depart",
    "resigned": "depart",
    "departure": "depart",
    "departs": "depart",
    "exit": "depart",
    "guidance": "guidance",
    "outlook": "guidance",
    "forecast": "guidance",
    "forecasts": "guidance",
    "revenue": "revenue",
    "revenues": "revenue",
    "sales": "revenue",
    "margin": "margin",
    "margins": "margin",
    "shortage": "shortage",
    "shortages": "shortage",
    "constraint": "shortage",
    "constrained": "shortage",
    "lose": "loss",
    "loses": "loss",
    "lost": "loss",
    "losing": "loss",
    "loss": "loss",
    "chips": "chip",
    "gpus": "gpu",
    "fabs": "fab",
    "orders": "order",
    "customers": "customer",
    "suppliers": "supplier",
    "prices": "price",
    "pricing": "price",
}
STOPWORDS = frozenset(
    """
    a an and are as at be been being but by can could do does for from had has have if in
    into is it its may might more most no not of on or our over per should so than that the
    their them then there these they this to under until up was were what when which while
    who will with would year years quarter quarters next new company companies inc corp ltd
    co plc group holdings thesis any via vs versus also only than about after before during
    """.split()  # noqa: SIM905 - a word list reads better as text
)


def terms(text: str, exclude: Iterable[str] = ()) -> set[str]:
    """Informative, normalised terms of ``text``, minus words in ``exclude``."""
    lowered = text.casefold()
    for pattern, replacement in _PHRASES:
        lowered = pattern.sub(replacement, lowered)
    excluded = {w for name in exclude for w in _WORD.findall(name.casefold())}
    found = set()
    for word in _WORD.findall(lowered):
        if word in STOPWORDS or word in excluded or len(word) < MIN_TERM_CHARS:
            continue
        found.add(_SYNONYMS.get(word, _stem(word)))
    return found


def _stem(word: str) -> str:
    if word.isdigit() or len(word) <= 4:
        return word
    if word.endswith(("ss", "us", "is", "os")):
        return word  # "loss", "status", "analysis", "cowos"
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def match(event_text: str, trigger_text: str, exclude: Iterable[str] = ()) -> float:
    """0-1: how much of the trigger the event text covers (0 below the minimum)."""
    exclude = list(exclude)
    wanted = terms(trigger_text, exclude)
    if not wanted:
        return 0.0
    hits = len(wanted & terms(event_text, exclude))
    if hits < min(2, len(wanted)):
        return 0.0
    return round(hits / len(wanted), 4)
