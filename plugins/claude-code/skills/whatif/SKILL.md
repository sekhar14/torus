---
name: whatif
description: Scenario analysis on the Torus research graph - which theses a hypothetical event (a fab outage, export rule, CEO exit, lost customer) would reach, through which supplier/customer/competitor links, and what it would mean for each. Use when the user runs /torus:whatif or asks "what happens to my theses if X".
---

# What if...?

Torus can run its alerting on an event that hasn't happened. It finds every open
thesis the event would reach and why. You then reason about what it would
*mean* for each one. Nothing is recorded.

1. Turn the user's scenario into a one-line description and the companies it is
   about, e.g. "TSMC fab outage halts CoWoS packaging for 3 months" about
   `TSM`. If the scenario hits several companies (a rule on all Chinese
   foundries), list each one.
2. Call `whatif` with `entities` and `description`. Pass `materiality` only if
   the user says how severe it is. Otherwise Torus reads it from the wording.
3. For each hit, highest score first:
   - Say how the event reaches the thesis (its `paths`, e.g.
     `TSMC -SUPPLIES-> NVIDIA via assumption claim`) and whether it would raise
     an alert (`would_alert`) or fall just under the threshold.
   - Call `recall` on the thesis's subject if you need its assumptions.
   - Reason about direction: does it hurt or help this thesis? A supplier outage
     hurts a bullish thesis on the customer, and may help one on a competitor
     with other suppliers. Say which assumption it hits and roughly how hard.
4. Finish with a short table: thesis, stance, reached via, likely effect
   (supports / weakens / breaks / unclear), and why.
5. Offer follow-ups only if useful: record a new trigger on a thesis that the
   scenario exposes (`update_thesis`, `add_triggers`), or `/torus:challenge` a
   thesis that looks fragile. Don't record anything without the user's OK.

If nothing is reached, say so, and suggest why: the companies may have no links
in the graph yet (`/torus:onboard` or a starter pack would add them).
