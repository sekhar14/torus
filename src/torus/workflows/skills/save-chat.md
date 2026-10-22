---
name: save-chat
description: Save the research from the current conversation into the Torus graph - sources, atomic claims, supplier/customer/competitor links, theses and ruled-out ideas that weren't recorded yet. Use when the user asks to save, capture or distill this chat into Torus.
---

# Save this chat to Torus

Review the current conversation and record any research findings that are not
yet in the Torus graph, using only what the conversation and its sources state.

1. Call `recent_records` (`sessions: 1`) and `recall` on the companies discussed
   so you don't duplicate claims, links or theses that are already stored.
2. Record what is missing:
   - `record_evidence` for each primary filing, transcript or article cited in
     the chat (URL, title, kind, and a short verbatim quote).
   - `record_claim` for each atomic fact, metric or inference, citing the
     evidence id (or `derived_from` claim ids for an inference). Use
     `supersede_claim` when the chat updated an earlier claim.
   - `record_relation` (`SUPPLIES`, `CUSTOMER_OF`, `COMPETES_WITH`,
     `EXPOSED_TO`, `SUBSIDIARY_OF`) for durable company links, passing
     `claim_id` when backed by a claim.
   - `record_thesis` (or `update_thesis`) for conclusions reached in the chat,
     linking `assumption_claim_ids`, `supporting_claim_ids`, `risk_claim_ids`
     and concrete `triggers`.
   - `record_rejected_hypothesis` for ideas checked and ruled out.
3. Finish with a short receipt listing what you added (theses, claims, sources,
   links and ruled-out ideas) and remind the user they can revert anything with
   {{undo}}.
