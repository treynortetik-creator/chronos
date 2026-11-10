#!/usr/bin/env python3
"""Gap tests (2026-11 audit): paths the older suites did not reach. Throwaway homes, fake clock, fake `claude`, no launchctl.

    python3 tests/test_gaps.py        (also run by tests/run.sh)
"""
import json
import os
import subprocess
import sys
import time
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_chronos import Env, job, C, CLI, TICK, RUN, FAKE, ROOT  # noqa: E402,F401

D = "2026-10-05"          # a Monday


def state(e, name):
    return os.path.join(e.cfg["state_dir"], name)


def settled(e, jid, ds=D):
    return lambda: ((os.path.exists(state(e, "ran-%s-%s" % (jid, ds))) or os.path.exists(state(e, "failed-%s-%s" % (jid, ds))))
                    and not os.path.isdir(state(e, "claim-%s-%s" % (jid, ds))))


class RunWrapperWatchdog(unittest.TestCase):
    def test_a_timed_out_command_that_ignores_sigterm_is_killed_and_releases_its_claim(self):
        # timeout_min 0.03 is about 2 s. The command ignores TERM and would sleep 90 s, holding the claim all that time.
        e = Env([job("cmd", kind="command", command="trap '' TERM; sleep 90")], timeout_min=0.03)
        self.addCleanup(e.cleanup)
        e.run(["/bin/bash", TICK], CHRONOS_NOW=D + "T09:30")
        t0 = time.time()
        self.assertTrue(e.wait_for(settled(e, "cmd"), 30), "the claim was still held 30 s after the timeout")
        self.assertLess(time.time() - t0, 30)
        self.assertTrue(os.path.exists(state(e, "failed-cmd-" + D)))
        st = json.load(open(state(e, "run-cmd-%s.json" % D)))
        self.assertEqual(st["status"], "timed_out")


if __name__ == "__main__":
    unittest.main(verbosity=1)
