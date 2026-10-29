#!/usr/bin/env python3
"""WP-C1 (N3): deadlines enforced from OUTSIDE the process. The test makes a step really block in the kernel (open() of a FIFO
that has no writer); a monkeypatched open() would never prove that, and an in-process alarm only runs after the syscall returns."""
import os
import subprocess
import sys
import time
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_helpers import *  # noqa: E402,F401,F403

NOW = "2026-10-05T09:00"


class Deadlines(unittest.TestCase):
    def setUp(self):
        self.e = Env([job("alpha", time="08:30")], step_deadline_s=2, trigger_deadline_s=2)
        self.fifo = os.path.join(self.e.tmp, "stuck.fifo")
        os.mkfifo(self.fifo)

    def tearDown(self):
        self.e.cleanup()

    def log(self):
        p = os.path.join(self.e.cfg["logs_dir"], "chronos.log")
        return open(p).read() if os.path.exists(p) else ""

    def leftovers(self):
        r = subprocess.run(["pgrep", "-f", self.fifo], capture_output=True, text=True)
        return [p for p in r.stdout.split() if p != str(os.getpid())]

    def test_env_exports_the_deadlines(self):
        r = self.e.run([sys.executable, CLI, "env"])
        self.assertIn("CH_STEP_DEADLINE_S=2", r.stdout)
        self.assertIn("CH_TRIGGER_DEADLINE_S=2", r.stdout)
        self.assertEqual(Env([job("x")]).cfg["step_deadline_s"], 60)          # defaults: 60 for a step, 150 for the trigger pass
        self.assertEqual(C.DEFAULTS["trigger_deadline_s"], 150)

    def test_a_due_check_blocked_in_the_kernel_is_killed_logged_and_the_next_tick_runs(self):
        t0 = time.time()
        r = self.e.run(["/bin/bash", TICK], CHRONOS_NOW=NOW, CHRONOS_TEST_BLOCK_FIFO=self.fifo)
        took = time.time() - t0
        self.assertEqual(r.returncode, 0, r.stderr)                           # the tick costs one tick, never a hang or a crash
        self.assertLess(took, 2 + 2 + 3, "the tick was not cut off at step_deadline_s (+2 s): took %.1fs" % took)
        self.assertGreaterEqual(took, 1.5, "it finished before the deadline, so the step was not really blocked")
        self.assertIn("DEADLINE due", self.log())
        self.assertEqual(self.leftovers(), [], "the blocked child is still alive")
        self.assertEqual([n for n in os.listdir(self.e.cfg["state_dir"]) if n.startswith(("claim-", "ran-"))], [])
        # the very next tick, with nothing blocking, picks the job up
        r = self.e.run(["/bin/bash", TICK], CHRONOS_NOW=NOW)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.e.wait_for(lambda: os.path.exists(os.path.join(self.e.cfg["state_dir"], "ran-alpha-2026-10-05"))), "the next tick did not run the job")

    def test_a_blocked_trigger_pass_is_killed_after_the_clock_jobs_were_claimed(self):
        t0 = time.time()
        r = self.e.run(["/bin/bash", TICK], CHRONOS_NOW=NOW, CHRONOS_TEST_BLOCK_FIFO_TRIGGERS=self.fifo)
        took = time.time() - t0
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(took, 2 + 2 + 3, "took %.1fs" % took)
        self.assertIn("DEADLINE triggers", self.log())
        self.assertEqual(self.leftovers(), [])
        self.assertTrue(self.e.wait_for(lambda: os.path.exists(os.path.join(self.e.cfg["state_dir"], "ran-alpha-2026-10-05"))), "the clock job should still have run")

    def test_a_normal_tick_logs_no_deadline_and_leaves_no_watchdog_behind(self):
        r = self.e.run(["/bin/bash", TICK], CHRONOS_NOW=NOW)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.e.wait_for(lambda: os.path.exists(os.path.join(self.e.cfg["state_dir"], "ran-alpha-2026-10-05"))))
        self.assertNotIn("DEADLINE", self.log())
        time.sleep(0.5)
        r = subprocess.run(["pgrep", "-f", "sleep 2"], capture_output=True, text=True)
        self.assertEqual([p for p in r.stdout.split() if p != str(os.getpid())], [], "a deadline watchdog outlived the step it guarded")


if __name__ == "__main__":
    unittest.main(verbosity=2, warnings="ignore")
