---
name: distill
description: Process the Torus distillation queue - have the Haiku curator turn past research session transcripts into claims, evidence and relations in the research graph. Use when the user runs /torus:distill or asks to distill, curate or catch up the Torus graph.
---

# Distill queued research sessions

Research sessions are queued for distillation when they end. The curator, a
subagent running on Haiku, reads each session's transcript and records the claims,
evidence and relations the researcher didn't record.

1. Delegate to the `torus:graph-curator` subagent with the Agent tool, using this
   prompt: "Process the Torus distillation queue." Run it in the background if
   the user wants to keep working.
2. When it finishes, give the user its report in two or three lines: sessions
   processed, and what was added.
3. If it reports failures, say which sessions failed and why. They are retried
   automatically next time and parked after 3 attempts.

Don't distill sessions yourself in the main conversation. The curator exists so
this work runs on a cheaper model.

To have this happen without asking, the user can run
`torus config set auto_distill on`: a background curator then starts when a
session ends. `off` stops Torus from mentioning queued sessions at all.
