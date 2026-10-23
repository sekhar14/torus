# Manual release checklist

The automated tests cover the server, prompts, skills and config merging. These steps cover what
only the real apps can show. Run them on a clean `TORUS_HOME` before tagging a release.

```bash
export TORUS_HOME=~/torus-checklist        # PowerShell: $env:TORUS_HOME = "$HOME\torus-checklist"
pip install -e .
torus setup --contact you@example.com
torus doctor                               # Claude Desktop and Codex both "ok"
torus demo                                 # opens the sample graph in the explorer
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
10. **Digest.** Ask *"What happened in my research this week?"*. Claude calls `digest` and shows
    its sections.

## Codex (desktop app, then the CLI)

1. **Install.** Restart Codex. `/mcp` (CLI) or the MCP settings list `torus`. `~/.codex/skills`
   contains the `torus-*` folders.
2. **Skills.** Ask *"onboard NVDA AMD TSM with Torus"*. Codex picks the `torus-onboard` skill.
3. Repeat steps 3-10 above, asking in plain words instead of using prompts.
4. **Record what you find** in the release notes:
   - Does Codex show MCP prompts? (If not, the skills are the only entry point.)
   - Can Codex hooks run `torus hook`? (If so, automatic recall like Claude Code is possible.)

## Claude Code (regression)

1. `/plugin marketplace add` the local checkout, then `/plugin install torus@torus`.
2. `/torus:status`, a research prompt (the receipt appears), and `/torus:onboard` with the mapper
   subagent. The full toolset (45 tools) is listed.

## Notifications (on each of Windows, macOS and Linux)

The unit tests check the commands Torus runs. Only a real desktop shows whether a notification
appears.

1. **Test.** `torus notify test` shows "Torus: test notification". `torus doctor` reports
   `notifications` as ok.
2. **Hostile text.** Ask Claude to record a test event (`add_event`) whose title has quotes,
   `$(date)`, backticks and `<b>tags</b>`, on a thesis's subject so it raises a strong alert, then
   run `torus watch run --no-edgar --no-rss`. The notification shows the text literally, and
   nothing runs.
3. **From the scheduler.** `torus schedule install`, then raise a strong alert and wait for the
   next run (or start it: Task Scheduler "Run", `launchctl start dev.torus.watch`, or the cron line
   by hand). The notification appears without a terminal open. On Linux this checks the session
   bus fallback.
4. **Once only.** Run `torus watch run` again: no second notification for the same alert.
5. **Off.** `torus config set notify off`, raise another strong alert: no notification.

Windows 11 passed steps 1 and 2 for 0.4.0.

## Digest delivery

1. **Files.** `torus digest --since 2w --open` opens the HTML in light and dark mode.
2. **ntfy.** `torus config set digest_webhook https://ntfy.sh/<a random topic>`, subscribe in the
   ntfy app, then `torus digest test-send` and `torus digest --send`. The digest arrives as
   Markdown, at high priority when an alert is strong.
3. **Slack and Discord.** The same with an incoming-webhook URL for each. The text is readable,
   and a long digest ends with the "Shortened" note.
4. **Masking.** `torus config get digest_webhook`, `torus doctor` and a failed send (break the URL)
   show only the masked URL. `torus export` doesn't contain it.
5. **Gmail.** Create an app password, then set `digest_email_to`, `smtp_host smtp.gmail.com` and
   `smtp_user`, and run `torus digest email-password` and `torus digest test-send`. The mail
   arrives with HTML and a plain-text part. On macOS and Linux, `ls -l` shows the password file
   as `-rw-------`; on Windows, `icacls` shows only your user.
6. **Schedule.** `torus config set digest daily` and `digest_hour` to the next hour. After the
   watcher run past that hour, one digest arrives, and `torus doctor` shows when it was sent.
