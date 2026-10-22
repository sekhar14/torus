"""Turn a thesis horizon ("12m", "FY2026", "Q3 2026", "2027-06-30") into a date.

Horizons are written by people and agents in many ways. Anything not understood
gives ``None``; the thesis then simply has no horizon date (the audit can't tell
when it's due, nothing else changes).
"""

from __future__ import annotations

import calendar
import re
from datetime import UTC, datetime, timedelta

_DURATION = re.compile(
    r"^(\d{1,3})\s*(d|days?|w|wks?|weeks?|m|mo|mos|months?|q|qtrs?|quarters?|y|yrs?|years?)$"
)
_FISCAL_YEAR = re.compile(r"^(?:fy|cy)\s*'?(\d{2}|\d{4})$")
_YEAR = re.compile(r"^(?:end of |by |eoy )?(\d{4})$")
_ISO = re.compile(r"^(\d{4})-(\d{2})(?:-(\d{2}))?$")


def _year(text: str) -> int:
    value = int(text)
    return 2000 + value if value < 100 else value


def _month_end(year: int, month: int) -> datetime:
    day = calendar.monthrange(year, month)[1]
    return datetime(year, month, day, 23, 59, 59, tzinfo=UTC)


def _add_months(start: datetime, months: int) -> datetime:
    month = start.month - 1 + months
    year = start.year + month // 12
    month = month % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return start.replace(year=year, month=month, day=day)


def _period(text: str, letter: str, per_year: int) -> tuple[int, int] | None:
    """(quarter or half, year) from "Q3 2026", "Q3 FY26", "2026Q3" or "3Q26"."""
    n = f"[1-{per_year}]"
    year = r"'?(\d{2}|\d{4})"
    if m := re.match(rf"^{letter}({n})\s*(?:fy|cy)?\s*{year}$", text):
        return int(m.group(1)), _year(m.group(2))
    if m := re.match(rf"^(\d{{4}})\s*{letter}({n})$", text):
        return int(m.group(2)), _year(m.group(1))
    if m := re.match(rf"^({n}){letter}\s*{year}$", text):
        return int(m.group(1)), _year(m.group(2))
    return None


def parse_horizon(horizon: str | None, start: datetime) -> datetime | None:
    """When a thesis with this horizon, made at ``start``, is due."""
    if not horizon:
        return None
    text = re.sub(r"\s+", " ", horizon.strip().casefold()).strip(" .")
    text = text.removeprefix("next ").removeprefix("within ").removeprefix("over ")
    if m := _DURATION.match(text):
        n, unit = int(m.group(1)), m.group(2)
        if unit.startswith("d"):
            return start + timedelta(days=n)
        if unit.startswith("w"):
            return start + timedelta(weeks=n)
        if unit.startswith("q"):
            return _add_months(start, 3 * n)
        if unit.startswith("y"):
            return _add_months(start, 12 * n)
        return _add_months(start, n)
    if m := _FISCAL_YEAR.match(text) or _YEAR.match(text):
        return _month_end(_year(m.group(1)), 12)
    for letter, per_year in (("q", 4), ("h", 2)):
        found = _period(text, letter, per_year)
        if found is not None:
            number, year = found
            return _month_end(year, number * 12 // per_year)
    if m := _ISO.match(text):
        year, month = int(m.group(1)), int(m.group(2))
        if not 1 <= month <= 12:
            return None
        if m.group(3):
            try:
                return datetime(year, month, int(m.group(3)), 23, 59, 59, tzinfo=UTC)
            except ValueError:
                return None
        return _month_end(year, month)
    return None
