# Tests and docs audit, 2026-11-10

Branch `cloud/tests-docs`, based on `main` at a85c087 (0.5.0). Run on Linux (Python 3.11), not on a Mac.

## 1. Suite before the changes

`tests/run.sh`: 518 tests in 18 files, 6 failures. None of the 6 was a product bug.

| Test | Cause | Fix |
| --- | --- | --- |
| 4 `Installer` tests (test_chronos.py) | `install.sh` stops unless `uname -s` is `Darwin` and calls `plutil` | On a non-Mac host the tests put `uname` and `plutil` stand-ins on `PATH` (the `plutil` one parses the plist with `plistlib`). On a Mac nothing changes. |
| `Claims.test_the_stale_sweep_removes_both_files` (test_api_runs.py) | The claim is written under `TZ=America/New_York`, but the test read the naive clock in the host zone. It failed in any zone east of UTC-3:30. | The test sets `TZ` to New York while it runs. |
| `Features.test_api_version_lists_the_features_and_keeps_api_1` (test_times.py) | 0.5.0 added `memory` to `features`, and this assertion was never updated | Expect `["times", "meta", "memory"]` |

## 2. Suite after the changes

`tests/run.sh`: **542 tests in 19 files, all pass** (about 4 minutes). The new file is `tests/test_gaps.py` with 18 tests.

Coverage (coverage.py, subprocesses included): bin/chronos 89%, lib/chronoslib.py 84%, chronos_api 89%, chronos_runlog 90%,
chronos_triggers 86%, chronos_write 88%, ui/agent_files 88%, ui/control_room 89%, hooks 89%. `ui/server.py` shows 0% because the
tests stop it with SIGTERM before coverage writes its data, so that number tells you nothing. The Python is well covered. The
untested risk was in the bash scripts (`chronos-run.sh`, `chronos-tick.sh`, `chronos-notify`, `install.sh`) and in the
edge-case inputs, so that is where the new tests go.

## 3. Bugs found (test first, fix in its own commit)

1. **The run watchdog could hold a claim indefinitely.** When a run timed out, `chronos-run.sh` sent TERM and then ran `wait`
   with no limit. A child that ignores TERM (a command job, or a stuck `claude`) kept the claim and the run open until it exited
   on its own. Now the script waits 10 s and then sends kill -9 to the whole process tree. Commits d730b37 (test), a3ddb3e (fix).
2. **A negative `Content-Length` hung a UI server thread.** `int("-1")` passed the size check, and `rfile.read(-1)` then waited
   for the client to close. This hit both the token routes and `POST /hook/<id>`. Both now return 400. Commits 7b69504, df029fe.
3. **Trailing newlines got through validation.** `RUN_ID_RE`, `DS_RE` (runlog), `MODEL_RE` and `TOOL_RE` ended in `$`, which also
   matches before a final newline. This is the same class of bug R7 fixed for job ids. `chronos api claim` stored a run id that
   ended in `\n`, `chronos claim <id> <date>\n` made a second claim key, and a hand-edited `"model": "opus\n"` reached
   `--model`. All of these now use `\Z`. Commits 7b69504, ecee4d2.
4. **`chronos prompt <unknown id>` printed a Python traceback.** It now prints one error line and exits 1. Commits 76fcf76, 9df05f8.
5. **A full webhook queue gave the wrong reason.** The 429 for a full queue (20 deliveries) blamed `event_rate_per_hour`.
   Commits 76fcf76, 4cf314c.

## 4. New tests that pass on the current code (coverage for risky paths)

- `chronos-notify` passes a message containing `$(...)`, backticks, quotes and `;` as one literal argument, and does nothing when no notify command is set.
- `chronos-run.sh` refuses a bad job id, date, event hash or event type before it touches state. A relaunch after success skips the run and still releases the claim.
- Tick: `CHRONOS_DRYRUN` claims nothing, spawns nothing and writes no heartbeat. An unparseable `jobs.json` logs `TICK-ERROR` and exits 0. One invalid record does not block the others.
- `validate_schedule` refuses a `catchup_min` outside 15 to 1440.
- `install.sh`: a second run keeps the config, jobs and token, and ignores a new `--port`. `--force-config` rewrites only the config. A bad option or port exits 2 before it writes anything.

## 5. Docs checked against the code

README.md, docs/triggers.md, docs/control-room.md and the `bin/chronos` help text. control-room.md was already correct.

- **New: "First job in 10 minutes"** (install, a `jobs.json` entry and `prompt.md`, `chronos list`, `chronos prompt`, `chronos run`,
  where the log and report land). I ran the job record and the `list`, `run` and second-`run` steps against a throwaway home with
  the fake `claude`. I checked `install.sh` by reading it and through the installer tests (with stand-ins, not on a real Mac).
- Fixed: the `features` list (`memory` was missing in 2 places), the test count, the examples list (`pdf-digest`), the
  `session_wait_notify_min` note (it said the notification was not shipped yet, but it is), the `uninstall.sh --yes` and purge
  prompt, and the CLI help (`due` has no `--dry`; the `api` verb list was incomplete).
- Added: the config keys `launch_timeout_s`, `probe_timeout_s`, `env_pass` and `memory_dir`. The environment variables
  `CHRONOS_CONFIG`, `CHRONOS_UI_PORT`, `CHRONOS_HOOK_HOSTS`, `CHRONOS_GMAIL_COMMAND`, `CHRONOS_GH`, `CHRONOS_NOTIFY_ENV`,
  `CHRONOS_GMAIL_ENV`, `CHRONOS_LAUNCHAGENTS_DIR` and `CHRONOS_NO_LAUNCHCTL`, plus the variables a run and the notify command get.
  The `headless` job field, the `run-result` and `memory-binding` API verbs, a Job memory section, the full event-run flags
  (`--setting-sources=`, `~/.config` deny), and that `content_hash` covers `triggers` and `meta`.
- docs/triggers.md: the trigger pass is skipped while `headless.blocked` exists, and webhook deliveries expire after 24 h (it said "nothing is dropped").

## 6. Left as is (worth a look)

- `env_pass` and the scrubbed environment apply only to Run now, `chronos run` and `chronos api start`. Runs the tick spawns
  get launchd's environment unchanged. The README now says which runs it covers. Whether tick runs should be scrubbed too is a design call.
- The UI's read routes (`/api/jobs`, `/api/runs`, `/api/file`, `/api/history`, `/api/usage`, `/api/control`) check the Host
  header but not the token. Any local process can read logs and prompts, while agent files need the token even to read.
- `install.sh` renders plists with `sed s|...|$PATH|`. A home path containing `&` or `|` would render wrong, and the `&` would
  not be XML-escaped. This is unlikely on macOS.
- chronos_runlog.py: the probe docstring says it clears `headless.blocked` first, but the code keeps the block until the probe answers OK. The README describes the code.
- CHANGELOG 0.5.0 says "a job can carry a `memory_dir`", but it is a config key. I did not edit the CHANGELOG, because its entries record history.
- Not checked: anything that needs a real Mac (launchctl, TCC prompts, `plutil`), and how Claude Code 2.1.x behaves.
