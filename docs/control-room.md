# Control Room

The **Control room** tab collects what your Claude Code setup is made of in one place. Everything is read
from files; nothing is started. It is rooted at two folders from `config.json`: `workspace` (your project)
and `claude_home` (default `~/.claude`).

| Panel | Reads | Writes |
|---|---|---|
| Workspace health | the `health_checks` you define | nothing |
| Agent files | markdown that steers your agents (below) | the file you save, with history |
| Hook kill switches | `.claude/settings.json`, `settings.local.json`, `~/.claude/settings.json` | `<kill_switch_dir>/<name>.off` only |
| Hooks, Agents, Skills | the same settings, `.claude/agents/*.md`, `.claude/skills/*/SKILL.md` (project and user level) | nothing |
| Plugins, MCP servers | `enabledPlugins` and `mcpServers` in settings, `.mcp.json`, `~/.claude.json` | nothing |

Plugins and MCP servers show names, transport and host only, never arguments, environment or headers.
Chronos **never runs `claude mcp list`**: its health check starts every configured server, and a channel
plugin among them starts a second Telegram poller, which Telegram answers with HTTP 409 and which knocks
your live channel offline. Account-level connectors (the ones you add in claude.ai) are not in files, so
they are not listed.

## Hook kill switches

A toggle appears for a hook only if the hook's script *reads* a kill-switch file, which Chronos checks by
looking for a string literal like `"guard-rm.off"` in the script's code (a mention in a comment does not
count). This way the UI never offers a switch that does nothing. Convention:

```python
# in your hook script
import os, sys
if os.path.exists(os.path.expanduser("~/.chronos/switches/guard-rm.off")):
    sys.exit(0)            # switched off from the Control Room
```

```bash
# or in bash
[ -e "$HOME/.chronos/switches/format-on-save.off" ] && exit 0
```

The folder is `kill_switch_dir` (default `<home_dir>/switches`). Flipping a switch needs the page token and
is confirmed; it takes effect on the hook's next run, in every session on the machine.

## Workspace health tiles

`health_checks` in `config.json` is a list; paths are relative to the workspace (or absolute / `~`).

```json
"health_checks": [
  {"label": "CLAUDE.md size", "type": "chars", "path": "CLAUDE.md", "ceiling": 4000},
  {"label": "Last lint report", "type": "age", "path": "notes/lint-*.md", "max_age_days": 9},
  {"label": "Daily log", "type": "today", "path": "notes/%Y-%m-%d.md"}
]
```

| `type` | Shows | Colours |
|---|---|---|
| `chars` | characters in a file against an optional `ceiling` (Python `len()` of the decoded text) | warn above 95%, bad over, bad if missing |
| `age` | age of the newest file matching a glob | warn past `max_age_days` or if none match |
| `today` | whether today's file exists (`%Y-%m-%d` style codes expand) | warn until it exists |

A malformed entry becomes a visible warning tile, never an error.

## Agent files

Lists and edits these markdown files, and only these:

- the workspace `CLAUDE.md` and `~/.claude/CLAUDE.md`
- `.claude/agents/*.md` and `~/.claude/agents/*.md`
- `.claude/skills/*/SKILL.md` and `~/.claude/skills/*/SKILL.md` (folders starting with `_` or `.` are skipped)
- Claude Code's auto-memory for the workspace (`~/.claude/projects/<slug>/memory/`)
- anything you add with `agent_files_extra`

```json
"agent_files_extra": [
  {"glob": "notes/TODO.md", "category": "Notes", "agent_edited": true, "ceiling": 3000},
  {"glob": "notes/*.md", "category": "Notes", "agent_edited": true}
],
"agent_files_exclude": ["notes/private/**", "secrets"],
"agent_files_lint": "bash scripts/lint-claude-md.sh"
```

- `agent_edited` shows a "live" chip and a warning that an agent or hook may edit the file too.
- `ceiling` shows a live character counter while you type and warns (without blocking) when over.
- `agent_files_exclude` globs are matched against the file's real path, relative to the workspace (a bare
  folder name excludes everything under it). **Put private notes here.**
- `agent_files_lint` runs from the workspace after you save the workspace `CLAUDE.md` and shows its output.
  A failing lint warns; the save is kept and History can revert it. The command comes from your config, not from the browser.

### Safety

- **An allowlist rebuilt on every request.** The browser only ever sends an opaque id; the server looks it
  up in the list it just enumerated. No path or file name from the client is ever used.
- Every file's real path (symlinks resolved) must be inside the workspace or the Claude home, must not match
  an exclude glob, must not sit under a `.env*` path, must be `.md`, and must be a regular file. Symlinks
  pointing outside are not listed.
- **Conflict guard.** Opening a file records its sha256. A save whose sha no longer matches is refused with
  409 and nothing is written (an agent or hook changed the file meanwhile). The sha is checked again just
  before the atomic replace.
- **History.** Each save first snapshots the previous bytes to `~/.chronos/history/agent-files/<id>/<time>.md`.
  Restoring snapshots the current version first, so a restore can be undone.
- Line endings are preserved (CRLF stays CRLF). Files that are not valid UTF-8, contain NUL bytes, have mixed
  line endings or exceed 300 KB are view-only.
- Reading and saving both need the page token.
