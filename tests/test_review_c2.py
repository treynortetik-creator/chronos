#!/usr/bin/env python3
"""Adversarial review of WP-C2 (0.3 runs). Every test here FAILS on feat/0.3-runs @ e5e8b7e and names the finding it pins
(see the review's R-numbers). Real CLI runs only: the tick, the run script and `chronos api`, in a throwaway home, with a
temp HOME and the fake claude."""
import os
import tempfile

os.environ["HOME"] = tempfile.mkdtemp(prefix="chronos-review-home-")      # never the real HOME (set before chronoslib is imported)

import json  # noqa: E402
import signal  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
import warnings  # noqa: E402

warnings.simplefilter("ignore", ResourceWarning)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_helpers import *  # noqa: E402,F401,F403
from test_chronos import CLI, TICK, ROOT, make_old  # noqa: E402

RUN = os.path.join(ROOT, "bin", "chronos-run.sh")
D = "2026-10-05"


def launches(e):
    return open(e.fake_log).read().count("ARGS:") if os.path.exists(e.fake_log) else 0


def kill_tree_of(e):
    # Kill by PID only: list the processes under this test's own temp dir, then signal each PID.
    r = subprocess.run(["pgrep", "-f", e.tmp], capture_output=True, text=True)
    for pid in r.stdout.split():
        if pid.isdigit() and int(pid) != os.getpid():
            try:
                os.kill(int(pid), signal.SIGTERM)
            except ProcessLookupError:
                pass


class R1StaleSweepDoubleFiresALiveRun(unittest.TestCase):
    """The stale sweep is age-only. A run that is still alive when its claim passes stale_claim_min (Mac asleep mid-run, or
    timeout_min >= stale_claim_min) is swept and the next tick spawns a second copy beside it."""

    def test_a_claim_whose_run_is_alive_is_never_swept(self):
        e = Env([job("cmd", time="09:00", kind="command", command="echo start >> \"$CHRONOS_JOB.count\"; sleep 6")])
        try:
            cnt = os.path.join(e.cfg["workspace"], "cmd.count")
            e.run(["/bin/bash", TICK], CHRONOS_NOW=D + "T09:15")
            self.assertTrue(e.wait_for(lambda: os.path.exists(cnt), 10))
            e.run(["/bin/bash", TICK], CHRONOS_NOW=D + "T10:01")          # 46 min later on the scheduler's clock; run 1 is still alive
            time.sleep(2)
            self.assertEqual(open(cnt).read().count("start"), 1, "a second run was spawned while the first was still running")
        finally:
            e.cleanup()


class R2TheTickWaitsOnTheNotifyCommand(unittest.TestCase):
    """The session-wait notice runs chronos-notify in the tick's foreground with no deadline. A notify command that hangs (a
    network call) hangs the tick: the event pass never runs and launchd starts no new tick while this one lives."""

    def test_a_hanging_notify_command_costs_at_most_one_step_deadline(self):
        e = Env([job("sess", time="08:00", in_session=True, headless=False)], session_runner="talos", notify=False, step_deadline_s=3)
        hang = os.path.join(e.tmp, "hang-notify.sh")
        open(hang, "w").write("#!/bin/bash\nsleep 60\n")
        os.chmod(hang, 0o755)
        c = json.load(open(e.cfg_path))
        c["notify"] = hang
        json.dump(c, open(e.cfg_path, "w"))
        try:
            p = subprocess.Popen(["/bin/bash", TICK], env=e.env(CHRONOS_NOW=D + "T08:45", TZ=NY), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                p.wait(timeout=3 + 7)
                finished = True
            except subprocess.TimeoutExpired:
                finished = False
                p.kill()
            self.assertTrue(finished, "the tick was still running 10 s after start (step_deadline_s=3): blocked on the notify command")
        finally:
            kill_tree_of(e)
            e.cleanup()


class R3TheEventPassLaunchesASessionOnlyJobHeadless(unittest.TestCase):
    """The trigger pass (chronos_triggers.tick) never reads `headless`, so a session-only job that has a trigger is spawned
    as a headless `claude -p` by the tick. Triggers are outside content_hash, so a hand-added trigger is not even held."""

    def test_the_tick_never_spawns_a_headless_false_job_for_an_event(self):
        e = Env([], session_runner="talos", approval_required=True)
        try:
            self.assertTrue(apply_ok(e, create_change("sess-ev", runner="session"), now=D + "T07:00").get("ok"))
            w = os.path.join(e.tmp, "watch")
            os.makedirs(w)
            jobs = json.load(open(e.cfg["jobs_file"]))
            jobs[0]["triggers"] = [{"type": "file", "path": w, "glob": "*.txt"}]
            json.dump(jobs, open(e.cfg["jobs_file"], "w"))
            e.run([sys.executable, CLI, "triggers"])                     # baseline
            make_old(os.path.join(w, "new.txt"), "hello")
            e.run(["/bin/bash", TICK], CHRONOS_NOW=D + "T07:01")
            time.sleep(4)
            self.assertEqual(launches(e), 0, "the tick launched a session-only job headless (event run)")
        finally:
            e.cleanup()


class R4RunNowOutsideTheApiIgnoresHeadlessFalseAndTheBlock(unittest.TestCase):
    """`chronos run <id>` and the web UI's Run now (both C.start_run) spawn chronos-run.sh for a session-only job, and spawn
    headless Claude runs while headless.blocked exists. `chronos api start` refuses both."""

    def test_chronos_run_refuses_a_session_only_job(self):
        e = Env([job("sess", time="08:00", in_session=True, headless=False)], session_runner="talos")
        try:
            r = e.run([sys.executable, CLI, "run", "sess"], CHRONOS_NOW=D + "T08:45")
            time.sleep(3)
            self.assertNotEqual(r.returncode, 0, r.stdout)
            self.assertEqual(launches(e), 0, "a session-only job was run headless")
        finally:
            e.cleanup()

    def test_chronos_run_refuses_a_claude_job_while_headless_is_blocked(self):
        e = Env([job("bg", time="08:00")])
        try:
            open(e.cfg["blocked_file"], "w").write('{"reason":"launch_stalled"}')
            r = e.run([sys.executable, CLI, "run", "bg"], CHRONOS_NOW=D + "T08:45")
            time.sleep(3)
            self.assertEqual(launches(e), 0, "a headless Claude run started while headless.blocked exists")
        finally:
            e.cleanup()


class R5ProbeFailsOpenWhenItIsKilled(unittest.TestCase):
    """probe removes headless.blocked BEFORE it knows the answer and waits up to probe_timeout_s (120) in-process. The bridge
    kills adapter calls at 15 s (S2.4). Killed mid-wait, the block is gone for good, the one-off launchd job is never removed,
    and the next tick spawns headless runs into the same stall (each one a terminal `stalled-` day)."""

    def test_a_probe_killed_before_it_answers_leaves_the_block_in_place(self):
        e = Env([job("bg", time="07:30")], probe_timeout_s=30)
        lc = os.path.join(e.tmp, "launchd")
        try:
            open(e.cfg["blocked_file"], "w").write(json.dumps({"reason": "launch_stalled", "id": "bg", "ds": D}))
            env = e.env(CHRONOS_NOW=D + "T08:00", FAKE_MODE="hang", CHRONOS_LAUNCHCTL=os.path.join(ROOT, "tests", "fake-launchctl.sh"),
                        FAKE_LAUNCHCTL_DIR=lc)
            p = subprocess.Popen([sys.executable, CLI, "api", "probe"], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            time.sleep(3)
            p.send_signal(signal.SIGKILL)                                    # the bridge's adapter deadline
            p.wait()
            self.assertTrue(os.path.exists(e.cfg["blocked_file"]), "headless.blocked was cleared by a probe that never answered")
        finally:
            kill_tree_of(e)
            e.cleanup()


class R6TheTickNeverCatchesUpAcrossMidnight(unittest.TestCase):
    """due_jobs checks only today's fire. A headless job at 23:50-23:59 is never due from the tick (grace pushes it past
    midnight, where 'today' moves on), and a Mac asleep at 23:50 never catches up at 00:30, while the snapshot says `waiting`."""

    def test_a_2355_headless_job_is_due_after_midnight_on_yesterdays_ds(self):
        e = Env([job("nightly", time="23:55", days="daily")])
        try:
            r, lines = e.due("2026-10-06T00:10", TZ=NY)
            self.assertEqual(lines, ["nightly|2026-10-05"], r.stderr)
        finally:
            e.cleanup()

    def test_waking_at_0030_runs_a_2350_job_still_inside_its_window(self):
        e = Env([job("late", time="23:50", days="daily", kind="command", command="true")])
        try:
            self.assertEqual(sched(e, "late", now="2026-10-06T00:30")["today"], "waiting")
            e.run(["/bin/bash", TICK], CHRONOS_NOW="2026-10-06T00:30", TZ=NY)
            self.assertTrue(e.wait_for(lambda: os.path.exists(os.path.join(e.cfg["state_dir"], "ran-late-2026-10-05")), 10),
                            "the snapshot said waiting, but the tick never ran it")
        finally:
            e.cleanup()


class R7TheRunScriptIgnoresAStalledOrFailedDay(unittest.TestCase):
    """The tick's `chronos claim` and chronos-run.sh check only `ran-`. due_jobs filters stalled-/failed-, but nothing re-checks
    under the claim, so a day that stalls or fails between a tick's due list and its claim (two ticks at once) runs again."""

    def test_a_claimed_run_for_a_stalled_day_launches_nothing(self):
        e = Env([job("alpha", time="09:00")])
        try:
            os.makedirs(os.path.join(e.cfg["state_dir"], "claim-alpha-" + D))
            e.touch(e.cfg["state_dir"], "stalled-alpha-" + D)
            e.run(["/bin/bash", RUN, "alpha", D], CHRONOS_NOW=D + "T09:15")
            self.assertEqual(launches(e), 0, "the run script launched claude for a stalled (terminal) day")
        finally:
            e.cleanup()

    def test_the_tick_claim_refuses_a_failed_day(self):
        e = Env([job("alpha", time="09:00")])
        try:
            e.touch(e.cfg["state_dir"], "failed-alpha-" + D)
            r = e.run([sys.executable, CLI, "claim", "alpha"], CHRONOS_NOW=D + "T09:15")
            self.assertNotEqual(r.stdout.strip(), "CLAIMED")
        finally:
            e.cleanup()


class R8HeadlessRunsInheritTheCallersEnvironment(unittest.TestCase):
    """`chronos api start` spawns the run with dict(os.environ): whatever the bridge process holds (tokens, keys) reaches a
    `claude -p --dangerously-skip-permissions` run, along with CHRONOS_RUN_ID and the test seams."""

    def test_api_start_does_not_pass_the_callers_secrets_to_claude(self):
        e = Env([job("alpha", time="09:00")])
        dump = os.path.join(e.tmp, "envdump.txt")
        fake = os.path.join(e.tmp, "envclaude.sh")
        open(fake, "w").write("#!/bin/bash\nenv > %s\necho '{\"type\":\"system\"}'\n" % dump)
        os.chmod(fake, 0o755)
        c = json.load(open(e.cfg_path))
        c["claude_bin"] = fake
        json.dump(c, open(e.cfg_path, "w"))
        try:
            out, _ = api(e, ["start", "alpha"], {}, now=D + "T09:15", BRIDGE_RELAY_TOKEN="tok-not-for-jobs")
            self.assertTrue(out["ok"], out)
            self.assertTrue(e.wait_for(lambda: os.path.exists(dump), 10))
            time.sleep(0.5)
            leaked = "tok-not-for-jobs" in open(dump).read()
            os.remove(dump)                                                  # it holds the whole inherited environment: never print it
            self.assertFalse(leaked, "the caller's BRIDGE_RELAY_TOKEN reached the headless claude run")
        finally:
            e.cleanup()


class R9WaitNoticeRightAfterWake(unittest.TestCase):
    """On wake launchd runs the overdue tick before the bridge's first 60 s poll, so the session beat is always 'stale' at that
    moment and a live session gets a false 'waiting for your agent session' notice."""

    def test_no_notice_on_the_first_tick_after_a_sleep(self):
        e = Env([job("sess", time="08:30", in_session=True, headless=False)], session_runner="talos")
        try:
            api(e, ["session-beat"], {}, now=D + "T08:00")
            e.run(["/bin/bash", TICK], CHRONOS_NOW=D + "T08:00")
            e.run(["/bin/bash", TICK], CHRONOS_NOW=D + "T10:00")         # first tick after a 2 h sleep
            time.sleep(1)
            self.assertFalse(os.path.exists(e.notify_file) and open(e.notify_file).read().strip(),
                             "a notice went out before the session had a chance to beat after wake")
        finally:
            e.cleanup()


class R10FinishWritesWorldReadableFiles(unittest.TestCase):
    """finish (called by the bridge, umask 022) writes reports, done markers and runs.jsonl 0644. The run script writes the
    same files 0600 (umask 077). install.sh's 0700 home hides it only when the home is where install.sh put it."""

    def test_finish_writes_its_report_0600(self):
        old = os.umask(0o022)
        try:
            e = Env([job("sess", time="09:00", in_session=True, headless=False)], session_runner="talos")
            try:
                rid = "r_" + "0" * 25 + "1"
                api(e, ["claim", "sess"], {"run_id": rid, "ds": D, "runner": "session", "bridge_instance": "A"}, now=D + "T09:05")
                out, _ = api(e, ["finish", "sess"], {"run_id": rid, "status": "ok", "report": "private words"}, now=D + "T09:10")
                self.assertTrue(out["ok"], out)
                rep = os.path.join(e.cfg["reports_dir"], "sess-%s-%s.md" % (D, rid))
                self.assertEqual(oct(os.stat(rep).st_mode & 0o777), "0o600")
                self.assertEqual(oct(os.stat(e.cfg["runs_file"]).st_mode & 0o777), "0o600")
            finally:
                e.cleanup()
        finally:
            os.umask(old)


if __name__ == "__main__":
    unittest.main(verbosity=2)
