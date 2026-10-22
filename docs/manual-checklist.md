# Manual release checklist: Claude Desktop and Codex

The automated tests cover the server, prompts, skills and config merging. These steps cover what
only the real apps can show. Run them on a clean `TORUS_HOME` before tagging a release.

```bash
export TORUS_HOME=~/torus-checklist        # PowerShell: $env:TORUS_HOME = "$HOME\torus-checklist"
pip install -e .
torus setup --contact you@example.com
torus doctor                               # Claude Desktop and Codex both "ok"
```

## Claude Desktop

1. **Install.** Restart Claude Desktop. Settings > Developer lists `torus` as running.
2. **Prompts.** The "+" menu lists the Torus prompts (Research status, Review alerts, Onboard
   companies, ..., Save this chat to Torus).
3. **Onboard.** Run **Onboard companies** with `NVDA AMD TSM`. Claude adds the companies, offers
   the semiconductors starter pack, and maps suppliers and customers from the 10-Ks, citing each
   filing. No mapping jobs are left in `torus status`.
4. **Research.** Ask *"Is NVIDIA still supply-bound?"*. Claude calls `brief` first, records a
   thesis with triggers and sourced claims, and ends with a "Torus recorded ..." line.
5. **Recall.** In a new chat, ask about NVIDIA again. Claude starts from the earlier thesis.
6. **Alert.** `torus watch run` (or wait for the schedule). If nothing fires, record a test event
   on TSMC. `torus alerts list` shows it.
7. **Review.** Run **Review alerts**. Claude judges the alert and updates the thesis on approval.
8. **Memo.** Run **Write a thesis memo** for the NVIDIA thesis. The memo appears in the chat, and
   the Markdown and HTML files exist under `$TORUS_HOME/memos`.
9. **Save and undo.** Run **Save this chat to Torus**, then ask to undo one claim it added.

## Codex (desktop app, then the CLI)

1. **Install.** Restart Codex. `/mcp` (CLI) or the MCP settings list `torus`. `~/.codex/skills`
   contains the `torus-*` folders.
2. **Skills.** Ask *"onboard NVDA AMD TSM with Torus"*. Codex picks the `torus-onboard` skill.
3. Repeat steps 3-9 above, asking in plain words instead of using prompts.
4. **Record what you find** in the release notes:
   - Does Codex show MCP prompts? (If not, the skills are the only entry point.)
   - Can Codex hooks run `torus hook`? (If so, automatic recall like Claude Code is possible.)

## Claude Code (regression)

1. `/plugin marketplace add` the local checkout, then `/plugin install torus@torus`.
2. `/torus:status`, a research prompt (the receipt appears), and `/torus:onboard` with the mapper
   subagent. The full toolset (44 tools) is listed.
