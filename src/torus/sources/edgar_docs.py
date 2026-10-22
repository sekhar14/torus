"""Read SEC filings as text, split into their items (Business, Risk Factors, MD&A...).

* finds a company's filings through the submissions API the EDGAR watcher uses
* downloads the primary document politely (SEC User-Agent, rate limited) and
  converts its HTML to plain text, dropping scripts, styles and hidden inline-XBRL
  headers; table cells are joined with `` | ``
* splits 10-K, 10-Q and 20-F text into items. A table of contents repeats every
  heading, so for each item the occurrence that starts the longest section wins.
* caches converted text under ``<cache_dir>/<cik>/<accession>.txt``

Agents ask for sections by friendly name (``risk_factors``, ``mdna``,
``business``...) or by item id (``1A``; ``II-1A`` for 10-Q part II).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from torus.core.reference import format_cik
from torus.watchers.edgar import SUBMISSIONS_URL, Filing, parse_submissions
from torus.watchers.http import Fetcher, FetchError, RateLimiter, user_agent

MIN_INTERVAL = 0.125
MAX_HEADING_CHARS = 160

# Friendly section names -> item ids, per base form.
SECTION_NAMES: dict[str, dict[str, str]] = {
    "10-K": {
        "business": "1",
        "risk_factors": "1A",
        "cybersecurity": "1C",
        "properties": "2",
        "legal": "3",
        "market": "5",
        "mdna": "7",
        "market_risk": "7A",
        "financials": "8",
        "controls": "9A",
    },
    "10-Q": {
        "financials": "I-1",
        "mdna": "I-2",
        "market_risk": "I-3",
        "controls": "I-4",
        "legal": "II-1",
        "risk_factors": "II-1A",
    },
    "20-F": {
        "key_information": "3",
        "risk_factors": "3",
        "business": "4",
        "mdna": "5",
        "management": "6",
        "shareholders": "7",
        "financials": "8",
    },
}
SECTION_NAMES["40-F"] = {}
SPLIT_FORMS = frozenset(SECTION_NAMES)

_HIDDEN_TAGS = frozenset({"script", "style", "head", "title", "ix:header", "noscript"})
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "br",
        "tr",
        "li",
        "ul",
        "ol",
        "table",
        "section",
        "article",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "center",
        "blockquote",
        "pre",
    }
)
_CELL_TAGS = frozenset({"td", "th"})
_VOID_TAGS = frozenset({"br", "hr", "img", "input", "meta", "link", "col", "wbr", "area", "base"})
_DISPLAY_NONE = re.compile(r"display\s*:\s*none", re.IGNORECASE)
_SPACES = re.compile(r"[ \t\r\f\v\u00a0\u2002\u2003\u2009\u200b]+")
_ITEM = re.compile(r"^item\s*(\d{1,2}[a-d]?)(?![0-9a-z])\s*[.:\-\u2013\u2014]?", re.IGNORECASE)
_PART = re.compile(r"^part\s+(iv|i{1,3})(?![a-z])", re.IGNORECASE)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.line: list[str] = []
        self.hidden_depth = 0
        self.stack: list[tuple[str, bool]] = []  # (tag, opened a hidden region)

    def _break(self) -> None:
        text = _SPACES.sub(" ", "".join(self.line)).strip()
        if text.strip("| "):
            self.lines.append(text)
        elif self.lines and self.lines[-1] != "":
            self.lines.append("")
        self.line = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        style = next((v for k, v in attrs if k == "style" and v), "")
        hides = tag in _HIDDEN_TAGS or bool(_DISPLAY_NONE.search(style))
        if tag not in _VOID_TAGS:
            self.stack.append((tag, hides))
            if hides:
                self.hidden_depth += 1
        if self.hidden_depth:
            return
        if tag in _BLOCK_TAGS:
            self._break()
        elif tag in _CELL_TAGS and "".join(self.line).strip():
            self.line.append(" | ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self.hidden_depth and tag in _BLOCK_TAGS:
            self._break()

    def handle_endtag(self, tag: str) -> None:
        # Pop to the matching start tag; tolerate the unclosed tags real filings have.
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                for _, hides in self.stack[i:]:
                    if hides:
                        self.hidden_depth -= 1
                del self.stack[i:]
                break
        else:
            return
        if not self.hidden_depth and tag in _BLOCK_TAGS:
            self._break()
        elif not self.hidden_depth and tag in _CELL_TAGS:
            self.line.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.line.append(data)

    def text(self) -> str:
        self._break()
        return "\n".join(self.lines).strip()


def decode(body: bytes) -> str:
    """Filings are UTF-8 or, often, Windows-1252 (curly quotes as 0x92)."""
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("cp1252", errors="replace")


def html_to_text(html: str) -> str:
    """Readable plain text from a filing's HTML (or the text itself if not HTML)."""
    if "<" not in html[:2000] and "<html" not in html.lower():
        return "\n".join(_SPACES.sub(" ", line).strip() for line in html.splitlines()).strip()
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()


@dataclass(frozen=True)
class Heading:
    line: int
    key: str  # "1A", or "II-1A" for 10-Q


def split_items(text: str, form: str) -> dict[str, str]:
    """Item id -> section text (heading included). Empty when the form isn't split."""
    base = form.upper().removesuffix("/A")
    if base not in SPLIT_FORMS:
        return {}
    by_part = base == "10-Q"
    lines = text.split("\n")
    headings: list[Heading] = []
    part = "I"
    for i, raw in enumerate(lines):
        line = raw.strip().lstrip("|").strip()
        if len(line) > MAX_HEADING_CHARS:
            continue
        part_match = _PART.match(line)
        if part_match:
            part = part_match.group(1).upper()
            # "PART II - OTHER INFORMATION | Item 1. Legal Proceedings" on one line
            line = line[part_match.end() :].lstrip(" .:-\u2013\u2014|")
        item_match = _ITEM.match(line)
        if item_match:
            item = item_match.group(1).upper()
            headings.append(Heading(i, f"{part}-{item}" if by_part else item))
    best: dict[str, tuple[int, int, int]] = {}  # key -> (start line, end line, chars)
    for n, heading in enumerate(headings):
        end = headings[n + 1].line if n + 1 < len(headings) else len(lines)
        size = sum(len(line) for line in lines[heading.line : end])
        if heading.key not in best or size > best[heading.key][2]:
            best[heading.key] = (heading.line, end, size)
    ordered = sorted(best.items(), key=lambda kv: kv[1][0])
    return {key: "\n".join(lines[start:end]).strip() for key, (start, end, _) in ordered}


def section_key(form: str, section: str) -> str:
    """Item id for a friendly name or an item id, e.g. ("10-K", "risk_factors") -> "1A"."""
    base = form.upper().removesuffix("/A")
    names = SECTION_NAMES.get(base, {})
    wanted = section.strip()
    if wanted.lower() in names:
        return names[wanted.lower()]
    key = re.sub(r"(?i)^item\s*", "", wanted).upper()
    if base == "10-Q" and "-" not in key:
        key = f"I-{key}"
    return key


@dataclass(frozen=True)
class FilingDoc:
    company: str
    filing: Filing
    text: str
    sections: dict[str, str]

    def section(self, name: str) -> str:
        key = section_key(self.filing.form, name)
        if key in self.sections:
            return self.sections[key]
        names = SECTION_NAMES.get(self.filing.form.upper().removesuffix("/A"), {})
        friendly = sorted(n for n, k in names.items() if k in self.sections)
        mapped = {names[n] for n in friendly}
        known = friendly + [f"item {k}" for k in self.sections if k not in mapped]
        raise ValueError(
            f"{self.filing.form} {self.filing.accession} has no section {name!r};"
            f" available: {', '.join(known) or 'none (use the full text)'}"
        )


def page(text: str, offset: int = 0, max_chars: int = 20_000) -> tuple[str, int | None]:
    """A slice of ``text`` ending on a line break where possible, and the next offset."""
    offset = max(0, offset)
    if offset >= len(text):
        return "", None
    end = offset + max_chars
    if end >= len(text):
        return text[offset:], None
    cut = text.rfind("\n", offset + max_chars // 2, end)
    end = cut if cut > 0 else end
    return text[offset:end], end


def base_form(form: str) -> str:
    return form.upper().removesuffix("/A")


class EdgarDocs:
    def __init__(
        self,
        fetcher: Fetcher,
        contact: str,
        cache_dir: Path,
        limiter: RateLimiter | None = None,
    ) -> None:
        if "@" not in contact:
            raise ValueError("the SEC requires a contact email in the User-Agent")
        self.fetcher = fetcher
        self.headers = {"User-Agent": user_agent(contact)}
        self.cache_dir = cache_dir
        self.limiter = limiter or RateLimiter(MIN_INTERVAL)

    def _get(self, url: str) -> bytes:
        self.limiter.wait()
        response = self.fetcher.get(url, self.headers)
        if not response.ok:
            raise ValueError(f"HTTP {response.status} for {url}")
        return response.body

    def filings(
        self,
        cik: str,
        forms: Sequence[str],
        limit: int = 10,
        include_amendments: bool = False,
    ) -> tuple[str, list[Filing]]:
        """(company name, matching filings newest first)."""
        cik = format_cik(cik)
        company, filings = parse_submissions(json.loads(self._get(SUBMISSIONS_URL.format(cik=cik))))
        wanted = {base_form(f) for f in forms}
        found = [
            f
            for f in filings
            if base_form(f.form) in wanted
            and (include_amendments or not f.form.upper().endswith("/A"))
            and f.primary_document
        ]
        found.sort(key=lambda f: (f.filed, f.accession), reverse=True)
        return company, found[:limit]

    def text(self, filing: Filing) -> str:
        """The filing's primary document as text (downloaded once, then cached)."""
        path = self.cache_path(filing)
        if path.is_file():
            return path.read_text("utf-8")
        text = html_to_text(decode(self._get(filing.url)))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8")
        return text

    def cache_path(self, filing: Filing) -> Path:
        return self.cache_dir / filing.cik / f"{filing.accession}.txt"

    def document(self, company: str, filing: Filing) -> FilingDoc:
        text = self.text(filing)
        return FilingDoc(company, filing, text, split_items(text, filing.form))

    def latest(self, cik: str, form: str, index: int = 0) -> FilingDoc:
        """The ``index``-th most recent filing of ``form`` (0 = latest)."""
        company, found = self.filings(cik, [form], limit=index + 1)
        if len(found) <= index:
            raise ValueError(f"no {form} filing #{index + 1} for CIK {format_cik(cik)}")
        return self.document(company, found[index])


def previous_filing(filing: Filing, filings: Sequence[Filing]) -> Filing | None:
    """The same company's previous original filing of the same form, if listed."""
    form = base_form(filing.form)
    earlier = [
        f
        for f in filings
        if base_form(f.form) == form
        and not f.form.upper().endswith("/A")
        and f.primary_document
        and (f.filed, f.accession) < (filing.filed, filing.accession)
    ]
    return max(earlier, key=lambda f: (f.filed, f.accession), default=None)


def risk_change_summary(
    docs: EdgarDocs, company: str, filing: Filing, filings: Sequence[Filing]
) -> str | None:
    """ "Risk factors: N new..." for a 10-K or 10-Q against the previous one, or None
    when there is nothing to compare (no previous filing, no risk factors section)."""
    from torus.core.filing_diff import diff_sections, summarize

    if base_form(filing.form) not in ("10-K", "10-Q", "20-F") or filing.form.endswith("/A"):
        return None
    previous = previous_filing(filing, filings)
    if previous is None:
        return None
    try:
        before = docs.document(company, previous).section("risk_factors")
        after = docs.document(company, filing).section("risk_factors")
    except (ValueError, FetchError):
        return None
    return summarize(diff_sections(before, after))
