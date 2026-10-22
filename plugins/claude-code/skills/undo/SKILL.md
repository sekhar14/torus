---
name: undo
description: Show what Torus recorded in recent research sessions and revert what the user doesn't want - wrong claims, a thesis recorded by mistake, a bad link. Use when the user runs /torus:undo, says Torus recorded something wrong, or asks what was saved.
---

# Undo recorded research

After each turn Torus tells the user what it recorded. This skill reverts any of
it. Nothing is deleted: claims are retracted, theses archived, links retired and
ruled-out ideas withdrawn, so the history still shows what happened.

1. Call `recent_records` (`sessions: 1`, or more if the user means an earlier
   session).
2. Show the records as a short numbered list: type, text, and state. Mark the
   ones that can't be undone and say why (evidence is kept because a source on
   its own changes nothing; a thesis the session only revised needs
   `update_thesis`; a record that changed since can't be reverted blindly).
3. Ask which to revert, unless the user already said ("undo everything from
   that turn", "drop the TSMC claim"). Don't guess.
4. Call `undo_records` with the `session_id` and the chosen records.
5. Report each outcome in one line. A correction goes back to the earlier
   version of the claim. A confirmed starter link goes back to its starter
   version.

If the user wants a record changed rather than removed, use the normal tools
instead: `supersede_claim` to fix a claim, `update_thesis` to revise a thesis.
Settings: `torus config set capture_receipts off` turns off the per-turn receipt.
