# Changelog

## 0.1.0

The first release: a local research knowledge graph for Claude Code.

**Graph and storage**
- Bi-temporal SQLite graph of entities, evidence, claims, relations, theses, rejected hypotheses,
  events and alerts. Corrections add versions instead of overwriting, and any query can be asked
  "as of" a past date.
- Entity resolution by ticker, CIK, name, alias or id, using the SEC ticker list, with merging
  of duplicate entities.
- Relations record their provenance: `agent`, `curator`, `user` or `starter:<pack>@<version>`.

**Claude Code plugin**
- Hooks: prior research is added to prompts that mention known companies, research sources are
  logged, and research sessions are queued for distillation. Hooks never break a session.
- Capture modes: `live`, `balanced` (default) and `curator`.
- Skills: `research-protocol`, `/torus:status`, `/torus:review`, `/torus:distill`,
  `/torus:explore`.
- `graph-curator` subagent (Haiku) that distils past sessions from their transcripts.

**MCP server** (`torus serve`), with 26 tools:
- recall, search, timeline and what_changed
- record and revise claims, evidence, relations, theses and rejected hypotheses
- events, impact analysis and alerts
- watchlist, graph neighbours and paths
- the distillation queue

**Events and alerts**
- Impact analysis walks supplier, customer and competitor links (up to 2 hops, valid at the
  event's date) to find the theses an event may affect, with paths that explain each alert.
- Watchers turn SEC EDGAR filings and RSS/Atom items into events (`torus watch run`).

**Graph tools**
- The `semis` starter pack: 23 companies and 52 curated links across the semiconductor and AI
  hardware value chain.
- `neighbors` and `find_paths` queries (CLI and MCP).
- A visual explorer (`torus explore`): one self-contained, offline HTML file with search,
  filters and a details panel.

**Operations**
- `torus doctor` (with `--repair`), and `torus export` / `torus import` as a lossless JSON-lines
  round trip.
