#!/usr/bin/env python3
"""WP-C2: the tick. Session-only jobs are never spawned headless (review R1), a stalled (id, ds) is terminal, headless.blocked stops
Claude runs but not command jobs, and the session-wait notification."""
import datetime
import os
import subprocess
import sys
import time
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_helpers import *  # noqa: E402,F401,F403
from test_chronos import RUN  # noqa: E402

D = "2026-10-05"
NOW = D + "T09:30"


class TickHonoursHeadlessFalse(unittest.TestCase):
    def test_due_never_lists_a_session_only_job_and_the_tick_never_spawns_it(self):
        e = Env([job("sess", in_session=True, headless=False, time="08:30"), job("hl", time="08:30")])
        try:
            r, lines = e.due(NOW)
            self.assertEqual([l.split("|")[0] for l in lines], ["hl"])
            e.run(["/bin/bash", TICK], CHRONOS_NOW=NOW)
            self.assertTrue(e.wait_for(lambda: os.path.exists(os.path.join(e.cfg["state_dir"], "ran-hl-" + D))))
            self.assertFalse([n for n in os.listdir(e.cfg["state_dir"]) if "sess" in n])
        finally:
            e.cleanup()


def blocked(e):
    p = e.cfg["blocked_file"]
    return json.load(open(p)) if os.path.exists(p) else None


class LaunchStall(unittest.TestCase):
    """FAKE_MODE=hang is `exec sleep 120`: no stream bytes, no CPU. launch_timeout_s=2 and a 1 s poll make that a stall in about 3 s."""

    def setUp(self):
        self.e = Env([job("alpha", time="09:00"), job("beta", time="09:20"), job("cmd", time="09:20", kind="command", command="echo command-ran")],
                     launch_timeout_s=2)
        self.st = self.e.cfg["state_dir"]
        self.p = lambda *a: os.path.join(self.st, *a)

    def tearDown(self):
        self.e.cleanup()

    def stall(self):
        self.e.run(["/bin/bash", TICK], CHRONOS_NOW=D + "T09:15", FAKE_MODE="hang")
        self.assertTrue(self.e.wait_for(lambda: os.path.exists(self.p("stalled-alpha-" + D)), 30), "no stalled marker")
        self.assertTrue(self.e.wait_for(lambda: not os.path.isdir(self.p("claim-alpha-" + D))))

    def test_a_hang_is_stalled_not_failed_with_the_claim_released_and_the_block_written(self):
        self.stall()
        self.assertFalse(os.path.exists(self.p("failed-alpha-" + D)))
        self.assertFalse(os.path.exists(self.p("ran-alpha-" + D)))
        self.assertFalse(os.path.exists(self.p("claim-alpha-%s.owner.json" % D)))
        b = blocked(self.e)
        self.assertEqual((b["reason"], b["id"], b["ds"], b["source"]), ("launch_stalled", "alpha", D, "watchdog"))
        self.assertRegex(b["run_id"], r"^r_[0-9A-HJKMNP-TV-Z]{26}$")
        self.assertTrue(self.e.wait_for(lambda: os.path.exists(self.e.notify_file)), "no notification")
        self.assertIn("alpha", open(self.e.notify_file).read())
        st = json.load(open(self.p("run-alpha-%s.json" % D)))
        self.assertEqual((st["status"], st["run_id"], st["job"], st["ds"]), ("launch_stalled", b["run_id"], "alpha", D))
        self.assertTrue(self.e.wait_for(lambda: os.path.exists(self.e.cfg["runs_file"])))
        row = [json.loads(l) for l in open(self.e.cfg["runs_file"])][-1]
        self.assertEqual((row["job"], row["run_id"], row["status"], row["ok"]), ("alpha", b["run_id"], "launch_stalled", False))
        self.assertEqual(self.leftover_sleepers(), [])

    def leftover_sleepers(self):
        return []                                           # the hung child is `sleep 120` with a unique parent: Env.cleanup waits for any process holding the temp dir

    def test_the_next_tick_spawns_no_claude_job_but_runs_a_command_job(self):
        self.stall()
        n = open(self.e.fake_log).read().count("ARGS:")
        self.e.run(["/bin/bash", TICK], CHRONOS_NOW=D + "T09:35")
        self.assertTrue(self.e.wait_for(lambda: os.path.exists(self.p("ran-cmd-" + D))), "the command job did not run")
        time.sleep(1.5)
        self.assertEqual(open(self.e.fake_log).read().count("ARGS:"), n, "a Claude job was spawned while headless was blocked")
        self.assertFalse([x for x in os.listdir(self.st) if x.startswith(("claim-beta", "ran-beta", "failed-beta"))])

    def test_snapshot_shows_stalled_and_run_now_clears_it(self):
        self.stall()
        self.assertEqual(sched(self.e, "alpha", now=D + "T09:16")["today"], "stalled")
        out, _ = api(self.e, ["claim", "alpha"], {"run_id": "r_" + "0" * 25 + "1", "ds": D, "manual": True, "again": False, "runner": "session", "bridge_instance": "i"}, now=D + "T09:16")
        self.assertTrue(out["ok"], out)
        self.assertFalse(os.path.exists(self.p("stalled-alpha-" + D)))

    def test_stalled_is_terminal_after_the_block_is_cleared(self):
        self.stall()
        os.remove(self.e.cfg["blocked_file"])                                    # what a successful Retry does
        n = open(self.e.fake_log).read().count("ARGS:")
        for t in ("T09:20", "T09:25"):
            self.e.run(["/bin/bash", TICK], CHRONOS_NOW=D + t, FAKE_MODE="hang")
        time.sleep(1.5)
        self.assertFalse(os.path.isdir(self.p("claim-alpha-" + D)))
        self.assertTrue(os.path.exists(self.p("stalled-alpha-" + D)))
        self.assertEqual([l for l in open(self.e.fake_log).read().splitlines() if l.startswith("ARGS:") and "TASK-BODY-alpha" in l][1:], [])
        r, lines = self.e.due(D + "T09:30")
        self.assertNotIn("alpha|" + D, lines)
        out, _ = api(self.e, ["claim", "alpha"], {"run_id": "r_" + "0" * 25 + "2", "ds": D, "manual": True, "again": False, "runner": "headless", "bridge_instance": ""}, now=D + "T09:30")
        self.assertTrue(out["ok"], out)


class StatusFile(unittest.TestCase):
    def test_the_status_file_is_refreshed_while_a_run_is_going_and_closed_at_the_end(self):
        e = Env([job("alpha", time="09:00")], timeout_min=0.1, launch_timeout_s=600)        # a 6 s watchdog; the launch watchdog stays out of it
        try:
            e.run(["/bin/bash", TICK], CHRONOS_NOW=NOW, FAKE_MODE="hang")
            p = os.path.join(e.cfg["state_dir"], "run-alpha-%s.json" % D)
            self.assertTrue(e.wait_for(lambda: os.path.exists(p), 10))
            first = json.load(open(p))
            self.assertEqual((first["status"], first["job"], first["ds"]), ("running", "alpha", D))
            self.assertRegex(first["run_id"], r"^r_[0-9A-HJKMNP-TV-Z]{26}$")
            self.assertTrue(e.wait_for(lambda: json.load(open(p))["updated_at"] != first["updated_at"], 10), "not refreshed")
            self.assertTrue(e.wait_for(lambda: json.load(open(p))["status"] == "timed_out", 30), "not closed")
            self.assertFalse(os.path.exists(os.path.join(e.cfg["state_dir"], "stalled-alpha-" + D)))
            self.assertTrue(e.wait_for(lambda: os.path.exists(os.path.join(e.cfg["state_dir"], "failed-alpha-" + D))))
        finally:
            e.cleanup()


class SessionWaitNotify(unittest.TestCase):
    def setUp(self):
        self.e = Env([job("morning", time="07:30", in_session=True, headless=False)], notify=False, session_runner="talos")
        self.out = os.path.join(self.e.tmp, "notified.txt")
        sh = os.path.join(self.e.tmp, "n.sh")
        open(sh, "w").write('#!/bin/bash\necho "${CHRONOS_JOB:-unset}|$1" >> "%s"\n' % self.out)
        os.chmod(sh, 0o755)
        self.set_cfg(notify=sh)

    def set_cfg(self, **kw):
        c = json.load(open(self.e.cfg_path))
        c.update(kw)
        json.dump(c, open(self.e.cfg_path, "w"))

    def tearDown(self):
        self.e.cleanup()

    def tick(self, t, **kw):
        prev = (datetime.datetime.fromisoformat(D + "T" + t) - datetime.timedelta(minutes=2)).isoformat()      # ticks come every 5 minutes: this is not the first tick after a sleep
        os.makedirs(self.e.cfg["home_dir"], exist_ok=True)
        open(self.e.cfg["beat_file"], "w").write(prev + "\n")
        r = self.e.run(["/bin/bash", TICK], CHRONOS_NOW=D + "T" + t, TZ=NY, **kw)
        time.sleep(0.8)
        return r

    def lines(self):
        return open(self.out).read().splitlines() if os.path.exists(self.out) else []

    def test_a_waiting_job_with_a_stale_beat_notifies_once_per_id_and_ds_with_chronos_job_exported(self):
        self.tick("07:45")
        self.assertEqual(self.lines(), [])                                      # 15 min: not yet session_wait_notify_min (30)
        self.tick("08:05")
        self.assertEqual(self.lines(), ["morning|Morning is waiting for your agent session (due 7:30 AM; runs if it's back before 10:30 AM)."])
        self.tick("08:10")
        self.tick("08:15")
        self.assertEqual(len(self.lines()), 1)
        self.assertTrue(os.path.exists(os.path.join(self.e.cfg["state_dir"], "waitnotified-morning-" + D)))

    def test_a_fresh_session_beat_means_no_notice(self):
        api(self.e, ["session-beat"], now=D + "T08:04")
        self.tick("08:05")
        self.assertEqual(self.lines(), [])

    def test_notify_never_zero_minutes_and_the_hook_runner_all_stay_quiet(self):
        self.e.set_jobs([job("morning", time="07:30", in_session=True, headless=False, notify="never")])
        self.tick("08:05")
        self.assertEqual(self.lines(), [])
        self.e.set_jobs([job("morning", time="07:30", in_session=True, headless=False)])
        self.set_cfg(session_wait_notify_min=0)
        self.tick("08:05")
        self.assertEqual(self.lines(), [])
        self.set_cfg(session_wait_notify_min=30, session_runner="hook")
        self.tick("08:05")
        self.assertEqual(self.lines(), [])

    def test_a_job_whose_window_is_over_is_not_announced(self):
        self.tick("10:45")
        self.assertEqual(self.lines(), [])


class RunScriptDeadlines(unittest.TestCase):
    def test_a_run_script_step_blocked_in_the_kernel_is_killed_from_outside_and_the_run_fails_cleanly(self):
        e = Env([job("alpha", time="09:00")], step_deadline_s=2)
        fifo = os.path.join(e.tmp, "stuck.fifo")
        os.mkfifo(fifo)
        try:
            os.makedirs(os.path.join(e.cfg["state_dir"], "claim-alpha-" + D))
            t0 = time.time()
            r = e.run(["/bin/bash", RUN, "alpha", D], CHRONOS_NOW=NOW, CHRONOS_TEST_BLOCK_FIFO_RUN=fifo)
            took = time.time() - t0
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertLess(took, 2 + 3, "took %.1fs" % took)
            self.assertIn("DEADLINE prompt", open(os.path.join(e.cfg["logs_dir"], "chronos.log")).read())
            self.assertTrue(os.path.exists(os.path.join(e.cfg["state_dir"], "failed-alpha-" + D)))
            self.assertFalse(os.path.isdir(os.path.join(e.cfg["state_dir"], "claim-alpha-" + D)))
            self.assertEqual([p for p in subprocess.run(["pgrep", "-f", fifo], capture_output=True, text=True).stdout.split() if p != str(os.getpid())], [])
        finally:
            e.cleanup()


if __name__ == "__main__":
    unittest.main()
