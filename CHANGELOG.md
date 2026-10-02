# Changelog

## 0.2.1 - 2026-10-01

Command jobs, and a quieter installer. Everything from 0.2.0 keeps working unchanged.

**Command jobs** - `"kind": "command"` plus `"command": "<shell command>"` runs a plain command on the clock
schedule instead of `claude -p`: same atomic claim, done-marker, catch-up, watchdog, log, notice and notify,
no `prompt.md`, no tokens
- success is exit code 0 inside the watchdog; the output is the log, its last 40 lines the report
- runs through `/bin/bash -c` in the workspace; `CHRONOS_JOB` and `CHRONOS_RUN=1` are set; stdin is closed
- refused for a command job: `triggers`, `clock: false`, `in_session: true` (outside text never reaches a shell)
- the web UI shows the command read-only, hides the Prompt, Triggers, Model and Guardrails tabs for it, and
  cannot create or edit one; "Run now" works
- the Usage page lists the run as `(command)` with zero tokens and zero cost

**Installer**
- `install.sh --quiet`: warnings and errors plus one summary line, for another installer that calls this one
- with `--no-load` the closing text no longer prints a UI address as if it were running; it prints the exact
  commands to start the scheduler and the UI by hand (also in the README)
- "created an empty jobs.json" now reads "no jobs yet: ..." and says what to do about it

**Docs** - a note about the macOS protected-folders prompt (keep the workspace out of Desktop, Documents and
Downloads)

**Tests** - 109 -> 122

## 0.2.0 - 2026-10-01

Events, a usage meter and the Control Room. Everything from 0.1.0 keeps working unchanged: a config or
`jobs.json` written for 0.1.0 loads as is, and every new key is optional.

**Event triggers**
- A job can carry `triggers`: `file` (new or modified file, settled 30 s), `gmail` (through a `gmail_command`
  adapter you configure; an IMAP example ships in `examples/gmail/`), `github` (pull requests, issues,
  releases through `gh api`) and `webhook` (`POST /hook/<id>`)
- `"clock": false` makes an event-only job
- The first check of a trigger records a baseline and fires nothing; every event has its own atomic claim,
  done-marker, log and report; at most 6 event runs per job per hour (`event_rate_per_hour`)
- Gmail and GitHub triggers degrade cleanly when the adapter or the `gh` CLI is missing: the error is
  recorded and shown, other triggers and jobs are unaffected
- Webhook: 127.0.0.1 only, per-job secret (0600, constant-time compare), 64 KB bodies, queue of 20,
  throttle on wrong secrets, extra tunnel hostnames accepted on `/hook/` only (`hook_hosts`)
- **Event runs are untrusted and never bypass permissions**: `--permission-mode=default`, a narrow
  `--allowedTools` list (job setting `allowed_tools`, config `event_allowed_tools`), `--tools`,
  `--disallowedTools` for secret paths, `--strict-mcp-config`; payloads are sanitised, capped and wrapped in
  a nonce-marked data block; `claude_args` is ignored for them

**Per-job model** - `model` on a job is passed as `--model` (config `model_denylist` can forbid some)

**Usage meter** - runs use `--output-format=stream-json --verbose`; each run appends a row to `runs.jsonl`
(tokens, cost, model, turns, denials) and the newest 7-day / 5-hour utilisation goes to `rate-limits.json`.
New Usage page and a per-job usage panel. The plain-text log is rebuilt from the stream; a truncated or
unrecognised stream degrades to the raw output

**Control Room** - agents, skills, hooks with on/off switches (for hooks that read a `<name>.off` file),
configurable workspace health tiles, plugins and MCP servers (read from files, never `claude mcp list`)

**Agent files** - view and edit CLAUDE.md, agents, skills, notes and auto-memory from the browser:
allowlist rebuilt per request, opaque ids, exclude globs, sha256 conflict guard, history and restore,
optional ceiling counter and lint command

**Other**
- `chronos triggers --dry`, `chronos jobenv`, `chronos claim-event`, `chronos record`, `chronos --version`
- `tools/demo.py` builds a fake workspace for the UI (used for the new screenshots)
- A config named through `CHRONOS_CONFIG` that is missing is now an error instead of silently becoming the
  defaults, so a run can never write into the default home by accident
- Run script validates the job id, event hash and type before doing anything; `eval` of the config output no
  longer swallows a failed `chronos env`
- New docs: `docs/triggers.md`, `docs/control-room.md`; new screenshots taken against the demo workspace
- 109 tests (was 32)
- `uninstall.sh --purge` deletes the config folder only when it is a folder named `chronos`, and quotes every
  path (a `CHRONOS_CONFIG` sitting straight in `$HOME` can no longer widen the purge)

## 0.1.0 - 2026-10-01

First public-shaped release, extracted and generalized from a private scheduler.

- launchd tick every 5 minutes; jobs defined in `jobs.json`, one `prompt.md` per job
- Headless runs through `claude -p` with a 40-minute watchdog
- Atomic claim (`mkdir`) plus done-markers, so a job runs once per day across every runner
- Catch-up windows for a machine that was asleep, and a grace period so a live session can take a job first
- One-shot jobs that switch themselves off after a successful run
- Failure notifications through a pluggable `notify` command (Telegram and macOS examples included)
- Telegram/iMessage plugins disabled in the headless child (prevents the second-poller HTTP 409)
- Web UI on 127.0.0.1: dashboard, prompt editor with history, schedule editor, run now, pause, logs and reports
- Optional SessionStart hook that arms in-session timers and reports headless runs
- `install.sh` / `uninstall.sh`, test suite with a fake clock and a fake `claude`
