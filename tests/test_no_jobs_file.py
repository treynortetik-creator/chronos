#!/usr/bin/env python3
"""A new install has no jobs.json yet. Every reader treats that as an empty registry (as `api health` and `api snapshot`
already did in 0.3.1): no traceback, no "unreadable" error, nothing due. A jobs file that exists but cannot be parsed is
still an error. Real CLI, throwaway home, fixed clock."""
import os
import subprocess
import sys
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_helpers import *  # noqa: E402,F401,F403

NOW = "2026-10-05T09:00"


def cli(e, args):
    env = e.env(CHRONOS_NOW=NOW)
    return subprocess.run([sys.executable, CLI] + list(args), env=env, input="", capture_output=True, text=True, timeout=60)


class NoJobsFile(unittest.TestCase):
    def setUp(self):
        self.e = Env([])
        os.remove(self.e.cfg["jobs_file"])

    def tearDown(self):
        self.e.cleanup()

    def test_load_jobs_is_empty(self):
        self.assertEqual(C.load_jobs(self.e.cfg), [])

    def test_list_prints_nothing_and_exits_0(self):
        r = cli(self.e, ["list"])
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_due_is_empty_and_exits_0(self):
        r = cli(self.e, ["due"])
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_triggers_dry_exits_0(self):
        r = cli(self.e, ["triggers", "--dry"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("unreadable", r.stdout + r.stderr)

    def test_field_prints_empty_and_exits_0(self):
        r = cli(self.e, ["field", "morning", "model"])
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_api_due_for_session_is_empty(self):
        out, _ = api(self.e, ["due", "--for", "session"])
        self.assertEqual(out["due"], [])

    def test_api_detail_is_not_found(self):
        out, _ = api(self.e, ["detail", "morning"], expect=1)
        self.assertEqual(out["error"]["code"], "not_found")

    def test_tick_logs_no_error(self):
        env = self.e.env(CHRONOS_NOW=NOW)
        r = subprocess.run(["bash", TICK], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = os.path.join(self.e.cfg["logs_dir"], "chronos.log")
        text = open(log).read() if os.path.exists(log) else ""
        self.assertNotIn("TICK-ERROR", text)
        self.assertNotIn("unreadable", text)


class CorruptJobsFile(unittest.TestCase):
    def setUp(self):
        self.e = Env([])
        open(self.e.cfg["jobs_file"], "w").write("{not json")

    def tearDown(self):
        self.e.cleanup()

    def test_due_still_fails(self):
        r = cli(self.e, ["due"])
        self.assertEqual(r.returncode, 3)
        self.assertIn("unreadable", r.stderr)

    def test_list_fails_with_a_message_not_a_traceback(self):
        r = cli(self.e, ["list"])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unreadable", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_api_due_for_session_is_registry_unreadable(self):
        out, _ = api(self.e, ["due", "--for", "session"], expect=1)
        self.assertEqual(out["error"]["code"], "registry_unreadable")


if __name__ == "__main__":
    unittest.main(verbosity=2, warnings="ignore")
