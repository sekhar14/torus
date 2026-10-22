"""What changed between two versions of a filing section, sentence by sentence.

Built for "what's new in the risk factors": filings repeat most of their text
year to year, so the new, dropped and reworded sentences are where the news is.
Sentences are compared after normalising whitespace, quotes and numbers' commas;
a dropped sentence and a new one that are mostly the same wording count as one
*changed* sentence. Short fragments (headings, page numbers) are ignored.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from pydantic import BaseModel

MIN_SENTENCE_CHARS = 30
HEADING_CHARS = 100
CHANGED_RATIO = 0.6
MIN_SHARED_WORDS = 0.4  # cheap pre-filter before the character-level comparison
LEFT_QUOTE, RIGHT_QUOTE = chr(0x201C), chr(0x201D)
_QUOTES = str.maketrans({chr(0x2019): "'", chr(0x2018): "'", LEFT_QUOTE: '"', RIGHT_QUOTE: '"'})
_BULLETS = re.compile("[" + chr(0x2022) + chr(0x25CF) + chr(0x25AA) + "]")
_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+(?=[A-Z(\"])")
_SPACE = re.compile(r"\s+")
_WORD = re.compile(r"[a-z0-9]+")
_PAGE_FURNITURE = re.compile(r"^\s*(\d{1,3}|table of contents|page \d+)\s*$", re.IGNORECASE)
COSMETIC_RATIO = 0.97  # rewordings this close (punctuation, a word) count as unchanged


class Changed(BaseModel):
    before: str
    after: str
    similarity: float


class SectionDiff(BaseModel):
    added: list[str]
    removed: list[str]
    changed: list[Changed]
    unchanged: int
    total_added: int
    total_removed: int
    total_changed: int

    @property
    def is_empty(self) -> bool:
        return not (self.total_added or self.total_removed or self.total_changed)

    @property
    def truncated(self) -> bool:
        shown = (len(self.added), len(self.removed), len(self.changed))
        return shown != (self.total_added, self.total_removed, self.total_changed)


def sentences(text: str) -> list[str]:
    """Sentences of a section, in order, without duplicates or fragments."""
    # Join lines first: page breaks split sentences across blocks. Tables are
    # numbers, not prose, and a bullet starts a new sentence.
    lines = [
        line.strip()
        for line in text.split("\n")
        if line.strip() and "|" not in line and not _PAGE_FURNITURE.match(line)
    ]
    parts = []
    for line, following in zip(lines, [*lines[1:], ""], strict=True):
        parts.append(line)
        # A short unpunctuated line before a capitalised one is a heading, not a
        # sentence broken by a page break ("Risk Factors" / "We depend on...").
        if len(line) < HEADING_CHARS and line[-1] not in ".!?;:," and following[:1].isupper():
            parts.append(".")
    prose = " ".join(parts)
    prose = _SPACE.sub(" ", _BULLETS.sub(". ", prose.translate(_QUOTES)))
    found: dict[str, None] = {}
    for sentence in _SENTENCE_END.split(prose):
        sentence = sentence.strip(" .")
        if len(sentence) >= MIN_SENTENCE_CHARS:
            found[sentence + "."] = None
    return list(found)


def _key(sentence: str) -> str:
    return re.sub(r"(?<=\d),(?=\d{3})", "", sentence.casefold())


def diff_sections(before: str, after: str, max_items: int = 40) -> SectionDiff:
    old, new = sentences(before), sentences(after)
    old_keys, new_keys = {_key(s) for s in old}, {_key(s) for s in new}
    removed = [s for s in old if _key(s) not in new_keys]
    added = [s for s in new if _key(s) not in old_keys]
    unchanged = len(new) - len(added)

    words = {s: set(_WORD.findall(_key(s))) for s in removed + added}
    changed: list[Changed] = []
    remaining_added = list(added)
    still_removed = []
    for sentence in removed:
        best, best_ratio = None, 0.0
        mine = words[sentence]
        for candidate in remaining_added:
            theirs = words[candidate]
            if len(mine & theirs) < MIN_SHARED_WORDS * len(mine | theirs):
                continue
            matcher = SequenceMatcher(None, _key(sentence), _key(candidate), autojunk=False)
            if matcher.real_quick_ratio() < CHANGED_RATIO or matcher.quick_ratio() < CHANGED_RATIO:
                continue
            ratio = matcher.ratio()
            if ratio > best_ratio:
                best, best_ratio = candidate, ratio
        if best is not None and best_ratio >= COSMETIC_RATIO:
            remaining_added.remove(best)
            unchanged += 1
        elif best is not None and best_ratio >= CHANGED_RATIO:
            changed.append(Changed(before=sentence, after=best, similarity=round(best_ratio, 3)))
            remaining_added.remove(best)
        else:
            still_removed.append(sentence)

    changed.sort(key=lambda c: c.similarity)  # most rewritten first
    return SectionDiff(
        added=remaining_added[:max_items],
        removed=still_removed[:max_items],
        changed=changed[:max_items],
        unchanged=unchanged,
        total_added=len(remaining_added),
        total_removed=len(still_removed),
        total_changed=len(changed),
    )


def summarize(diff: SectionDiff, label: str = "Risk factors", top: int = 3) -> str:
    """One paragraph for an event summary: counts and the first new sentences."""
    if diff.is_empty:
        return f"{label}: no changes from the previous filing."
    parts = [
        f"{label}: {diff.total_added} new, {diff.total_changed} reworded,"
        f" {diff.total_removed} dropped sentence(s) since the previous filing."
    ]
    news = diff.added[:top] or [c.after for c in diff.changed[:top]]
    if news:
        parts.append("New: " + " ".join(f"{LEFT_QUOTE}{s}{RIGHT_QUOTE}" for s in news))
    return " ".join(parts)
