---
name: ingest
<!-- if code -->
description: Read a document into the Torus research graph - an SEC filing (by company and form), what changed since the previous filing, a web page, or a local PDF or transcript - and record its key facts as sourced claims linked to existing theses. Use when the user runs /torus:ingest, shares a filing, transcript, report or link to "add to Torus", or asks what changed in a company's latest 10-K or 10-Q.
<!-- else -->
description: Read a document into the Torus research graph - an SEC filing (by company and form), what changed since the previous filing, a web page, or a transcript or PDF - and record its key facts as sourced claims linked to existing theses. Use when the user shares a filing, transcript, report or link to add to Torus, or asks what changed in a company's latest 10-K or 10-Q.
<!-- end -->
---

# Ingest a document

Turn one document into evidence and claims, with verbatim quotes, linked to
the theses it bears on.

1. **Get the text.**
   - SEC filing ("NVDA latest 10-K", "TSMC 20-F"): `filing_text` with the
     company, form and a section (`business`, `risk_factors`, `mdna`...).
     Long sections come in pages; follow `next_offset`.
   - "What changed": `filing_diff` with the company, form and section. Focus
     on `added` and `changed` sentences. That's where the news is.
<!-- if code -->
   - Web page: WebFetch. Local file (PDF, transcript, notes): Read.
<!-- else -->
   - Web page, PDF, transcript or notes: read the link, attachment or text the
     user provided.
<!-- end -->
2. **Check what's known.** Call `recall` for the companies involved, so you
   build on existing claims and theses instead of duplicating them.
3. **Pick what matters**: facts, metrics, guidance, new risks, named
   customers or suppliers, and anything that bears on an existing thesis's
   assumptions or triggers. Skip boilerplate.
4. **Record**, after showing the user a short list of what you plan to record
   (unless they asked you to just do it):
   - `record_evidence` once for the document: its URL or path, a title, the
     kind (`filing` for SEC filings, `transcript` for calls, else `web`), the
     publication date, and a verbatim quote.
   - `record_claim` for each finding, atomic and with the companies named,
     citing that evidence. Use `metric` for numbers and set `valid_from` /
     `valid_to` for time-bound facts like guidance.
   - Use `supersede_claim` when the document updates an existing claim (new
     guidance, a revised number).
   - `record_relation` for named suppliers, customers or competitors.
   - Link claims that bear on a thesis with `update_thesis` (`add_claims`,
     role `supporting` or `risk`), giving the reason.
5. **Report** in a few lines: what was recorded, which theses it touches and
   in which direction, and anything that looks like it should trigger a
   review. If something undercuts a thesis's assumption, say so plainly and
   offer {{challenge}} or a status change.

Quotes must be verbatim from the document. Never record a number the document
doesn't state.
