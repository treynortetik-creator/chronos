#!/usr/bin/env python3
"""Generate tests/golden/api/*.json: the real `chronos api` CLI, run against a throwaway HOME with a fixed clock and zone.

    python3 tests/golden.py            (or: tests/run.sh --golden)   rewrites tests/golden/api/
    golden.generate(dir) -> [names]   used by tests/test_api_golden.py to prove the committed files are what the CLI prints

Each file is the verb's stdout exactly as printed (errors included), with the temp HOME replaced by "$HOME". Downstream
consumers vendor these as contract fixtures. Nothing here touches a real install: HOME is a temp dir, there is no
`claude`, launchd or network involved.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "bin", "chronos")
NOW = "2026-10-05T07:45"            # a Monday
ZONE = "America/New_York"


def generate(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    home = os.path.realpath(tempfile.mkdtemp(prefix="chronos-golden-home-"))
    made = []
    env = {k: v for k, v in os.environ.items() if not k.startswith("CHRONOS_") and k not in ("TZ", "HOME")}
    env.update({"HOME": home, "TZ": ZONE, "CHRONOS_TEST_MODE": "1", "PATH": os.environ.get("PATH", "/usr/bin:/bin")})
    cfg_dir = os.path.join(home, ".config", "chronos")
    jobs_file = os.path.join(cfg_dir, "jobs.json")

    def norm(text):
        return text.replace(home, "$HOME")

    def run(name, args, payload=None, now=NOW, stdin=None, expect=0, save=True, extra=None):
        e = dict(env, CHRONOS_NOW=now, **(extra or {}))
        data = stdin if stdin is not None else (json.dumps(payload) if payload is not None else "")
        r = subprocess.run([sys.executable, CLI, "api"] + args, env=e, input=data, capture_output=True, text=True, timeout=60)
        if r.returncode != expect:
            raise RuntimeError("%s: exit %s (wanted %s)\n%s\n%s" % (name, r.returncode, expect, r.stdout, r.stderr))
        if save:
            with open(os.path.join(out_dir, name + ".json"), "w", encoding="utf-8") as fh:
                fh.write(norm(r.stdout))
            made.append(name)
        return json.loads(r.stdout)

    def draft(jid, **kw):
        d = {"id": jid, "name": jid.replace("-", " ").title(), "description": "Example job", "when": {"type": "weekdays", "time": "07:30"},
             "catchup_min": 180, "enabled": True, "runner": "session", "permissions": "full", "allowed_tools": [], "model": None,
             "notify": "failure", "prompt": "Summarise yesterday's activity and post it.\n"}
        d.update(kw)
        return {"op": "create", "draft": d}

    def create(jid, golden=None, **kw):
        v = run("validate-" + golden if golden else "_v", ["validate"], draft(jid, **kw), save=bool(golden))
        return run("apply-" + golden if golden else "_a", ["apply"], {"change": v["normalized"], "expected_etag": v["expected_etag"]}, save=bool(golden))

    def snapshot_job(jid, now=NOW):
        return next(s for s in run("_s", ["snapshot"], now=now, save=False)["schedules"] if s["id"] == jid)

    def hand_job(rec, prompt):
        jobs = json.load(open(jobs_file)) if os.path.exists(jobs_file) else []
        jobs.append(rec)
        json.dump(jobs, open(jobs_file, "w"), indent=2)
        os.makedirs(os.path.join(home, ".config", "chronos", "jobs", rec["id"]), exist_ok=True)
        open(os.path.join(home, ".config", "chronos", "jobs", rec["id"], "prompt.md"), "w").write(prompt)

    try:
        run("version", ["version"])
        run("snapshot-empty", ["snapshot"])
        run("health-empty", ["health"])                # a new install: no jobs.json yet
        create("daily-digest", golden="create")
        create("weekly-cleanup", when={"type": "days", "days": ["fri"], "time": "17:00"}, runner="headless", permissions="restricted",
               allowed_tools=["Read", "Grep"], model="sonnet", prompt="Tidy the scratch folder.\n")
        create("remind-me", when={"type": "once", "at": "2026-10-09T15:00"}, runner="headless", prompt="Send the reminder.\n")
        run("snapshot-three-jobs", ["snapshot"])
        run("detail", ["detail", "daily-digest"])

        before = snapshot_job("daily-digest")
        change = {"op": "update", "id": "daily-digest", "base_etag": before["etag"], "set": {"name": "Morning Digest", "catchup_min": 120, "prompt": "Summarise yesterday's activity.\n"}}
        v = run("validate-update", ["validate"], change)
        run("apply-update", ["apply"], {"change": v["normalized"], "expected_etag": v["expected_etag"]})
        run("apply-conflict", ["apply"], {"change": change, "expected_etag": v["expected_etag"]}, expect=1)     # the etag it was based on is stale now
        run("validate-error", ["validate"], draft("Bad Id", catchup_min=5, notify="loud"), expect=1)

        create("scratch-job")
        d = snapshot_job("scratch-job")
        v = run("validate-delete", ["validate"], {"op": "delete", "id": "scratch-job", "base_etag": d["etag"]})
        run("apply-delete", ["apply"], {"change": v["normalized"], "expected_etag": None})

        run("pause-job", ["pause"], {"scope": {"id": "remind-me"}, "paused": True})
        run("pause-all", ["pause"], {"scope": "all", "paused": True})
        run("_u1", ["pause"], {"scope": "all", "paused": False}, save=False)
        run("_u2", ["pause"], {"scope": {"id": "remind-me"}, "paused": False}, save=False)
        run("disable", ["disable", "remind-me"])

        run("session-beat", ["session-beat"])
        run("due-session", ["due", "--for", "session"])
        create("night-report", when={"type": "daily", "time": "23:50"})
        run("due-session-midnight", ["due", "--for", "session"], now="2026-10-06T00:05")

        run("approved", ["approved", "daily-digest"])
        run("approved-none", ["approved", "no-such-job"])

        hand_job({"id": "legacy-both", "name": "Legacy Both", "description": "written by hand", "time": "07:30", "days": "weekdays",
                  "catchup_min": 180, "enabled": True, "in_session": True, "notify": "failure", "once": None,
                  "created": "2026-09-01T00:00:00", "updated": "2026-09-01T00:00:00"}, "Do the old thing.\n")
        run("config-set-refused", ["config", "set", "session_runner", "talos", "approval_required", "true"], expect=1)
        run("config-set", ["config", "set", "approval_required", "true"])
        run("snapshot-held", ["snapshot"])
        s = snapshot_job("legacy-both")
        run("apply-adopt", ["apply"], {"change": {"op": "adopt", "id": "legacy-both", "content_hash": s["content_hash"]}, "expected_etag": s["etag"]})
        hand_job({"id": "hand-written", "name": "Hand Written", "description": "written by hand", "time": "08:00", "days": "daily",
                  "catchup_min": 180, "enabled": True, "in_session": False, "notify": "failure", "once": None,
                  "created": "2026-09-01T00:00:00", "updated": "2026-09-01T00:00:00"}, "Do the hand-written thing.\n")
        run("migrate", ["migrate"], stdin="y\ny\n")
        run("error-unknown-verb", ["frobnicate"], expect=1)

        # ---- the run verbs (WP-C2). A fake claude and a fake launchctl stand in for the real ones; run ids come from the caller.
        cfgp = os.path.join(cfg_dir, "config.json")
        conf = json.load(open(cfgp)) if os.path.exists(cfgp) else {}
        conf.update({"claude_bin": os.path.join(ROOT, "tests", "fake-claude.sh"), "probe_timeout_s": 2})
        json.dump(conf, open(cfgp, "w"), indent=2)
        os.makedirs(os.path.join(home, "chronos-workspace"), exist_ok=True)
        probe_env = {"CHRONOS_LAUNCHCTL": os.path.join(ROOT, "tests", "fake-launchctl.sh"), "FAKE_LAUNCHCTL_DIR": os.path.join(home, "launchd")}
        rid1, rid2 = "r_" + "0" * 25 + "1", "r_" + "0" * 25 + "2"
        creq = lambda rid: {"run_id": rid, "ds": "2026-10-05", "fire_at": "2026-10-05T07:30:00-04:00", "manual": False, "again": False,
                            "runner": "session", "bridge_instance": "instance-a"}
        create("run-session", when={"type": "daily", "time": "07:30"})
        run("claim-ok", ["claim", "run-session"], creq(rid1))
        run("claim-running-elsewhere", ["claim", "run-session"], creq(rid2))
        run("orphans", ["orphans", "--bridge-instance", "instance-b"])
        run("finish-not-owner", ["finish", "run-session"], {"run_id": rid2, "status": "ok", "report": "Not mine.\n"})
        run("finish-ok", ["finish", "run-session"], {"run_id": rid1, "status": "ok", "report": "Posted the summary.\n"})
        run("finish-late", ["finish", "run-session"], {"run_id": rid1, "status": "ok", "report": "Too late.\n"})
        run("detail-with-runs", ["detail", "run-session"])
        run("report", ["report", "run-session", "--run", rid1])
        run("report-not-found", ["report", "run-session", "--run", rid2], expect=1)
        run("session-prompt", ["session-prompt", "run-session"])
        hand_job({"id": "hand-session", "name": "Hand Session", "description": "written by hand", "time": "08:00", "days": "daily",
                  "catchup_min": 180, "enabled": True, "in_session": True, "headless": False, "notify": "failure", "once": None,
                  "created": "2026-09-01T00:00:00", "updated": "2026-09-01T00:00:00"}, "Do the hand session thing.\n")
        hand_job({"id": "hand-bg", "name": "Hand Bg", "description": "written by hand", "time": "08:00", "days": "daily",
                  "catchup_min": 180, "enabled": True, "in_session": False, "notify": "failure", "once": None,
                  "created": "2026-09-01T00:00:00", "updated": "2026-09-01T00:00:00"}, "Do the hand background thing.\n")
        run("session-prompt-held", ["session-prompt", "hand-session"], expect=1)
        run("start-held", ["start", "hand-bg"], {"again": False})
        run("start-session-only", ["start", "daily-digest"], {"again": False})
        os.makedirs(os.path.join(home, ".chronos"), exist_ok=True)
        json.dump({"reason": "launch_stalled", "since": "2026-10-05T07:40:00-04:00", "id": "weekly-cleanup", "ds": "2026-10-05", "run_id": rid2,
                   "source": "watchdog", "tcc_suspected": False}, open(os.path.join(home, ".chronos", "headless.blocked"), "w"))
        run("start-headless-blocked", ["start", "weekly-cleanup"], {"again": False})
        run("health", ["health"])
        run("probe-stalled", ["probe"], {}, extra=dict(probe_env, FAKE_MODE="hang"))
        run("probe", ["probe"], {}, extra=probe_env)
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return made


if __name__ == "__main__":
    target = os.path.join(ROOT, "tests", "golden", "api")
    if os.path.isdir(target):
        for n in os.listdir(target):
            if n.endswith(".json"):
                os.remove(os.path.join(target, n))
    names = generate(target)
    print("wrote %d goldens to %s" % (len(names), target))
