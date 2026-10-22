---
name: review
description: Review open Torus alerts - new events (filings, news) that may affect existing investment theses - and record whether each supports, weakens or breaks the thesis. Use when the user runs /torus:review or asks which theses are affected by recent news.
---

# Review alerts

When an event is added to Torus (by a watcher, the user or you), Torus walks the
research graph from the companies involved, through their suppliers, customers
and competitors, to find theses that rest on them, and opens an **alert** for
each. Torus only finds the connection. Deciding what the event *means* is your
job.

Each alert shows how its score was built:
`graph score x materiality x (1 + trigger match)`. The graph score says how
closely the event's companies are tied to the thesis. Materiality says what kind
of event it is (a bankruptcy 8-K is 1.0, a routine 10-Q 0.4, a listicle 0.2;
the event's `materiality_reason` says why). The trigger match says how much of
one of the thesis's triggers the event's wording covers (`matched_trigger`).

1. Call `list_alerts` with `grouped: true`. If there are none, say so and stop.
2. Take the groups in order, highest score first, and judge each thesis once
   against all of its events. For each alert:
   - Read the event: title, summary, and the `uri` (fetch it if it's a URL and
     the summary isn't enough).
   - Read the paths, which say why Torus connected the event to the thesis, e.g.
     `TSMC -SUPPLIES-> NVIDIA CORP via assumption claim`.
   - Call `recall` for the thesis's subject to see its assumptions, evidence and
     current confidence.
   - Decide, and call `resolve_alert`:
     - `supports`: the event backs an assumption. You may raise confidence.
     - `weakens`: it undercuts an assumption without disproving it. An active
       thesis becomes `at_risk`. Lower the confidence.
     - `breaks`: an assumption is now false. The thesis becomes `broken`.
     - `unrelated`: the connection is coincidental. The alert is dismissed and
       the thesis is untouched.
   - Write a rationale that cites the event and the specific claim it bears on.
3. If the event adds a new fact, also `record_evidence` and `record_claim`, and
   link the claim with `update_thesis` (`add_claims`, role `supporting` or
   `risk`). If the alert reveals a new warning sign, add it with
   `update_thesis` (`add_triggers`).
4. If a thesis keeps getting the same kind of `unrelated` alert (say, every
   10-Q from one supplier), offer the user a mute with `update_thesis`
   (`add_mutes`, e.g. `{"entity": "TSM", "form": "10-Q"}`) or a higher
   `alert_threshold` for that thesis. Don't mute without asking.
5. Finish with a short table: alert, thesis, verdict, confidence before → after.

Be conservative. One news item rarely breaks a thesis. Prefer `weakens` unless
the event directly contradicts an assumption.

`alert_stats` shows how often alerts were judged related, by source, form, hop
count and materiality. Mention it if the user asks whether alerts are useful.
