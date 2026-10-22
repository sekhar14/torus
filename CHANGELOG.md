# Changelog

## 0.3.0

Torus in Claude Desktop and Codex: the same single-user research loop as in Claude Code, set up
with one command.

- **`torus setup`.** Asks for the SEC contact email once, downloads the SEC ticker list, and
  configures the apps it finds: a `torus` entry in Claude Desktop's
  `claude_desktop_config.json`, and `[mcp_servers.torus]` plus the Torus skills for Codex
  (`~/.codex`). Each file is backed up first and other servers are kept; a Codex entry you wrote
  yourself is left alone. It also prints the Claude Code `/plugin` commands and installs the
  watcher schedule. `torus init --contact` now saves the email too, so it's entered once.
- **Live capture outside Claude Code.** The capture mode used to default to `balanced`
  everywhere, which left supporting claims and sources to a curator that only Claude Code has.
  Claude Desktop, Codex and other clients now default to `live`.
- **Harness-aware instructions.** Outside Claude Code the server tells the model to call `brief`
  before researching a company, to record as it goes, and to end each turn with a "Torus
  recorded ..." receipt. Records are tracked per chat, so `recent_records` and `undo_records`
  work without hooks.
- **New tools.** `brief` spots the companies in a question and returns prior research and the
  digest in one call. `open_explorer` writes the graph explorer and opens it in the browser.
  `thesis_memo` returns the memo's Markdown, and `audit` and `scorecard` include ready-made
  Markdown tables.
- **Workflows as prompts and skills.** Each workflow is written once in `src/torus/workflows/`
  and rendered as the plugin's `SKILL.md` files, as MCP prompts (Claude Desktop's "+" menu) and
  as Codex skills. A test fails if the plugin drifts from its source. A new **Save this chat to
  Torus** prompt replaces the curator in apps that don't have one.
- **Fewer tools in chat apps.** `TORUS_TOOLSET=analyst` (the default in Claude Desktop and
  Codex) hides the queue and low-level graph tools; `full` (the default elsewhere) shows all 44.
- **`torus doctor`** checks the Claude Desktop and Codex entries: that the command exists and
  that the server starts.
- **Desktop Extension (experimental).** `scripts/build_mcpb.py` builds `dist/torus.mcpb` with a
  settings form for the SEC email. It runs Torus through `uv`, so it needs `uv` installed;
  `torus setup` is the supported path.

## 0.2.1

Readable company names, alert strength labels, a shorter audit summary and scorecard wording
fixes.

- **Readable company names.** SEC titles arrive in capitals; Torus now shows them as
  "Taiwan Semiconductor Manufacturing Co Ltd", "ASML Holding NV" or "KLA Corp" in alerts,
  memos, recall and event titles. A word that is the company's ticker, and short acronyms, stay
  in capitals; names with any lower-case letter are left as they are. Migration 0012 tidies the
  names already stored for SEC-keyed companies (schema v12). The old spelling stays an alias,
  and lookups ignore case.
- **Alert strength labels.** Alert scores have no upper bound, so `torus alerts list`,
  `torus whatif` and recalled alerts now label them: *strong* (1.0 and up, as much as a fully
  material event about the thesis's own subject), *moderate* (0.5 and up) or *weak*.
- **Audit summary.** `torus audit` lists three low findings of each kind and summarises the rest
  (for example, every starter link after seeding a pack), so high and medium findings stay
  visible. `torus audit --all` lists everything; the MCP tool still returns every finding.
- **Scorecard wording.** Calibration says it covers every decided thesis, tracked against a price
  or not, and warns when fewer than 10 are decided.
- **Memo fixes.** A source with a date but no publisher no longer shows "None", and a competitor
  link recorded in both directions (research one way, a starter pack the other) is listed once.
- **Starter packs** no longer add a competitor link that research already recorded in the other
  direction.

## 0.2.0

Alert scoring, onboarding, audit, challenge, what-if, memos and the scorecard.

**Alert precision**
- Alerts score `graph score x materiality x (1 + trigger match)`. Materiality weighs SEC forms and
  8-K items (a routine 10-Q is 0.4, a bankruptcy 1.0) and news wording; trigger matching compares
  the event's text with each thesis's triggers. Each alert records how its score was built.
- Per-thesis alert thresholds and mutes (by company, event kind, SEC form, until a date), a global
  `alert_threshold` setting, alerts grouped by thesis for review, and `torus alerts stats`
  (the share of judged alerts that were related, by source, form, hops and materiality).

**Onboarding**
- `torus onboard` and `/torus:onboard`: tickers or a portfolio CSV become watched companies, with
  matching starter packs suggested and one mapping job per company.
- `company-mapper` subagent (Haiku): reads each company's latest 10-K or 20-F and records
  suppliers, customers, competitors and customer concentration, citing the filing.
- Starter packs `software`, `banks`, `energy` and `pharma`, alongside `semis`.
- SEC filings as text: `filing_text` reads a filing by section (business, risk factors, MD&A...),
  cached under `TORUS_HOME/sources/edgar`.

**Automation**
- `auto_distill` setting: `ask` (default) has Claude offer `/torus:distill` when sessions are
  queued; `on` starts a background Haiku curator when a session ends.
- `torus schedule install|status|remove` runs the watchers with the platform's scheduler;
  `torus doctor` warns when nothing does.

**Checking the research**
- `torus audit` and `/torus:audit`: expired or thinly sourced assumptions, theses past their
  horizon (horizons such as "12m", "FY2026" or "Q3 2026" are now read as dates) or gone stale,
  possible conflicts, inferences on retracted claims, unconfirmed starter links, orphans.
- Capture receipts after each turn, and `/torus:undo` with `recent_records` / `undo_records`.
- `/torus:challenge`: `thesis_weak_points` plus a `thesis-challenger` subagent that argues the
  other side and proposes risk claims and triggers for approval.
- `/torus:whatif` and `torus whatif`: alert analysis on a hypothetical event, recording nothing.

**Filings, memos and the scorecard**
- `filing_diff`: new, dropped and reworded sentences between a filing and the previous one of the
  same form. New 10-K/10-Q events from the watcher summarise their risk-factor changes.
- `/torus:ingest`: a filing, transcript, PDF or web page into evidence and claims.
- `torus memo` and `/torus:memo`: a thesis as a sourced memo in Markdown and HTML.
- `torus scorecard` and `/torus:scorecard`: returns against a benchmark in the thesis's direction,
  drawdowns, outcomes (from status, at the horizon, or set by hand) and calibration (Brier score,
  reliability by confidence band). Daily closes from Yahoo Finance or a CSV import.

**Explorer**
- A time slider and Replay: the graph as it stood on any date, with each thesis's status then.
  `#at=YYYY-MM-DD` opens a past date directly (and combines with `#select=`).
- A thesis's alerts show how their score was built.

**Under the hood**
- The Stop hook now runs synchronously (it prints the capture receipt).
- `scripts/seed_demo.py` spreads the demo research over four months, for the screenshots.
- Schema v11 (migrations 0008-0011), applied automatically on first use.
- MCP server: 16 new tools (42 in all), most in `torus.mcp.workflows`.

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
