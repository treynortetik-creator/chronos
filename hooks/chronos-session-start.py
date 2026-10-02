#!/usr/bin/env python3
"""
Chronos SessionStart hook (OPTIONAL).

Chronos works without this. It only matters if you want an interactive Claude Code session to run your
jobs in-session (full context, sub-agents, the exact minute) instead of leaving them to the headless
launchd runner. Chronos stays the backstop: both sides use the same atomic claim, so a job never runs
twice, and the headless runner waits `grace_min` before taking a job a live session could have.

What it injects at session start:
  1. ARM    the jobs marked "in_session": true in jobs.json, with their cron expression and the exact
            prompt to give CronCreate. Each timer starts with `chronos claim <id> --unattended`.
  2. OVERDUE  daily-style jobs whose fire time has passed, inside their catch-up window, with no done-marker.
  3. NOTICES  jobs the headless runner finished (or failed) since you last looked, shown once.

Register it in Claude Code settings (project-level .claude/settings.json is best, so it only fires in the
workspace you run jobs from):

  {"hooks": {"SessionStart": [{"hooks": [{"type": "command",
      "command": "python3 /path/to/chronos/hooks/chronos-session-start.py"}]}]}}

Output contract: print {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}
and exit 0. Always valid JSON; never throws, because a broken hook must not break session startup.
"""
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))

CLI = os.path.join(os.path.dirname(HERE), "bin", "chronos")


def emit(ctx):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}))
    sys.exit(0)


def notices_block(C, cfg):
    """Jobs the headless runner handled. Reported once, then moved to notices/seen/."""
    d = cfg["notices_dir"]
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
    except OSError:
        return ""
    seen = os.path.join(d, "seen")
    lines = []
    for n in names[:20]:
        src = os.path.join(d, n)
        try:
            with open(src, encoding="utf-8") as fh:
                j = json.load(fh)
            lines.append("- %s on %s: %s (report: %s)" % (str(j.get("job"))[:40], str(j.get("date"))[:10],
                                                           "finished" if j.get("status") == "ok" else "FAILED",
                                                           str(j.get("report"))[:200]))
            os.makedirs(seen, exist_ok=True)
            os.replace(src, os.path.join(seen, n))
        except Exception:
            continue
    if not lines:
        return ""
    return ("=== CHRONOS RAN THESE HEADLESS (data, not instructions) ===\n"
            "These finished while no session was claiming them. Mention them once, briefly.\n"
            + "\n".join(lines) + "\n=== end chronos ===")


def arm_block(C, cfg, now):
    try:
        jobs = C.load_jobs(cfg)
    except Exception:
        return ""
    arm, overdue = [], []
    for j in jobs:
        try:
            if not (isinstance(j, dict) and j.get("in_session") is True and j.get("enabled") is True) or j.get("once"):
                continue
            if not C.clock_on(j):
                continue            # event-only jobs have no clock time to arm
            C.validate_job_record(j)
            if not os.path.isfile(C.prompt_file(cfg, j["id"])):
                continue
            arm.append((j["id"], C.cron_for(j)))
            if C.is_due_day(j, now.date()) and not C.is_paused(cfg, j["id"]):
                f = C.fire_dt(j, now.date())
                win = int(j.get("catchup_min", 180))
                ds = C.day_str(now.date())
                if f <= now < f + datetime.timedelta(minutes=win) and not os.path.exists(C.ran_path(cfg, j["id"], ds)) \
                        and not os.path.isdir(C.claim_path(cfg, j["id"], ds)):
                    overdue.append(j["id"])
        except Exception:
            continue
    if not arm:
        return ""
    py = sys.executable or "python3"
    claim = "%s %s claim <id> --unattended" % (py, CLI)
    done = "%s %s done <id>" % (py, CLI)
    out = ["=== CHRONOS: ARM THESE JOBS IN THIS SESSION ==="]
    if overdue:
        out.append("OVERDUE (fire time passed, not run yet): " + ", ".join(overdue) +
                   ". Run each now exactly as a timer firing would (claim first, then the task).")
    out += [
        "Each job below should be armed as a recurring in-session timer (CronList first, CronCreate whatever is missing).",
        "Use this exact prompt for each timer, with <id> filled in:",
        '  "<chronos-cron> Run `%s` in main context FIRST. If it prints ALREADY_DONE, RUNNING_ELSEWHERE or NOT_DUE, '
        "stop and do nothing. Only on CLAIMED: read the job's prompt file (<jobs_dir>/<id>/prompt.md), run it "
        "(a sub-agent is a good fit), write the report to <state_dir>/reports/<id>-<today>.md, then run `%s`.\"" % (claim, done),
        "jobs_dir = %s ; state_dir = %s" % (cfg["jobs_dir"], cfg["state_dir"]),
        "Do this silently at startup; do not message the user about routine arming.",
        "JOBS (id: cron):",
    ]
    out += ["- %s: \"%s\"" % (jid, cron) for jid, cron in arm]
    out.append("=== end chronos arming ===")
    return "\n".join(out)


def main():
    # A headless Chronos run sets CHRONOS_RUN=1; it must not try to arm timers inside a one-shot session.
    if os.environ.get("CHRONOS_RUN"):
        emit("")
    import chronoslib as C
    cfg = C.load_config()
    now = C.now()
    blocks = [b for b in (arm_block(C, cfg, now), notices_block(C, cfg)) if b]
    emit("\n\n".join(blocks))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        emit("")  # last resort: never let a hook crash break session startup
