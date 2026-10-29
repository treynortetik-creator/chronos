#!/usr/bin/env python3
"""0.3.1: `api detail` returns the job's recent runs, `api health` works on an install with no jobs.json, and no compiled
Python is tracked by git. Real CLI, throwaway home, fixed clock."""
import os
import subprocess
import sys
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_helpers import *  # noqa: E402,F401,F403

NOW = "2026-10-05T07:45"
DS = "2026-10-05"
RID1 = "r_" + "0" * 25 + "1"
RID2 = "r_" + "0" * 25 + "2"


def claim_req(rid, ds=DS):
    return {"run_id": rid, "ds": ds, "fire_at": ds + "T07:30:00-04:00", "manual": False, "again": False, "runner": "session", "bridge_instance": "inst-a"}


class DetailRuns(unittest.TestCase):
    def setUp(self):
        self.e = Env([job("morning", time="07:30", in_session=True, headless=False), job("other", time="08:00", in_session=True, headless=False)])

    def tearDown(self):
        self.e.cleanup()

    def test_detail_lists_the_finished_run_with_its_times(self):
        api(self.e, ["claim", "morning"], claim_req(RID1), now=NOW)
        api(self.e, ["finish", "morning"], {"run_id": RID1, "status": "ok", "report": "Done.\n"}, now="2026-10-05T07:50")
        out, _ = api(self.e, ["detail", "morning"], now="2026-10-05T08:00")
        self.assertEqual(len(out["runs"]), 1, out["runs"])
        r = out["runs"][0]
        self.assertEqual((r["run_id"], r["ds"], r["status"], r["runner"]), (RID1, DS, "ok", "session"))
        self.assertRegex(r["started_at"], RFC3339)
        self.assertRegex(r["ended_at"], RFC3339)
        self.assertEqual(r["duration_s"], 300)
        self.assertTrue(r["has_report"])

    def test_detail_runs_are_newest_first_failed_and_stalled_marked_and_only_this_job(self):
        api(self.e, ["claim", "morning"], claim_req(RID1), now=NOW)
        api(self.e, ["finish", "morning"], {"run_id": RID1, "status": "failed", "report": "No.\n"}, now="2026-10-05T07:50")
        ds2 = "2026-10-06"
        api(self.e, ["claim", "morning"], claim_req(RID2, ds2), now="2026-10-06T07:45")
        api(self.e, ["finish", "morning"], {"run_id": RID2, "status": "ok", "report": "Yes.\n"}, now="2026-10-06T07:46")
        open(os.path.join(self.e.cfg["state_dir"], "stalled-morning-2026-10-04"), "w").close()      # a marker with no run row
        rid3 = "r_" + "0" * 25 + "3"
        api(self.e, ["claim", "other"], claim_req(rid3), now=NOW)
        api(self.e, ["finish", "other"], {"run_id": rid3, "status": "ok", "report": "x\n"}, now="2026-10-05T07:46")
        out, _ = api(self.e, ["detail", "morning"], now="2026-10-06T09:00")
        self.assertEqual([(r["ds"], r["status"]) for r in out["runs"]],
                         [("2026-10-06", "ok"), ("2026-10-05", "failed"), ("2026-10-04", "launch_stalled")])

    def test_detail_with_no_runs_is_an_empty_list(self):
        out, _ = api(self.e, ["detail", "morning"])
        self.assertEqual(out["runs"], [])

    def test_detail_caps_runs_at_twenty(self):
        self.e.set_jobs([job("morning", time="07:30", days="daily", in_session=True, headless=False)])
        for i in range(25):
            ds = "2026-09-%02d" % (i + 1)
            rid = "r_" + "0" * 23 + "%03d" % (i + 10)
            api(self.e, ["claim", "morning"], claim_req(rid, ds), now=ds + "T07:45")
            api(self.e, ["finish", "morning"], {"run_id": rid, "status": "ok", "report": "ok\n"}, now=ds + "T07:46")
        out, _ = api(self.e, ["detail", "morning"], now="2026-10-05T09:00")
        self.assertEqual(len(out["runs"]), 20)
        self.assertEqual(out["runs"][0]["ds"], "2026-09-25")


class HealthEmpty(unittest.TestCase):
    def test_health_on_an_install_with_no_jobs_file(self):
        e = Env([])
        try:
            os.remove(e.cfg["jobs_file"])
            out, _ = api(e, ["health"])
            self.assertNotIn("error", out)
            self.assertEqual(out["headless"], "unused")
            self.assertEqual(out["issues"], [])
            self.assertFalse(out["paused_all"])
        finally:
            e.cleanup()

    def test_health_with_an_empty_jobs_list_matches(self):
        e = Env([])
        try:
            out, _ = api(e, ["health"])
            self.assertEqual((out["headless"], out["issues"]), ("unused", []))
        finally:
            e.cleanup()

    def test_a_corrupt_jobs_file_is_still_an_error(self):
        e = Env([])
        try:
            open(e.cfg["jobs_file"], "w").write("{not json")
            out, _ = api(e, ["health"], expect=1)
            self.assertEqual(out["error"]["code"], "registry_unreadable")
        finally:
            e.cleanup()


class NoCompiledPython(unittest.TestCase):
    def test_git_tracks_no_pyc_or_pycache_and_ignores_them(self):
        r = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
        bad = [p for p in r.stdout.split("\n") if p.endswith(".pyc") or "__pycache__" in p]
        self.assertEqual(bad, [])
        gi = open(os.path.join(ROOT, ".gitignore")).read().split("\n")
        self.assertIn("__pycache__/", gi)
        self.assertIn("*.pyc", gi)


class Version(unittest.TestCase):
    def test_version_is_0_3_1(self):
        e = Env([])
        try:
            out, _ = api(e, ["version"])
            self.assertEqual(out["chronos"], "0.3.1")
        finally:
            e.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2, warnings="ignore")
