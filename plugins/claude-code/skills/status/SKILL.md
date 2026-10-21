---
name: status
description: Show the state of the Torus research memory - theses at risk, recent changes, and research on a given company. Use when the user asks what Torus knows, what changed recently, or which theses need attention.
---

# Torus status

Give the user a short overview of their research memory.

1. Read the `torus://digest` resource from the torus MCP server. It lists theses
   marked at risk and changes from the last 7 days.
2. If the user named a company or ticker, also call `recall` for it and
   summarise its active theses (status, confidence, horizon) and the most recent
   claims, with dates.
3. If the user asked about a period ("since Monday", "this month"), call
   `what_changed` with that start date.

Keep it brief: a few bullet points per section, ids in parentheses so the user
can ask follow-up questions. Don't record anything while doing this.
