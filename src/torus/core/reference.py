"""Reference data for entity resolution: the SEC company ticker list.

Source: https://www.sec.gov/files/company_tickers.json, shaped like
``{"0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"}, ...}``.
It is cached under ``TORUS_HOME/reference`` and only re-downloaded on request
(``torus init --refresh-reference --contact you@example.com``). The SEC requires
a User-Agent that identifies the requester.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

from torus import __version__
from torus.core.text import display_name, name_key, normalize_ticker

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_TICKERS_FILENAME = "sec_company_tickers.json"


def format_cik(cik: int | str) -> str:
    """Zero-padded 10-digit CIK, the form used in SEC URLs and canonical keys."""
    digits = str(cik).strip().lower().removeprefix("cik:").removeprefix("cik").strip()
    if not digits.isdigit() or len(digits) > 10:
        raise ValueError(f"not a CIK: {cik!r}")
    return digits.zfill(10)


def cik_key(cik: int | str) -> str:
    return f"cik:{format_cik(cik)}"


@dataclass(frozen=True)
class ReferenceCompany:
    cik: str  # 10-digit
    title: str
    tickers: tuple[str, ...]  # SEC order; first is the primary listing

    @property
    def canonical_key(self) -> str:
        return f"cik:{self.cik}"


@dataclass
class ReferenceData:
    """In-memory index over reference companies. Empty when nothing is cached."""

    companies: dict[str, ReferenceCompany] = field(default_factory=dict)  # by cik
    _by_ticker: dict[str, str] = field(default_factory=dict)
    _by_name: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_rows(cls, rows: Iterable[tuple[int | str, str, str]]) -> ReferenceData:
        """Build from (cik, ticker, title) rows; rows sharing a CIK are one company."""
        tickers: dict[str, list[str]] = {}
        titles: dict[str, str] = {}
        for cik, ticker, title in rows:
            c = format_cik(cik)
            titles.setdefault(c, title.strip())
            t = normalize_ticker(ticker)
            if t and t not in tickers.setdefault(c, []):
                tickers[c].append(t)
        data = cls()
        for c, title in titles.items():
            company = ReferenceCompany(
                cik=c, title=display_name(title, tickers[c]), tickers=tuple(tickers[c])
            )
            data.companies[c] = company
            for t in company.tickers:
                data._by_ticker.setdefault(t, c)
            key = name_key(title)
            if key:
                data._by_name.setdefault(key, []).append(c)
        return data

    @classmethod
    def from_sec_json(cls, payload: Any) -> ReferenceData:
        if not isinstance(payload, dict):
            raise ValueError("SEC ticker file must be a JSON object")
        rows = []
        for item in payload.values():
            try:
                rows.append((item["cik_str"], str(item["ticker"]), str(item["title"])))
            except (KeyError, TypeError) as exc:
                raise ValueError(f"unexpected SEC ticker row: {item!r}") from exc
        return cls.from_rows(rows)

    @classmethod
    def load(cls, path: Path) -> ReferenceData:
        """Load a cached SEC file; a missing file gives an empty index."""
        if not path.exists():
            return cls()
        return cls.from_sec_json(json.loads(path.read_text("utf-8")))

    def __len__(self) -> int:
        return len(self.companies)

    def by_cik(self, cik: int | str) -> ReferenceCompany | None:
        try:
            return self.companies.get(format_cik(cik))
        except ValueError:
            return None

    def by_ticker(self, ticker: str) -> ReferenceCompany | None:
        cik = self._by_ticker.get(normalize_ticker(ticker))
        return self.companies[cik] if cik else None

    def by_name(self, name: str) -> list[ReferenceCompany]:
        return [self.companies[c] for c in self._by_name.get(name_key(name), [])]

    def name_keys(self) -> dict[str, list[str]]:
        """name_key -> CIKs, for the detector's n-gram index."""
        return self._by_name


if TYPE_CHECKING:  # urllib is only needed for downloads; keep hook start-up lean
    import urllib.request

Opener = Callable[["urllib.request.Request", float], BinaryIO]


def _default_opener(request: urllib.request.Request, timeout: float) -> BinaryIO:
    import urllib.request

    response: BinaryIO = urllib.request.urlopen(request, timeout=timeout)
    return response


def fetch_sec_tickers(
    dest: Path,
    contact: str,
    url: str = SEC_TICKERS_URL,
    timeout: float = 30.0,
    opener: Opener = _default_opener,
) -> ReferenceData:
    """Download the SEC ticker list to ``dest`` atomically and return its index.

    The file is validated before it replaces any existing cache, so a bad
    download never clobbers good data.
    """
    import urllib.request

    if "@" not in contact:
        raise ValueError("the SEC requires a contact email in the User-Agent")
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"torus/{__version__} {contact}",
            "Accept": "application/json",
        },
    )
    with opener(request, timeout) as response:
        raw = response.read()
    data = ReferenceData.from_sec_json(json.loads(raw))
    if not data:
        raise ValueError("SEC ticker file was empty")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(raw)
    os.replace(tmp, dest)
    return data
