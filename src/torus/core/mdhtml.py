"""Markdown to a self-contained HTML page, for memos and digests. No dependencies: it
handles the subset of Markdown that Torus's own renderers write."""

from __future__ import annotations

import html
import re

_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")


def _inline(text: str) -> str:
    escaped = html.escape(text, quote=False)
    escaped = _LINK.sub(
        lambda m: (
            f'<a href="{html.escape(m.group(2))}">{m.group(1)}</a>'
            if m.group(2).startswith(("http://", "https://"))
            else m.group(1)
        ),
        escaped,
    )
    escaped = _BOLD.sub(r"<strong>\1</strong>", escaped)
    return _ITALIC.sub(r"<em>\1</em>", escaped)


STYLE = """
:root {
  --bg:#fbfaf7;
  --fg:#1d1d1b;
  --muted:#6b6a65;
  --rule:#e2e0d9;
  --accent:#1f5f8b;
  --quote:#f1efe8;
}
@media (prefers-color-scheme: dark) {
  :root {
  --bg:#17181a;
  --fg:#e8e6e1;
  --muted:#a19f99;
  --rule:#2e3034;
  --accent:#7fb4dc;
  --quote:#212327;
}
}
body {
  background:var(--bg);
  color:var(--fg);
  margin:0;
  font:16px/1.55 ui-serif, Georgia, "Times New Roman", serif;
}
main {
  max-width:760px;
  margin:0 auto;
  padding:40px 16px 64px;
}
h1 {
  font-size:2rem;
  line-height:1.2;
  margin:0 0 12px;
}
h2 {
  font:600 0.8rem/1.4 ui-sans-serif, system-ui, sans-serif;
  text-transform:uppercase;
  letter-spacing:0.08em;
  color:var(--muted);
  margin:36px 0 12px;
  border-bottom:1px solid var(--rule);
  padding-bottom:6px;
}
p, li, td, th {
  overflow-wrap:anywhere;
}
blockquote {
  font-size:1.15rem;
  margin:16px 0;
  padding:12px 16px;
  border-left:3px solid var(--accent);
  background:var(--quote);
}
a {
  color:var(--accent);
}
ol, ul {
  padding-left:22px;
} li {
  margin:0 0 12px;
}
li > div + div {
  font:0.88rem/1.5 ui-sans-serif, system-ui, sans-serif;
  color:var(--muted);
}
.quote {
  font-style:italic;
  border-left:2px solid var(--rule);
  padding-left:8px;
  margin:2px 0;
}
table {
  width:100%;
  border-collapse:collapse;
  font:0.88rem/1.4 ui-sans-serif, system-ui, sans-serif;
}
th, td {
  text-align:left;
  padding:6px 8px;
  border-bottom:1px solid var(--rule);
  vertical-align:top;
}
hr {
  border:0;
  border-top:1px solid var(--rule);
  margin:32px 0 12px;
}
em {
  color:var(--muted);
}
"""


def markdown_to_html(markdown: str, title: str) -> str:
    """Markdown as written by Torus renderers, as one self-contained HTML page (light and
    dark). Handles what they use: headings, quotes, tables, lists, rules, links, emphasis."""
    body: list[str] = []
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("# "):
            body.append(f"<h1>{_inline(line[2:])}</h1>")
        elif line.startswith("## "):
            body.append(f"<h2>{_inline(line[3:])}</h2>")
        elif line.startswith("> "):
            body.append(f"<blockquote>{_inline(line[2:])}</blockquote>")
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append(lines[i])
                i += 1
            cells = [[c.strip() for c in re.split(r"(?<!\\)\|", r)[1:-1]] for r in rows]
            head = "".join(f"<th>{_inline(c)}</th>" for c in cells[0])
            rest = "".join(
                "<tr>"
                + "".join(f"<td>{_inline(c.replace(chr(92) + '|', '|'))}</td>" for c in r)
                + "</tr>"
                for r in cells[2:]
            )
            body.append(f"<table><thead><tr>{head}</tr></thead><tbody>{rest}</tbody></table>")
            continue
        elif re.match(r"^\d+\. ", line):
            items = []
            while i < len(lines) and (re.match(r"^\d+\. ", lines[i]) or lines[i].startswith("   ")):
                if re.match(r"^\d+\. ", lines[i]):
                    items.append([re.sub(r"^\d+\. ", "", lines[i])])
                else:
                    items[-1].append(lines[i].strip())
                i += 1
            parts = []
            for item in items:
                details = "".join(
                    f'<div class="quote">{_inline(d[2:])}</div>'
                    if d.startswith("> ")
                    else f"<div>{_inline(d[2:] if d.startswith('- ') else d)}</div>"
                    for d in item[1:]
                )
                parts.append(f"<li><div>{_inline(item[0])}</div>{details}</li>")
            body.append(f"<ol>{''.join(parts)}</ol>")
            continue
        elif line.startswith("- "):
            bullets = []
            while i < len(lines) and lines[i].startswith("- "):
                bullets.append(f"<li>{_inline(lines[i][2:])}</li>")
                i += 1
            body.append(f"<ul>{''.join(bullets)}</ul>")
            continue
        elif line == "---":
            body.append("<hr>")
        elif line.strip():
            body.append(f"<p>{_inline(line)}</p>")
        i += 1
    title = html.escape(title)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
{STYLE}</style></head>
<body><main>
{chr(10).join(body)}
</main></body></html>
"""
