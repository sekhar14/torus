from __future__ import annotations

import pytest

from torus.core.text import display_name, name_key, normalize_alias, normalize_ticker


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


@pytest.mark.parametrize(
    ("title", "tickers", "shown"),
    [
        (
            "TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD",
            ("TSM",),
            "Taiwan Semiconductor Manufacturing Co Ltd",
        ),
        ("NVIDIA CORP", ("NVDA",), "Nvidia Corp"),
        ("ASML HOLDING NV", ("ASML",), "ASML Holding NV"),
        ("KLA CORP", ("KLAC",), "KLA Corp"),
        ("BANK OF AMERICA CORP /DE/", ("BAC",), "Bank of America Corp /DE/"),
        ("AT&T INC.", ("T",), "AT&T Inc."),
        ("3M CO", ("MMM",), "3M Co"),
        ("COCA-COLA CO", ("KO",), "Coca-Cola Co"),
        ("MCDONALD'S CORP", ("MCD",), "McDonald's Corp"),
        ("ARM HOLDINGS PLC /UK", ("ARM",), "ARM Holdings plc /UK"),
        ("SK hynix Inc", (), "SK hynix Inc"),
        ("", (), ""),
    ],
)
def test_display_name(title: str, tickers: tuple[str, ...], shown: str) -> None:
    assert display_name(title, tickers) == shown
    assert name_key(shown) == name_key(title)
