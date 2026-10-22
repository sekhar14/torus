"""Filing text extraction and item splitting (recorded responses, no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from edgar_support import ACME_CIK, FILINGS, FakeFetcher, acme_docs, acme_routes
from torus.sources.edgar_docs import decode, html_to_text, page, section_key, split_items


def _text(name: str) -> str:
    return html_to_text(decode((FILINGS / name).read_bytes()))


def test_html_to_text_drops_hidden_content_and_keeps_tables() -> None:
    text = _text("acme-10k-2025.htm")
    assert "EntityRegistrantName" not in text  # hidden inline XBRL header
    assert "should never appear" not in text  # script
    assert "p { margin" not in text  # style
    assert "Revenue | $ | 12,000" in text
    assert "Management\u2019s Discussion" in text  # &#146; and &#8217; both become \u2019


def test_cp1252_bytes_decode() -> None:
    assert "Café “quoted” text" in _text("cp1252.htm")


def test_split_10k_prefers_body_over_table_of_contents() -> None:
    sections = split_items(_text("acme-10k-2025.htm"), "10-K")
    assert list(sections) == ["1", "1A", "7", "8"]
    assert sections["1"].startswith("Item 1. Business")
    assert "sole foundry" in sections["1"]
    assert "HBM memory prices" in sections["1A"]
    assert "Item 7" not in sections["1A"]


def test_split_10q_tracks_parts() -> None:
    sections = split_items(_text("acme-10q.htm"), "10-Q")
    assert set(sections) == {"I-1", "I-2", "II-1", "II-1A"}
    assert "export rules" in sections["II-1A"]
    assert "35%" in sections["I-2"]


def test_unsplit_forms() -> None:
    assert split_items("Item 2.02 Results", "8-K") == {}


def test_section_key() -> None:
    assert section_key("10-K", "risk_factors") == "1A"
    assert section_key("10-K", "Item 7") == "7"
    assert section_key("10-Q", "mdna") == "I-2"
    assert section_key("10-Q", "1A") == "I-1A"
    assert section_key("10-Q", "ii-1a") == "II-1A"
    assert section_key("20-F", "business") == "4"


def test_page_breaks_on_lines() -> None:
    text = "\n".join(f"line {i}" for i in range(100))
    chunk, nxt = page(text, 0, 50)
    assert chunk.endswith(("line 6", "line 7")) and nxt is not None
    rest, end = page(text, nxt, 10_000)
    assert end is None and (chunk + rest) == text


def test_latest_and_previous_filings_are_cached(tmp_path: Path) -> None:
    fetcher = FakeFetcher(acme_routes())
    docs = acme_docs(tmp_path, fetcher)
    latest = docs.latest(ACME_CIK, "10-K")
    assert latest.company == "ACME CHIPS INC"
    assert latest.filing.accession == "0000999001-26-000010"
    assert "sole foundry" in latest.section("business")
    previous = docs.latest(ACME_CIK, "10-K", index=1)
    assert "two foundries" in previous.section("risk_factors")
    downloads = [u for u in fetcher.calls if u.endswith(".htm")]
    docs.latest(ACME_CIK, "10-K")
    assert [u for u in fetcher.calls if u.endswith(".htm")] == downloads  # cached
    assert docs.cache_path(latest.filing).is_file()

    with pytest.raises(ValueError, match="available: business"):
        latest.section("legal")
    with pytest.raises(ValueError, match="no 10-K filing #3"):
        docs.latest(ACME_CIK, "10-K", index=2)
