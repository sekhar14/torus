---
name: memo
<!-- if code -->
description: Write a Torus thesis up as a shareable memo - the claims it rests on with sources and quotes, triggers, events and how they were judged, ruled-out ideas, scorecard and history - as Markdown and a standalone HTML page. Use when the user runs /torus:memo or wants to share, export or write up a thesis.
<!-- else -->
description: Write a Torus thesis up as a shareable memo - the claims it rests on with sources and quotes, triggers, events and how they were judged, ruled-out ideas, scorecard and history - as Markdown and a standalone HTML page. Use when the user wants to share, export or write up a thesis.
<!-- end -->
---

# Thesis memo

1. Find the thesis. If the user named it loosely ("the NVIDIA one"), call
   `recall` and pick it; ask if several match.
2. Call `thesis_memo` with its id. Torus writes the memo as Markdown and HTML
   under `TORUS_HOME/memos` and returns the Markdown and both paths. Every claim
   in it is traced to its sources by Torus, not written by you, so it stays
   faithful to the record.
3. Read the memo and write a **summary of 3-5 sentences** for the top: the
   thesis, the strongest support, the biggest open risk, and what would change
   the view. Use only what the memo says. Show the summary to the user, then
   the file paths.
<!-- if code -->
4. If the user wants the summary inside the memo, add it under the title of the
   Markdown file with the Edit tool, headed `## Summary`. Mention that running
   `thesis_memo` again regenerates both files and drops manual edits.
<!-- else -->
4. Present the full `markdown` from `thesis_memo` (with your `## Summary`
   placed right under the title) so the user can read or copy it straight from
   the chat, and mention the saved `markdown_path` and `html_path` files.
<!-- end -->
5. Point out gaps the memo shows, briefly: assumptions with no source, claims
   only from news, an expired assumption. Suggest {{audit}} or
   {{challenge}} if there are several.

The memo ends with "Torus is a research memory, not financial advice." Keep that
line.
