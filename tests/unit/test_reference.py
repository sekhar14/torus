from __future__ import annotations

import io
import json
import urllib.request
from pathlib import Path

import pytest

from conftest import FIXTURES
from torus.core.reference import (
    ReferenceData,
    cik_key,
    fetch_sec_tickers,
    format_cik,
)


def test_format_cik() -> None:
    assert format_cik(1045810) == "0001045810"
    assert format_cik("cik:0001045810") == "0001045810"
    assert format_cik("CIK1045810") == "0001045810"
    assert cik_key(2488) == "cik:0000002488"
    for bad in ("abc", "12345678901", ""):
        with pytest.raises(ValueError):
            format_cik(bad)


def test_load_groups_share_classes_by_cik(reference: ReferenceData) -> None:
    alphabet = reference.by_ticker("GOOG")
    assert alphabet is not None
    assert alphabet.tickers == ("GOOGL", "GOOG")
    assert reference.by_ticker("googl") == alphabet
    assert reference.by_cik(1652044) == alphabet
    assert alphabet.canonical_key == "cik:0001652044"


def test_share_class_ticker_forms(reference: ReferenceData) -> None:
    brk = reference.by_ticker("BRK.B")
    assert brk is not None and brk.title == "BERKSHIRE HATHAWAY INC"
    assert reference.by_ticker("BRK-A") == brk


def test_by_name_uses_name_key(reference: ReferenceData) -> None:
    [tsm] = reference.by_name("Taiwan Semiconductor Manufacturing Company Limited")
    assert tsm.tickers == ("TSM",)
    assert len(reference.by_name("Twin Name Holdings")) == 2
    assert reference.by_name("Nope Corp") == []


def test_missing_file_is_empty(tmp_path: Path) -> None:
    data = ReferenceData.load(tmp_path / "nope.json")
    assert len(data) == 0
    assert data.by_ticker("NVDA") is None


def test_rejects_malformed_payload() -> None:
    with pytest.raises(ValueError):
        ReferenceData.from_sec_json([1, 2, 3])
    with pytest.raises(ValueError):
        ReferenceData.from_sec_json({"0": {"ticker": "X"}})


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def test_fetch_writes_atomically_with_user_agent(tmp_path: Path) -> None:
    payload = (FIXTURES / "sec_company_tickers.json").read_bytes()
    seen: dict[str, object] = {}

    def opener(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        seen["ua"] = request.get_header("User-agent")
        seen["url"] = request.full_url
        return _FakeResponse(payload)

    dest = tmp_path / "ref" / "sec.json"
    data = fetch_sec_tickers(dest, "me@example.com", opener=opener)
    assert len(data) == len(ReferenceData.from_sec_json(json.loads(payload)))
    assert dest.read_bytes() == payload
    assert "me@example.com" in str(seen["ua"])
    assert str(seen["url"]).startswith("https://www.sec.gov/")
    assert not dest.with_suffix(".json.tmp").exists()


def test_fetch_bad_payload_keeps_existing_cache(tmp_path: Path) -> None:
    dest = tmp_path / "sec.json"
    dest.write_text('{"0": {"cik_str": 1, "ticker": "OLD", "title": "Old Co"}}', "utf-8")

    def opener(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        return _FakeResponse(b"<html>rate limited</html>")

    with pytest.raises(ValueError):
        fetch_sec_tickers(dest, "me@example.com", opener=opener)
    assert ReferenceData.load(dest).by_ticker("OLD") is not None


def test_fetch_requires_contact_email(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        fetch_sec_tickers(tmp_path / "x.json", "anonymous")
