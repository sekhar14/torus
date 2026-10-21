"""Find entities mentioned in free text (prompts, headlines) without an LLM.

Three signals, from most to least reliable:

* cashtags      ``$NVDA``          any known ticker (graph or SEC reference)
* bare tickers  ``NVDA``           upper-case 2-5 letters, not a stop-word, known
                                   ticker; disabled when the whole text is shouted
* names         ``Nvidia``,        n-grams matched on ``name_key`` against graph
                ``Taiwan Semi...`` aliases and SEC titles; single words must be
                                   Capitalised (except graph abbreviations)

Every hit gets a confidence; hits below ``min_confidence`` are dropped, then
overlapping spans keep the longest (then most confident) one. A surface that maps
to more than one entity is reported as ambiguous instead of guessed. The intended
use (prompt-time recall) prefers missing an entity to injecting the wrong one.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field

from torus.core.models import AliasType
from torus.core.reference import ReferenceData
from torus.core.store.base import Backend
from torus.core.text import name_key, normalize_alias, normalize_ticker

# Confidence by signal and source.
CONF_CASHTAG_GRAPH = 0.95
CONF_CASHTAG_REFERENCE = 0.9
CONF_TICKER_GRAPH = 0.85
CONF_TICKER_REFERENCE = 0.65
CONF_NAME_GRAPH_MULTI = 0.95
CONF_NAME_GRAPH_SINGLE = 0.9
CONF_NAME_REFERENCE_MULTI = 0.7
CONF_NAME_REFERENCE_SINGLE = 0.55  # below the default threshold: "Target", "Visa", "Shell"
DEFAULT_MIN_CONFIDENCE = 0.6
MAX_NAME_TOKENS = 8

# Upper-case words that are common acronyms or English words, never taken as bare
# tickers (a cashtag still works: "$AI", "$NOW", "$ON").
STOPLIST = frozenset(
    """
    A I AI ALL AN AND ANY ARE AS AT BE BIG BY CAN DO FOR GO HAS HE IF IN IS IT ME MY NO
    NOT NOW OF OK ON ONE OR OUT SO THE TO UP US WE WELL YOU NEW KEY LOW HIGH TOP BEST
    REAL TRUE FUN LOVE PLAY RUN SAVE WORK OPEN CASH HOLD BUY SELL LONG SHORT GOOD BAD
    CEO CFO COO CTO CIO CMO VP SVP EVP IR PR HR
    USA UK EU UAE APAC EMEA LATAM
    AGI LLM LLMS ML GPU GPUS CPU CPUS TPU HBM DRAM NAND ASIC ASICS API APIS SAAS PAAS IAAS
    ARR MRR NRR TAM SAM SOM KPI KPIS OKR
    ETF ETFS IPO IPOS SPAC SPACS ADR ADRS OTC NYSE NASDAQ SEC FINRA FDA FTC DOJ FCC EPA
    FED FOMC ECB BOJ PBOC IMF OPEC WTO
    GDP CPI PPI PCE PMI ISM NFP YOY QOQ MOM YTD QTD MTD TTM LTM NTM FY FYE CY
    EPS PE PEG PB PS EV EVS EBIT EBITDA FCF ROE ROA ROI ROIC WACC DCF NPV IRR CAGR
    GAAP IFRS OPEX CAPEX COGS SGA RND NII NIM AUM NAV
    ESG DEI LBO
    USD EUR JPY GBP CNY INR BTC ETH
    TV PC PCS AR VR XR AV IOT SKU SKUS
    Q1 Q2 Q3 Q4 H1 H2
    """.split()  # noqa: SIM905 - a word list reads better as text
)

_TOKEN = re.compile(r"[A-Za-z0-9]+(?:&[A-Za-z0-9]+)*")
_NAME_GAP = re.compile(r"^[\s\-.,&']{1,3}$")
_CASHTAG = re.compile(r"(?<![\w$])\$([A-Za-z]{1,5}(?:[.\-][A-Za-z]{1,2})?)(?![\w])")
_BARE_TICKER = re.compile(r"(?<![\w$&.\-])([A-Z]{2,5}(?:[.\-][A-Z])?)(?![\w&])")


@dataclass(frozen=True)
class Detection:
    surface: str
    start: int
    end: int
    method: str  # cashtag | ticker | name
    confidence: float
    entity_id: str | None  # set when the entity is in the graph
    canonical_key: str | None  # cik:... for reference companies
    name: str

    @property
    def identity(self) -> str:
        return self.entity_id or self.canonical_key or self.name


@dataclass(frozen=True)
class Ambiguity:
    surface: str
    start: int
    end: int
    candidates: tuple[str, ...]  # entity ids and/or canonical keys


@dataclass(frozen=True)
class DetectResult:
    detections: tuple[Detection, ...]
    ambiguous: tuple[Ambiguity, ...] = field(default=())

    @property
    def entity_ids(self) -> list[str]:
        return [d.entity_id for d in self.detections if d.entity_id]


@dataclass(frozen=True)
class _Hit:
    start: int
    end: int
    method: str
    confidence: float
    identities: frozenset[tuple[str | None, str | None, str]]  # (entity_id, key, name)


class EntityDetector:
    """Builds an in-memory index once; call ``refresh()`` after the graph changes."""

    def __init__(self, store: Backend, reference: ReferenceData | None = None) -> None:
        self.store = store
        self.reference = reference or ReferenceData()
        self.refresh()

    def refresh(self) -> None:
        self._entities = self.store.entity_index()  # id -> (canonical_key, name)
        self._by_key = {key: eid for eid, (key, _) in self._entities.items() if key}
        self._tickers: dict[str, set[str]] = {}
        self._names: dict[str, set[str]] = {}
        self._lowercase_ok: set[str] = set()
        for entity_id, alias, alias_type in self.store.all_aliases():
            if alias_type is AliasType.TICKER:
                self._tickers.setdefault(normalize_ticker(alias), set()).add(entity_id)
            elif alias_type in (AliasType.NAME, AliasType.FORMER_NAME, AliasType.ABBREVIATION):
                key = name_key(alias)
                if key:
                    self._names.setdefault(key, set()).add(entity_id)
                    if alias_type is AliasType.ABBREVIATION:
                        # "tsmc" is safe lower-case; "apple" is not.
                        self._lowercase_ok.add(key)
        longest = [len(k.split()) for k in (*self._names, *self.reference.name_keys())]
        self._max_tokens = min(MAX_NAME_TOKENS, max(longest, default=1))

    # -- public ---------------------------------------------------------------

    def detect(self, text: str, min_confidence: float = DEFAULT_MIN_CONFIDENCE) -> DetectResult:
        hits = [*self._cashtags(text), *self._names_in(text)]
        if not _is_shouting(text):
            hits += self._bare_tickers(text)

        ambiguous = [
            Ambiguity(
                surface=text[h.start : h.end],
                start=h.start,
                end=h.end,
                candidates=tuple(sorted(eid or key or name for eid, key, name in h.identities)),
            )
            for h in hits
            if len(h.identities) > 1 and h.confidence >= min_confidence
        ]
        usable = [h for h in hits if len(h.identities) == 1 and h.confidence >= min_confidence]
        usable.sort(key=lambda h: (-(h.end - h.start), -h.confidence, h.start))
        taken: list[_Hit] = []
        for h in usable:
            if all(h.end <= t.start or h.start >= t.end for t in taken):
                taken.append(h)

        best: dict[str, Detection] = {}
        for h in sorted(taken, key=lambda h: h.start):
            ((entity_id, key, name),) = h.identities
            d = Detection(
                surface=text[h.start : h.end],
                start=h.start,
                end=h.end,
                method=h.method,
                confidence=h.confidence,
                entity_id=entity_id,
                canonical_key=key,
                name=name,
            )
            if d.identity not in best or d.confidence > best[d.identity].confidence:
                best[d.identity] = d
        detections = sorted(best.values(), key=lambda d: d.start)
        return DetectResult(tuple(detections), tuple(ambiguous))

    # -- signals --------------------------------------------------------------

    def _graph_identity(self, entity_id: str) -> tuple[str | None, str | None, str]:
        key, name = self._entities[entity_id]
        return (entity_id, key, name)

    def _reference_identity(self, cik: str) -> tuple[str | None, str | None, str]:
        company = self.reference.companies[cik]
        entity_id = self._by_key.get(company.canonical_key)
        if entity_id is not None:
            return self._graph_identity(entity_id)
        return (None, company.canonical_key, company.title)

    def _ticker_hit(
        self, ticker: str, start: int, end: int, method: str, graph_conf: float, ref_conf: float
    ) -> _Hit | None:
        ids = self._tickers.get(normalize_ticker(ticker))
        if ids:
            identities = frozenset(self._graph_identity(e) for e in ids)
            return _Hit(start, end, method, graph_conf, identities)
        company = self.reference.by_ticker(ticker)
        if company is not None:
            identity = self._reference_identity(company.cik)
            conf = graph_conf if identity[0] else ref_conf
            return _Hit(start, end, method, conf, frozenset({identity}))
        return None

    def _cashtags(self, text: str) -> list[_Hit]:
        hits = []
        for m in _CASHTAG.finditer(text):
            hit = self._ticker_hit(
                m.group(1),
                m.start(),
                m.end(),
                "cashtag",
                CONF_CASHTAG_GRAPH,
                CONF_CASHTAG_REFERENCE,
            )
            if hit:
                hits.append(hit)
        return hits

    def _bare_tickers(self, text: str) -> list[_Hit]:
        hits = []
        for m in _BARE_TICKER.finditer(text):
            if m.group(1) in STOPLIST:
                continue
            hit = self._ticker_hit(
                m.group(1),
                m.start(1),
                m.end(1),
                "ticker",
                CONF_TICKER_GRAPH,
                CONF_TICKER_REFERENCE,
            )
            if hit:
                hits.append(hit)
        return hits

    def _names_in(self, text: str) -> list[_Hit]:
        tokens = [(m.start(), m.end()) for m in _TOKEN.finditer(text)]
        ref_names = self.reference.name_keys()
        hits = []
        for i in range(len(tokens)):
            for n in range(min(self._max_tokens, len(tokens) - i), 0, -1):
                span = tokens[i : i + n]
                if any(not _NAME_GAP.match(text[a[1] : b[0]]) for a, b in itertools.pairwise(span)):
                    continue
                start, end = span[0][0], span[-1][1]
                surface = text[start:end]
                key = name_key(surface)
                if not key:
                    continue
                # Capitalisation of the word(s) that carry the key, not a leading "The".
                key_tokens = [
                    text[a:b] for a, b in span if normalize_alias(text[a:b]) in key.split()
                ]
                capitalised = bool(key_tokens) and key_tokens[0][0].isupper()
                single = len(key.split()) == 1
                if single and (
                    (not capitalised and key not in self._lowercase_ok)
                    or surface.upper() in STOPLIST
                ):
                    continue
                ids = self._names.get(key)
                if ids:
                    conf = CONF_NAME_GRAPH_SINGLE if single else CONF_NAME_GRAPH_MULTI
                    identities = frozenset(self._graph_identity(e) for e in ids)
                    hits.append(_Hit(start, end, "name", conf, identities))
                    continue
                ciks = ref_names.get(key)
                if ciks and capitalised:
                    identities = frozenset(self._reference_identity(c) for c in ciks)
                    in_graph = all(eid for eid, _, _ in identities)
                    if in_graph:
                        conf = CONF_NAME_GRAPH_SINGLE if single else CONF_NAME_GRAPH_MULTI
                    else:
                        conf = CONF_NAME_REFERENCE_SINGLE if single else CONF_NAME_REFERENCE_MULTI
                    hits.append(_Hit(start, end, "name", conf, identities))
        return hits


def _is_shouting(text: str) -> bool:
    """All-caps text makes every word look like a ticker; skip bare tickers then."""
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 12:
        return False
    return sum(c.isupper() for c in letters) / len(letters) > 0.7
