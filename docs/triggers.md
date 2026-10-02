# Event triggers

A job can start on events instead of (or as well as) the clock. Add `triggers` to its entry in `jobs.json`,
or use the **Triggers** tab in the web UI.

```json
{
  "id": "inbox-triage", "name": "Inbox Triage", "enabled": true, "notify": "always", "model": "sonnet",
  "clock": false,
  "triggers": [
    {"type": "gmail", "query": "label:customers is:unread"},
    {"type": "webhook"}
  ]
}
```

`"clock": false` means the job has no clock schedule and starts only on its triggers. Leave `clock` out to
keep the clock schedule as well. A one-shot job cannot have triggers.

## How it works

1. Every 5 minutes `chronos-tick.sh` handles the clock jobs first, then runs `chronos triggers`
   (a 150 second hard stop, so a slow poll can never wedge the tick).
2. Each enabled, unpaused job's triggers are evaluated. **The first evaluation of a trigger only records a
   baseline** (what already matches) and fires nothing, so pointing a trigger at a full inbox or an old
   repository never replays history.
3. Each new item becomes one event. Chronos claims it (`claim-<id>-<date>-<hash>`, an atomic `mkdir`) and
   starts `chronos-run.sh` in event mode. The same event can never run twice.
4. At most `event_rate_per_hour` (default **6**) events fire per job per hour. The rest wait and fire when
   the budget returns; nothing is dropped.
5. Success is a clean exit with no error result. Chronos then writes the done-marker, saves the final
   message as `reports/<id>-<date>-<hash>.md`, and sends the `notify` message if the job says `always`.
   A failed event run is reported through `notify` (unless `never`) and **not retried**. It never marks the
   clock day failed.

State lives in `~/.chronos/trigger-state/<id>.json`. `chronos triggers --dry` shows what would fire and
changes nothing. The **Test** button in the UI does the same for one trigger.

## Trigger types and prerequisites

| Type | Fires on | Needs | If the prerequisite is missing |
|---|---|---|---|
| `file` | a new or modified file in a folder that has stopped changing for 30 s (`{"path": "~/Downloads", "glob": "*.pdf"}`) | nothing | the folder cannot be read: the trigger records the error |
| `gmail` | a message matching a Gmail search that Chronos has not seen (`{"query": "from:billing", "body": false}`) | an **adapter command** in `gmail_command` (below) | "no Gmail adapter is configured" |
| `github` | a new pull request, issue or release (`{"repo": "owner/name", "event": "pr"}`) | the [`gh`](https://cli.github.com) CLI, logged in (`gh auth login`) | "the gh command line tool was not found" |
| `webhook` | `POST /hook/<job id>` with the job's secret | nothing | n/a |

A trigger that cannot run does **not** break anything: it records `last_error` (shown on the Triggers tab,
and by **Test**), the tick carries on, and every other trigger and job keeps working. `interval_min`
(5-1440) overrides the default 10 minute poll spacing for Gmail and GitHub.

### file

Only the file name, path, size and modification time reach the run, never its contents. The run may open
the file itself if its tool list allows `Read`.

### gmail

Chronos has no Google credentials of its own. You give it a command, and it calls

```
<gmail_command> "<your query> newer_than:1d" 10 [--body]
```

which must print JSON on stdout:

```json
{"emails": [{"id": "stable-message-id", "from": "...", "to": "...", "subject": "...", "date": "...",
             "hasAttachments": false, "attachmentNames": [], "body": "only with --body"}]}
```

`examples/gmail/imap-search.py` is a ready-made adapter using Gmail's IMAP interface and an App Password
(standard library only, read-only: it opens the mailbox read-only and fetches with `BODY.PEEK`):

1. In your Google account, turn on 2-step verification and create an **App Password**; make sure IMAP is enabled.
2. Create `~/.config/chronos/gmail.env` (`chmod 600`) containing `GMAIL_ADDRESS=...` and `GMAIL_APP_PASSWORD=...`.
3. Set `"gmail_command": "/path/to/chronos/examples/gmail/imap-search.py"` in `config.json`.
4. Open the job's Triggers tab and press **Test**.

Any other tool works as long as it prints that JSON. Mail whose subject contains **`[Chronos]`** never fires
a trigger, so tag any mail Chronos itself sends and it cannot loop. With `"body": true` more outside text
reaches the run; leave it off unless the job needs the message text.

### github

Polled with `gh api repos/<repo>/pulls | issues | releases`. It uses whatever account `gh` is logged in as.
Set `gh_bin` in `config.json` if `gh` is not on the PATH that launchd gives Chronos.

### webhook

The job gets a secret when you save a webhook trigger (`~/.chronos/hook-secrets/<id>`, mode 0600; show or
rotate it on the Triggers tab).

```bash
curl -X POST http://127.0.0.1:4747/hook/inbox-triage -H "X-Chronos-Secret: <secret>" -d '{"hello": 1}'
```

The delivery is queued (20 per job, 64 KB each) and fires at the next tick. The UI server listens on
`127.0.0.1` only, so out of the box only programs on your Mac can call it. To let an outside service call
a hook, publish **only** the `/hook/` path through a tunnel you control and list the public hostname in
`hook_hosts` in `config.json`; it is accepted on `/hook/` and nowhere else. Never publish the whole UI.
Examples:

```
tailscale funnel --bg --set-path /hook http://127.0.0.1:4747/hook
# config.json: "hook_hosts": ["your-machine.your-tailnet.ts.net"]
```

```yaml
# ~/.cloudflared/config.yml  (a named tunnel; do not use a bare quick tunnel, it publishes everything)
ingress:
  - hostname: hooks.example.com
    path: ^/hook/[a-z0-9-]+$
    service: http://127.0.0.1:4747
  - service: http_status:404
```

Restart the UI after changing `hook_hosts` (`launchctl kickstart -k gui/$(id -u)/io.github.chronos.ui`).

## What an event run can do

The run's prompt is: a short preamble, your `guard.md` (if any), your `prompt.md` as **trusted
instructions**, then the event inside an `UNTRUSTED EVENT DATA` block. Write `prompt.md` as "when this
event arrives, do X with it"; it should say the event is data. `examples/jobs/pdf-digest/` is a model.

The child is started with:

```
claude -p --permission-mode=default
  --allowedTools=<per-job list>           default: Read,Grep,Glob (+ the notify sender when `notify` is set)
  --tools=<the built-ins that list needs>
  --disallowedTools=<secret paths>        .env files, ~/.ssh, ~/.aws, ~/.gnupg, Chronos's home + config folder, Claude credentials
  --strict-mcp-config                     unless the list names an mcp__ tool
  --settings '{"enabledPlugins": {...channel plugins: false}}'
  [--model=<job model>] --output-format=stream-json --verbose
```

It never gets `--dangerously-skip-permissions`, and `claude_args` is ignored for event runs. Edit the list
per job in the UI ("What an event run may do") or with `allowed_tools` in `jobs.json`. A bare `Bash`
entry, a `Bash(...)` prefix shorter than four characters, or a malformed entry is refused; an invalid list
found in `jobs.json` falls back to the safe default rather than to something wider.

Widening the list (Write, Edit, Bash patterns, MCP tools) widens what a stranger's email can make the job
do. Do it per job, for jobs whose input you trust, and prefer a narrow `Bash(/path/to/script.sh:*)` over anything broader.

## Troubleshooting

| Symptom | Look at |
|---|---|
| Trigger says "Waiting for the first check" | It has not been polled yet; the first poll only records a baseline |
| Nothing fires for a new file | It may still be changing (30 s settle), or it does not match the glob; press **Test** |
| Gmail "no Gmail adapter is configured" | Set `gmail_command`; see above |
| GitHub "gh command line tool was not found" | Install `gh`, run `gh auth login`, or set `gh_bin` |
| Events are delayed | 5 minute tick; Gmail/GitHub poll every 10; `event_rate_per_hour` may be used up |
| An event run keeps failing | `~/.chronos/logs/<id>-<date>-<time>-<hash>.log`, and the "denials" count on the Usage tab: a denied tool means the list is too narrow |
