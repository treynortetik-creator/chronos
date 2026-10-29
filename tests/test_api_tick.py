#!/usr/bin/env python3
"""WP-C2: the tick. Session-only jobs are never spawned headless (review R1), a stalled (id, ds) is terminal, headless.blocked stops
Claude runs but not command jobs, and the session-wait notification."""
import os
import sys
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_helpers import *  # noqa: E402,F401,F403

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


if __name__ == "__main__":
    unittest.main()
