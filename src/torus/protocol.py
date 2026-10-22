"""What the research agent should record, per capture mode and harness.

Shared by the MCP server's instructions; the Claude Code plugin's
``research-protocol`` skill states the same rules in long form. Harnesses
without hooks or a background curator (Claude Desktop, Codex) receive the full
recall, live-capture and turn-receipt rules directly in the server instructions.
"""

from __future__ import annotations

from torus.config import CaptureMode, has_curator

BASE = """\
Torus is a persistent research memory shared across sessions.

Before researching a company or topic, call `recall` to see prior theses, claims and
rejected hypotheses, and build on them instead of starting over.

Entities may be given as tickers ("NVDA", "$NVDA"), CIKs, names or entity ids.
Correct earlier claims with `supersede_claim` / `retract_claim`, never new copies.
"""

STANDALONE = """\
## Standalone harness rules (no hooks or background curator)
- **Start with `brief` or `recall`.** At the start of any company or stock question,
  call `brief` with the user's question (or `recall` with the tickers) so prior
  theses, claims, open alerts and ruled-out ideas are loaded before you answer.
- **Read primary filings.** Prefer `filing_text` and `filing_diff` for SEC filings,
  citing the filing `url` with `kind: "filing"` and a short verbatim `quote`.
- **Write triggers in headline words.** When calling `record_thesis`, attach
  concrete invalidation `triggers` (with `entity` set to the company watched) and a
  horizon such as `"12m"` or `"FY2027"`.
- **Nothing is captured after the chat.** No curator reads this conversation later.
  If the user asks to save the chat, record the sources, claims, links, theses and
  ruled-out ideas it contains that `recent_records` doesn't list yet.
- **End with a receipt.** Whenever you record or update anything in Torus during a
  turn, finish your reply with one line stating what was saved (e.g.
  "Torus recorded 1 thesis, 3 claims and 2 sources this turn — ask to undo any of it.").
"""

RULES = {
    CaptureMode.LIVE: """\
Capture mode: live. Record as you go:
1. `record_evidence` for every source you rely on (URL, title, a short quote).
2. `record_claim` for each atomic finding, citing evidence ids. Facts and metrics
   must cite evidence; inferences cite evidence or the claims they derive from.
3. `record_relation` for supplier, customer, competitor and exposure links.
4. `record_thesis` for each conclusion, linking the claims it assumes.
5. `record_rejected_hypothesis` for ideas you checked and dropped.
""",
    CaptureMode.BALANCED: """\
Capture mode: balanced. Record only what needs your judgement; a curator extracts
supporting detail from the transcript after the session:
1. `record_thesis` for each conclusion, with its key assumption claims.
2. `record_claim` (+ `record_evidence`) for the few claims a thesis rests on.
3. `record_rejected_hypothesis` for ideas you checked and dropped.
Cite sources (URLs) in your answer so the curator can link them.
""",
    CaptureMode.CURATOR: """\
Capture mode: curator. Focus on the research; a curator extracts claims and
evidence from the transcript afterwards. Record only:
1. `record_thesis` for each conclusion you reach.
2. `record_rejected_hypothesis` for ideas you checked and dropped.
Cite sources (URLs) in your answer so the curator can link them.
""",
}


def instructions(mode: CaptureMode, harness: str = "claude-code") -> str:
    text = BASE + "\n" + RULES[mode]
    if not has_curator(harness):
        text += "\n" + STANDALONE
    return text
