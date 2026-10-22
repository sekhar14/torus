---
name: challenge
<!-- if code -->
description: Stress-test a Torus thesis - a devil's-advocate subagent finds its weakest assumptions, unwatched supplier/customer dependencies and opposing evidence, then proposes risk claims and triggers for the user to approve. Use when the user runs /torus:challenge or asks to red-team, stress-test or argue against a thesis.
<!-- else -->
description: Stress-test a Torus thesis - find its weakest assumptions, unwatched supplier/customer dependencies and opposing evidence, then propose risk claims and triggers for the user to approve. Use when the user asks to challenge, red-team, stress-test or poke holes in a thesis.
<!-- end -->
---

# Challenge a thesis

1. Find the thesis. If the user named it loosely, call `recall` and pick it;
   ask if several match.
<!-- if code -->
2. Delegate to the `torus:thesis-challenger` subagent with the Agent tool. The
   prompt is "Challenge Torus thesis <id>: <title>", plus anything the user
   wants it to focus on.
3. Present its report: the counter-case, the weak points in order, and the
   proposed records. Keep its citations.
<!-- else -->
2. Call `thesis_weak_points` with the thesis id, and `recall` on the thesis's
   subject for the full claims and evidence. `thesis_weak_points` lists:
   - assumptions and their issues (thin sourcing, low confidence, opinions,
     expiring or expired, contradicted, derived from retracted claims)
   - `unwatched_dependencies`: strong supplier or customer links the thesis
     never mentions
   - `text_only_triggers`: triggers with no company attached
   - `opposing_views`: theses with the opposite stance, ruled-out ideas, and
     alerts already judged as weakening it
3. Pick the 2-4 weakest load-bearing points and research the counter-case:
   recent filings (`filing_text`, `filing_diff` on risk factors) and current
   news or data. Check `unwatched_dependencies` with `whatif` to see how trouble
   at a supplier or customer would reach the thesis. Then present:
   - **The counter-case** in one paragraph.
   - **Weak points**, most serious first: the assumption, why it is vulnerable,
     the evidence against it with citations, and how likely it is to fail.
   - **Proposed records** (risk claims and new triggers) for the user to approve.
<!-- end -->
4. Ask which proposed records to keep. For the ones the user approves:
   - risk claims: `record_evidence` for the source, `record_claim`, then
     `update_thesis` with `add_claims` (role `risk`) and a reason like
     "challenge: <weak point>"
   - triggers: `update_thesis` with `add_triggers`
5. Ask whether the challenge changes their view. Change the thesis's
   confidence or status only if the user says so, with `update_thesis` and the
   reason.

The challenger argues one side on purpose. Present it as a stress test, not a
verdict.
