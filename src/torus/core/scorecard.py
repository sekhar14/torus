"""Was the thesis right? Returns, outcomes and calibration. No LLM.

A tracked thesis has a ticker, an entry (price and date; by default the close on
the day it was recorded) and a benchmark (SPY unless set). Its score is the
excess return over the benchmark since entry, signed by the thesis's stance
(bullish +1, bearish -1), and the worst drawdown in that direction.

Outcomes, used for calibration:

* ``status``  - the user marked the thesis confirmed (right) or broken (wrong)
* ``horizon`` - an open, tracked thesis reached its horizon: right if the stance
  return is positive (automatic; the user can override it)
* ``user``    - set by hand; never replaced automatically

Calibration compares each decided thesis's *initial* confidence with whether it
was right: the Brier score (0 is perfect, 0.25 is a coin flip at 50%) and a
reliability table by confidence band. A record of the user's own calls, not
investment advice.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta

from pydantic import BaseModel

from torus.core.models import Outcome, Stance, Thesis, ThesisStatus
from torus.core.store.base import Backend
from torus.sources.prices import PriceSource

DEFAULT_BENCHMARK = "SPY"
BENCHMARK_SETTING = "benchmark"
PRICE_SOURCE_SETTING = "price_source"  # yahoo (default) | none
MIN_DECIDED = 10  # below this, calibration is anecdote rather than evidence
BANDS = ((0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01))
SIGN = {Stance.BULLISH: 1, Stance.BEARISH: -1}
LOOKBACK_DAYS = 10  # fetch a little before entry so the entry day has a close


class ThesisScore(BaseModel):
    thesis_id: str
    title: str
    stance: Stance
    status: ThesisStatus
    confidence: float
    initial_confidence: float
    symbol: str
    benchmark: str
    entry_day: date | None
    entry_price: float | None
    last_day: date | None
    last_price: float | None
    asset_return: float | None
    benchmark_return: float | None
    excess_return: float | None
    stance_return: float | None  # excess return in the thesis's direction
    max_drawdown: float | None  # worst fall from a peak, in the thesis's direction
    days_held: int | None
    days_to_horizon: int | None
    outcome: Outcome | None
    outcome_method: str | None
    missing: str | None = None  # why some numbers are absent


class Band(BaseModel):
    label: str
    theses: int
    mean_confidence: float | None
    hit_rate: float | None


class Calibration(BaseModel):
    decided: int
    right: int
    wrong: int
    hit_rate: float | None
    brier: float | None
    bands: list[Band]


class Scorecard(BaseModel):
    generated_at: datetime
    theses: list[ThesisScore]
    calibration: Calibration
    markdown: str = ""


def benchmark_for(store: Backend, thesis: Thesis) -> str:
    return thesis.benchmark or store.get_setting(BENCHMARK_SETTING) or DEFAULT_BENCHMARK


def entry_day(thesis: Thesis) -> date:
    return (thesis.entry_at or thesis.created_at).date()


def tracked(store: Backend) -> list[Thesis]:
    return [t for t in store.theses() if t.instrument]


def refresh_prices(
    store: Backend, source: PriceSource, today: date, theses: Sequence[Thesis] | None = None
) -> dict[str, int | str]:
    """Fetch closes for tracked theses and their benchmarks. Returns symbol ->
    rows stored, or an error message for symbols that failed."""
    wanted: dict[str, date] = {}
    for thesis in theses if theses is not None else tracked(store):
        start = entry_day(thesis) - timedelta(days=LOOKBACK_DAYS)
        for symbol in (thesis.instrument or "", benchmark_for(store, thesis)):
            if symbol:
                wanted[symbol] = min(start, wanted.get(symbol, start))
    report: dict[str, int | str] = {}
    for symbol, start in sorted(wanted.items()):
        have = store.prices(symbol)
        if have and have[0][0] <= start + timedelta(days=LOOKBACK_DAYS):
            start = have[-1][0]  # only what's new
        try:
            rows = source.daily(symbol, start, today)
        except Exception as exc:  # one bad symbol shouldn't stop the rest
            report[symbol] = f"error: {exc}"
            continue
        report[symbol] = store.upsert_prices(symbol, rows, source.name)
    return report


def _close_on(prices: list[tuple[date, float]], day: date) -> tuple[date, float] | None:
    """The last close on or before ``day``."""
    found = None
    for d, close in prices:
        if d > day:
            break
        found = (d, close)
    return found


def stance_move(store: Backend, thesis: Thesis, since: date, until: date) -> float | None:
    """Return against the benchmark between two days (from entry, if later), in the
    thesis's direction; None without a stance, a symbol or prices on both days."""
    sign = SIGN.get(thesis.stance)
    if not sign or not thesis.instrument:
        return None
    start = max(since, entry_day(thesis))
    moves = []
    for symbol in (thesis.instrument, benchmark_for(store, thesis)):
        prices = store.prices(symbol, start - timedelta(days=LOOKBACK_DAYS))
        first, last = _close_on(prices, start), _close_on(prices, until)
        if first is None or last is None or last[0] <= first[0]:
            return None
        moves.append(last[1] / first[1] - 1)
    return round((moves[0] - moves[1]) * sign, 4)


def _initial_confidence(store: Backend, thesis: Thesis) -> float:
    history = store.thesis_history(thesis.id)
    return history[0].confidence if history else thesis.confidence


def score(store: Backend, thesis: Thesis, now: datetime) -> ThesisScore:
    symbol = thesis.instrument or ""
    bench = benchmark_for(store, thesis)
    start = entry_day(thesis)
    outcomes = store.thesis_outcomes([thesis.id])
    outcome = outcomes.get(thesis.id)
    base = ThesisScore(
        thesis_id=thesis.id,
        title=thesis.title,
        stance=thesis.stance,
        status=thesis.status,
        confidence=thesis.confidence,
        initial_confidence=_initial_confidence(store, thesis),
        symbol=symbol,
        benchmark=bench,
        entry_day=None,
        entry_price=thesis.entry_price,
        last_day=None,
        last_price=None,
        asset_return=None,
        benchmark_return=None,
        excess_return=None,
        stance_return=None,
        max_drawdown=None,
        days_held=None,
        days_to_horizon=(thesis.horizon_date - now).days if thesis.horizon_date else None,
        outcome=outcome.outcome if outcome else None,
        outcome_method=outcome.method if outcome else None,
    )
    asset = store.prices(symbol, start - timedelta(days=LOOKBACK_DAYS))
    entry = _close_on(asset, start)
    if not asset or (entry is None and thesis.entry_price is None):
        return base.model_copy(update={"missing": f"no prices for {symbol} yet (refresh them)"})
    entry_px = thesis.entry_price or (entry[1] if entry else asset[0][1])
    last_day, last_px = asset[-1]
    asset_return = last_px / entry_px - 1
    sign = SIGN.get(thesis.stance, 0)
    # Drawdown in the thesis's direction: for a bearish thesis, a rally hurts.
    worst, peak = 0.0, None
    for day, close in asset:
        if day < start:
            continue
        value = (close / entry_px) ** sign if sign else close / entry_px
        peak = value if peak is None else max(peak, value)
        worst = min(worst, value / peak - 1)
    update: dict[str, object] = {
        "entry_day": entry[0] if entry else start,
        "entry_price": round(entry_px, 4),
        "last_day": last_day,
        "last_price": round(last_px, 4),
        "asset_return": round(asset_return, 4),
        "max_drawdown": round(worst, 4),
        "days_held": (last_day - start).days,
    }
    bench_prices = store.prices(bench, start - timedelta(days=LOOKBACK_DAYS))
    bench_entry = _close_on(bench_prices, start)
    if bench_entry and bench_prices:
        bench_return = bench_prices[-1][1] / bench_entry[1] - 1
        excess = asset_return - bench_return
        update["benchmark_return"] = round(bench_return, 4)
        update["excess_return"] = round(excess, 4)
        update["stance_return"] = round(excess * sign, 4) if sign else None
    else:
        update["missing"] = f"no prices for benchmark {bench} yet"
    return base.model_copy(update=update)


def decide_outcomes(store: Backend, now: datetime) -> int:
    """Record automatic outcomes: confirmed/broken theses, and tracked open theses
    past their horizon. Returns how many outcomes were set or changed."""
    existing = store.thesis_outcomes()
    changed = 0
    for thesis in store.theses():
        current = existing.get(thesis.id)
        if current is not None and current.method == "user":
            continue
        if thesis.status in (ThesisStatus.CONFIRMED, ThesisStatus.BROKEN):
            outcome = Outcome.RIGHT if thesis.status is ThesisStatus.CONFIRMED else Outcome.WRONG
            stance_return = score(store, thesis, now).stance_return if thesis.instrument else None
            if current is None or current.outcome is not outcome or current.method != "status":
                store.set_thesis_outcome(thesis.id, outcome, "status", stance_return)
                changed += 1
            continue
        if (
            thesis.instrument
            and thesis.status in (ThesisStatus.ACTIVE, ThesisStatus.AT_RISK)
            and thesis.horizon_date is not None
            and thesis.horizon_date <= now
            and SIGN.get(thesis.stance)
        ):
            result = score(store, thesis, now).stance_return
            if result is None:
                continue
            outcome = Outcome.RIGHT if result > 0 else Outcome.WRONG
            if current is None or current.outcome is not outcome:
                store.set_thesis_outcome(
                    thesis.id, outcome, "horizon", round(result, 4), "past horizon, by price"
                )
                changed += 1
    return changed


def calibration(store: Backend, theses: Sequence[Thesis] | None = None) -> Calibration:
    outcomes = store.thesis_outcomes()
    pairs = []
    for thesis in theses if theses is not None else store.theses():
        outcome = outcomes.get(thesis.id)
        if outcome is None or outcome.outcome is Outcome.UNCLEAR:
            continue
        pairs.append(
            (_initial_confidence(store, thesis), 1.0 if outcome.outcome is Outcome.RIGHT else 0.0)
        )
    bands = []
    for low, high in BANDS:
        inside = [(c, y) for c, y in pairs if low <= c < high]
        label = f"{low:.0%}-{min(high, 1.0):.0%}"
        bands.append(
            Band(
                label=label,
                theses=len(inside),
                mean_confidence=round(sum(c for c, _ in inside) / len(inside), 3)
                if inside
                else None,
                hit_rate=round(sum(y for _, y in inside) / len(inside), 3) if inside else None,
            )
        )
    right = sum(1 for _, y in pairs if y == 1.0)
    return Calibration(
        decided=len(pairs),
        right=right,
        wrong=len(pairs) - right,
        hit_rate=round(right / len(pairs), 3) if pairs else None,
        brier=round(sum((c - y) ** 2 for c, y in pairs) / len(pairs), 4) if pairs else None,
        bands=bands,
    )


def scorecard(store: Backend, now: datetime) -> Scorecard:
    decide_outcomes(store, now)
    scores = [score(store, t, now) for t in tracked(store)]
    scores.sort(key=lambda s: (s.stance_return is None, -(s.stance_return or 0.0)))
    cal = calibration(store)
    return Scorecard(
        generated_at=now,
        theses=scores,
        calibration=cal,
        markdown=render_scorecard(scores, cal),
    )


def render_scorecard(theses: Sequence[ThesisScore], cal: Calibration) -> str:
    """Markdown tables for tracked theses and calibration."""

    def pct(value: float | None) -> str:
        return f"{value:+.1%}" if value is not None else "n/a"

    lines: list[str] = []
    if theses:
        lines += [
            "| Thesis | Ticker | Stance | Vs Benchmark | Max Drawdown | Days | Outcome |",
            "|---|---|---|---:|---:|---:|---|",
        ]
        for s in theses:
            title = s.title.replace("|", "\\|")
            days = str(s.days_held) if s.days_held is not None else "n/a"
            outcome = s.outcome.value if s.outcome else "open"
            lines.append(
                f"| {title} (`{s.thesis_id}`) | {s.symbol} | {s.stance.value} |"
                f" {pct(s.stance_return)} | {pct(s.max_drawdown)} | {days} | {outcome} |"
            )
    else:
        lines.append("No theses are tracked against a price yet.")
    if cal.decided:
        brier = f"{cal.brier:.4f}" if cal.brier is not None else "n/a"
        hit = f"{cal.hit_rate:.0%}" if cal.hit_rate is not None else "n/a"
        note = " (fewer than 10 decided — anecdote)" if cal.decided < MIN_DECIDED else ""
        lines += [
            "",
            f"**Calibration:** {cal.right}/{cal.decided} right ({hit}), Brier {brier}{note}",
        ]
    return "\n".join(lines)


def render_score(s: ThesisScore) -> str:
    """Markdown lines for one thesis (used by the memo)."""

    def pct(value: float | None) -> str:
        return f"{value:+.1%}" if value is not None else "n/a"

    lines = [
        f"- Tracking **{s.symbol}** against {s.benchmark} since {s.entry_day or 'entry'}"
        f" (entry {s.entry_price if s.entry_price is not None else 'n/a'})",
        f"- Return {pct(s.asset_return)}, benchmark {pct(s.benchmark_return)},"
        f" excess {pct(s.excess_return)}; in the thesis's direction {pct(s.stance_return)}",
        f"- Worst drawdown {pct(s.max_drawdown)}",
    ]
    if s.outcome:
        lines.append(f"- Outcome: **{s.outcome.value}** ({s.outcome_method})")
    if s.missing:
        lines.append(f"- *{s.missing}*")
    return "\n".join(lines)
