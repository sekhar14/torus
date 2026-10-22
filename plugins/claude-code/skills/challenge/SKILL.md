---
name: challenge
description: Stress-test a Torus thesis - a devil's-advocate subagent finds its weakest assumptions, unwatched supplier/customer dependencies and opposing evidence, then proposes risk claims and triggers for the user to approve. Use when the user runs /torus:challenge or asks to red-team, stress-test or argue against a thesis.
---

# Challenge a thesis

1. Find the thesis. If the user named it loosely, call `recall` and pick it;
   ask if several match.
2. Delegate to the `torus:thesis-challenger` subagent with the Agent tool. The
   prompt is "Challenge Torus thesis <id>: <title>", plus anything the user
   wants it to focus on.
3. Present its report: the counter-case, the weak points in order, and the
   proposed records. Keep its citations.
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
