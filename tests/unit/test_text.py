from __future__ import annotations

import pytest

from torus.core.text import name_key, normalize_alias, normalize_ticker


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("NVIDIA CORP", "nvidia"),
        ("Nvidia Corporation", "nvidia"),
        ("NVIDIA, Inc.", "nvidia"),
        ("nvidia", "nvidia"),
        ("The Coca-Cola Co", "coca cola"),
        ("COCA COLA CO", "coca cola"),
        ("TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD", "taiwan semiconductor manufacturing"),
        ("JPMORGAN CHASE & CO", "jpmorgan chase"),
        ("AMAZON COM INC", "amazon"),
        ("QUALCOMM INC/DE", "qualcomm"),
        ("COSTCO WHOLESALE CORP /NEW", "costco wholesale"),
        ("Arm Holdings plc /UK", "arm holdings"),
        ("NOVO NORDISK A S", "novo nordisk"),
        ("Novo Nordisk A/S", "novo nordisk"),
        ("Banco Santander, S.A.", "banco santander"),
        ("AT&T INC.", "at t"),
        ("Merck & Co., Inc.", "merck"),
        ("The", "the"),
        ("Co", "co"),
        ("", ""),
    ],
)
def test_name_key(name: str, key: str) -> None:
    assert name_key(name) == key


def test_name_key_never_strips_to_empty() -> None:
    assert name_key("Company Inc") == "company"


@pytest.mark.parametrize(
    ("raw", "norm"),
    [("$nvda", "NVDA"), ("BRK.B", "BRK-B"), ("brk/b", "BRK-B"), (" TSM ", "TSM")],
)
def test_normalize_ticker(raw: str, norm: str) -> None:
    assert normalize_ticker(raw) == norm


def test_normalize_alias_keeps_suffixes() -> None:
    assert normalize_alias("NVIDIA, Corp.") == "nvidia corp"
