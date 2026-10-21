"""Text normalisation shared by the store, resolver and detector.

* ``normalize_alias``  - case-fold, punctuation -> spaces. Exact alias lookups.
* ``name_key``         - ``normalize_alias`` minus legal suffixes and a leading
                         "the", so "NVIDIA Corp", "Nvidia Corporation" and
                         "nvidia" share a key. Fuzzy company-name lookups.
* ``normalize_ticker`` - upper-case, "$" stripped, share-class "." -> "-"
                         (SEC style: BRK.B -> BRK-B).
"""

from __future__ import annotations

import re

_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)

LEGAL_SUFFIXES = frozenset(
    {
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "co",
        "company",
        "cos",
        "companies",
        "ltd",
        "limited",
        "plc",
        "llc",
        "lp",
        "sa",
        "ag",
        "nv",
        "se",
        "spa",
        "ab",
        "asa",
        "as",
        "oyj",
        "kk",
        "com",
        # SEC state/region tags such as "QUALCOMM INC/DE" or "COSTCO ... /NEW"
        "de",
        "new",
        "uk",
    }
)


def normalize_alias(alias: str) -> str:
    """Case-fold and collapse punctuation/whitespace: 'NVIDIA, Corp.' -> 'nvidia corp'."""
    return _NON_WORD.sub(" ", alias.casefold()).strip()


def name_key(name: str) -> str:
    """Comparable company-name key: 'The Coca-Cola Co' -> 'coca cola'.

    Strips a leading "the" and trailing legal suffixes, but never strips the
    name down to nothing ("CO" stays "co").
    """
    tokens = normalize_alias(name).split()
    if len(tokens) > 1 and tokens[0] == "the":
        tokens = tokens[1:]
    while len(tokens) > 1:
        if tokens[-1] in LEGAL_SUFFIXES:
            tokens.pop()
            continue
        # Dotted/slashed suffixes split into letters: "A/S", "S.A.", "N.V.", "S.p.A."
        n = 0
        while n < len(tokens) - 1 and len(tokens[-1 - n]) == 1:
            n += 1
        if n >= 2 and "".join(tokens[-n:]) in LEGAL_SUFFIXES:
            del tokens[-n:]
            continue
        break
    return " ".join(tokens)


def normalize_ticker(ticker: str) -> str:
    return ticker.strip().lstrip("$").upper().replace(".", "-").replace("/", "-")
