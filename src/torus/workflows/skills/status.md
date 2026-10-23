---
name: status
description: Show the state of the Torus research memory - theses at risk, recent changes, and research on a given company. Use when the user asks what Torus knows, what changed recently, or which theses need attention.
---

# Torus status

Give the user a short overview of their research memory.

<!-- if code -->
1. Read the `torus://digest` resource from the torus MCP server. It lists theses
   marked at risk, changes from the last 7 days, graph audit findings by
   severity, and how often recent alerts were judged related.
2. If the user named a company or ticker, also call `recall` for it and
   summarise its active theses (status, confidence, horizon) and the most recent
   claims, with dates.
<!-- else -->
1. Call `brief` (pass the company or ticker in `text` if the user named one).
   It returns the digest (open alerts, theses at risk, changes from the last
   7 days, graph audit findings by severity, and alert precision) plus any
   prior research on the company.
2. If the user named a company or ticker, summarise its active theses (status,
   confidence, horizon) and the most recent claims, with dates.
<!-- end -->
3. If the user asked about a period ("this week", "since Monday", "this month"),
   call `digest` with `since_days` covering it: new alerts by thesis, thesis
   status and confidence changes, theses coming due, scorecard moves and
   activity, with ready-made `markdown`. For a line-by-line record of what was
   written, call `what_changed` with the start date.
4. Point to the next step when there is one: {{review}} for open alerts,
   {{audit}} for high-severity findings, {{scorecard}} if theses are
   tracked.

Keep it brief: a few bullet points per section, ids in parentheses so the user
can ask follow-up questions. Don't record anything while doing this.
