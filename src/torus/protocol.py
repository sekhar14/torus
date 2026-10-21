"""What the research agent should record, per capture mode.

Shared by the MCP server's instructions; the Claude Code plugin's
``research-protocol`` skill states the same rules in long form.
"""

from __future__ import annotations

from torus.config import CaptureMode

BASE = """\
Torus is a persistent research memory shared across sessions.

Before researching a company or topic, call `recall` to see prior theses, claims and
rejected hypotheses, and build on them instead of starting over.

Entities may be given as tickers ("NVDA", "$NVDA"), CIKs, names or entity ids.
Correct earlier claims with `supersede_claim` / `retract_claim`, never new copies.
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


def instructions(mode: CaptureMode) -> str:
    return BASE + "\n" + RULES[mode]
