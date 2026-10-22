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


# Legal suffixes with a conventional spelling other than plain title case.
_SUFFIX_CASE = {
    "INC": "Inc",
    "LTD": "Ltd",
    "PLC": "plc",
    "SPA": "SpA",
    "OYJ": "Oyj",
    **{s: s for s in ("LLC", "LP", "NV", "SA", "AG", "SE", "AB", "ASA", "KK")},
}
# Short words that are words, not acronyms; the first three stay lower case mid-name.
_SHORT_WORDS = frozenset(
    {
        *("OF", "THE", "AND", "CO", "COM", "NEW", "OIL", "GAS", "BIO", "ONE", "TWO", "AIR"),
        *("SEA", "BAY", "BIG", "RED", "SUN", "CAR", "ART", "NET", "WEB", "BOX", "TOP", "FOX"),
    }
)


def display_name(name: str, tickers: tuple[str, ...] | list[str] = ()) -> str:
    """A readable company name from an all-caps SEC title.

    'TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD' -> 'Taiwan Semiconductor Manufacturing Co
    Ltd', 'KLA CORP' -> 'KLA Corp', and a word that is one of the company's ``tickers``
    stays upper case ('ASML HOLDING NV' -> 'ASML Holding NV'). Names with any lower-case
    letter are already deliberate and come back unchanged.
    """
    name = " ".join(name.split())
    if not name or any(c.islower() for c in name):
        return name
    keep = {t.upper() for t in tickers}
    words = []
    for i, word in enumerate(name.split(" ")):
        bare = word.rstrip(".,")
        tail = word[len(bare) :]
        if bare in _SUFFIX_CASE:
            words.append(_SUFFIX_CASE[bare] + tail)
        elif bare in keep or not bare.replace("-", "").replace("'", "").isalpha():
            words.append(word)  # the ticker itself, or 3M, AT&T, /DE/: leave as filed
        elif len(bare) <= 3 and bare not in _SHORT_WORDS:
            words.append(word)  # KLA, AMD, USA: acronyms
        elif i > 0 and bare in {"OF", "THE", "AND"}:
            words.append(word.lower())
        else:
            words.append("-".join(_cap(p) for p in bare.split("-")) + tail)
    return " ".join(words)


def _cap(word: str) -> str:
    if len(word) > 3 and word.startswith("MC"):
        return "Mc" + word[2:].capitalize()
    return word.capitalize()
