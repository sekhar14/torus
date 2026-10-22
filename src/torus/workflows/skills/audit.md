---
name: audit
<!-- if code -->
description: Health check of the Torus research memory - find expired or thinly sourced assumptions, theses past their horizon or gone stale, claims that may conflict, and unconfirmed starter links, then fix them with the user. Use when the user runs /torus:audit or asks whether their research is still current or trustworthy.
<!-- else -->
description: Health check of the Torus research memory - find expired or thinly sourced assumptions, theses past their horizon or gone stale, claims that may conflict, and unconfirmed starter links, then fix them with the user. Use when the user asks whether their research is still current or trustworthy, or asks to audit their Torus graph.
<!-- end -->
---

# Audit the research graph

Old or wrong entries don't just sit in the graph: recall puts them into future
prompts, and alerts are built on them. The audit finds what needs attention.
Torus finds the problems; you and the user decide what to do about them.

1. Call `audit`. If there are no findings, say the graph looks healthy and stop.
2. Summarise by severity in two or three lines, e.g. "2 high (a thesis past its
   horizon, an expired assumption), 3 medium, 9 low". Then work through the
   findings, high first. Group related findings for the same thesis.
3. For each finding, read what's needed (`recall`, `timeline`, or the claim's
   sources) and propose a fix in one line. Apply it only when the user agrees:
   - **past_horizon**: ask whether the thesis played out. Use `update_thesis`
     with status `confirmed` or `broken` (with the reason), or a new `horizon`.
   - **expired_assumption**: find current data (`filing_text` for filings, or
     research), then `supersede_claim` with new values and validity dates. Or
     re-judge the thesis if the assumption no longer holds.
   - **single_source_assumption** and **opinion_assumption**: look for a
     primary source. `record_evidence` plus `supersede_claim` with the extra
     evidence, or lower the claim's confidence.
   - **possible_conflict**: these are candidates. Read both claims and their
     sources. Supersede or retract the wrong one, or set `valid_from` /
     `valid_to` if both were true at different times. Say so if they don't
     actually conflict.
   - **retracted_basis**: re-derive the inference from current claims, or
     retract it.
   - **stale_thesis**: offer a quick refresh: recent filings and news on its
     subject, then confirm or revise it.
   - **unconfirmed_starter_link**: confirm the link from a filing
     (`filing_text`, then `record_relation`) or correct it. A few at a time is
     fine.
   - **orphan_entity**: usually harmless. Mention the count; don't fix them one
     by one.
4. Finish with what changed: claims superseded or retracted, theses updated,
   links confirmed, and what's left for another time.

Never delete or rewrite history: corrections supersede or retract, so Torus can
still say what was believed on any past date.
