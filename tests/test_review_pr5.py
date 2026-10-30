#!/usr/bin/env python3
"""Independent review of PR #5 (no jobs.json is an empty registry everywhere). Each test pins one claim or one suspected
problem. Real CLI, throwaway home, fixed clock. A failing test here is a finding, not a flake.
Run: python3 tests/test_review_pr5.py   (not in tests/run.sh: some tests fail on purpose until fixed)."""
import json
import os
import shutil
import subprocess
import sys
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_helpers import *  # noqa: E402,F401,F403

NOW = "2026-10-05T09:00"
DS = "2026-10-05"


def cli(e, args, **extra):
    env = e.env(CHRONOS_NOW=NOW, **extra)
    env["TZ"] = NY
    return subprocess.run([sys.executable, CLI] + list(args), env=env, input="", capture_output=True, text=True, timeout=60)


def rid(n):
    return "r_" + "0" * 23 + "%03d" % n


def claim_req(r, ds=DS):
    return {"run_id": r, "ds": ds, "fire_at": ds + "T07:30:00-04:00", "manual": False, "again": False, "runner": "session",
            "bridge_instance": "inst-a"}


def run_once(e, jid, r, ds, status="ok", start="07:45", end="07:46"):
    api(e, ["claim", jid], claim_req(r, ds), now=ds + "T" + start)
    api(e, ["finish", jid], {"run_id": r, "status": status, "report": "x\n"}, now=ds + "T" + end)


def append_row(e, row):
    with open(e.cfg["runs_file"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


# ----------------------------------------------------------------------------------------------- (1) detail recent runs
class DetailRuns(unittest.TestCase):
    def setUp(self):
        self.e = Env([job("morning", time="07:30", days="daily", in_session=True, headless=False),
                      job("morning-news", time="07:30", days="daily", in_session=True, headless=False)])

    def tearDown(self):
        self.e.cleanup()

    def test_a_job_id_that_is_a_prefix_of_another_sees_only_its_own_runs(self):
        run_once(self.e, "morning", rid(1), "2026-10-04")
        run_once(self.e, "morning-news", rid(2), "2026-10-05")
        open(os.path.join(self.e.cfg["state_dir"], "failed-morning-news-2026-10-03"), "w").close()
        out, _ = api(self.e, ["detail", "morning"])
        self.assertEqual([r["ds"] for r in out["runs"]], ["2026-10-04"])

    def test_runs_across_a_month_and_year_boundary_sort_newest_first(self):
        for i, ds in enumerate(["2026-12-31", "2027-01-01", "2026-11-30", "2026-12-01"]):
            run_once(self.e, "morning", rid(10 + i), ds)
        out, _ = api(self.e, ["detail", "morning"], now="2027-01-01T09:00")
        self.assertEqual([r["ds"] for r in out["runs"]], ["2027-01-01", "2026-12-31", "2026-12-01", "2026-11-30"])

    def test_two_runs_on_one_day_both_listed_later_first(self):
        run_once(self.e, "morning", rid(1), DS)
        api(self.e, ["claim", "morning"], dict(claim_req(rid(2)), again=True, manual=True), now=DS + "T08:10")
        api(self.e, ["finish", "morning"], {"run_id": rid(2), "status": "failed", "report": "x\n"}, now=DS + "T08:11")
        out, _ = api(self.e, ["detail", "morning"])
        self.assertEqual([r["run_id"] for r in out["runs"]], [rid(2), rid(1)])

    def test_timezone_does_not_change_ds_or_order(self):
        run_once(self.e, "morning", rid(1), "2026-10-04")
        run_once(self.e, "morning", rid(2), DS)
        a, _ = api(self.e, ["detail", "morning"], tz=NY)
        b, _ = api(self.e, ["detail", "morning"], tz="Asia/Tokyo")
        self.assertEqual([r["ds"] for r in a["runs"]], [r["ds"] for r in b["runs"]])

    def test_a_truncated_runs_line_is_skipped(self):
        run_once(self.e, "morning", rid(1), DS)
        with open(self.e.cfg["runs_file"], "a") as fh:
            fh.write('{"job": "morning", "run_id": "r_tr')
        out, _ = api(self.e, ["detail", "morning"])
        self.assertEqual([r["run_id"] for r in out["runs"]], [rid(1)])

    def test_a_run_row_with_a_non_numeric_start_ts_does_not_break_detail(self):
        """One bad row in runs.jsonl (hand edit, older writer) must not take down `api detail` for the job."""
        run_once(self.e, "morning", rid(1), DS)
        append_row(self.e, {"job": "morning", "run_id": rid(2), "ds": "2026-10-04", "start_ts": "yesterday", "status": "ok"})
        env = self.e.env(CHRONOS_NOW=NOW)
        p = subprocess.run([sys.executable, CLI, "api", "detail", "morning"], env=env, input="", capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(rid(1), [x["run_id"] for x in json.loads(p.stdout)["runs"]])

    def test_a_run_row_with_a_non_numeric_duration_does_not_break_detail(self):
        run_once(self.e, "morning", rid(1), DS)
        append_row(self.e, {"job": "morning", "run_id": rid(2), "ds": "2026-10-04", "start_ts": 1.0, "duration_s": "n/a",
                            "status": "ok"})
        env = self.e.env(CHRONOS_NOW=NOW)
        p = subprocess.run([sys.executable, CLI, "api", "detail", "morning"], env=env, input="", capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_no_runs_file_and_no_state_dir_is_an_empty_list(self):
        shutil.rmtree(self.e.cfg["state_dir"], ignore_errors=True)
        if os.path.exists(self.e.cfg["runs_file"]):
            os.remove(self.e.cfg["runs_file"])
        out, _ = api(self.e, ["detail", "morning"])
        self.assertEqual(out["runs"], [])


# ----------------------------------------------------------------------------------------------- (2) health edge cases
class Health(unittest.TestCase):
    def setUp(self):
        self.e = Env([])

    def tearDown(self):
        self.e.cleanup()

    def _health(self):
        env = self.e.env(CHRONOS_NOW=NOW)
        return subprocess.run([sys.executable, CLI, "api", "health"], env=env, input="", capture_output=True, text=True)

    def test_no_jobs_file(self):
        os.remove(self.e.cfg["jobs_file"])
        p = self._health()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["issues"], [])

    def test_empty_list(self):
        p = self._health()
        self.assertEqual(p.returncode, 0, p.stderr)

    def _is_registry_unreadable(self, p):
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(json.loads(p.stdout)["error"]["code"], "registry_unreadable")

    def test_zero_byte_file(self):
        open(self.e.cfg["jobs_file"], "w").close()
        self._is_registry_unreadable(self._health())

    def test_malformed(self):
        open(self.e.cfg["jobs_file"], "w").write("[{")
        self._is_registry_unreadable(self._health())

    def test_not_a_list(self):
        open(self.e.cfg["jobs_file"], "w").write("{}")
        self._is_registry_unreadable(self._health())

    def test_not_utf8(self):
        open(self.e.cfg["jobs_file"], "wb").write(b"[\xff\xfe]")
        self._is_registry_unreadable(self._health())

    def test_unreadable_path_is_a_directory(self):
        """The container runs as root, so chmod 000 cannot make it unreadable; a directory in its place does."""
        os.remove(self.e.cfg["jobs_file"])
        os.mkdir(self.e.cfg["jobs_file"])
        self._is_registry_unreadable(self._health())


# ----------------------------------------------------------------------- (3) behaviour of other commands after the root fix
class Compat(unittest.TestCase):
    def setUp(self):
        self.e = Env([job("morning", time="07:30")])

    def tearDown(self):
        self.e.cleanup()

    def test_list_output_format_is_unchanged_with_jobs(self):
        p = cli(self.e, ["list"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertRegex(p.stdout, r"^morning\s+.+\s+(on|disabled)\s+next: \S+\n$")

    def test_field_output_is_unchanged_with_jobs(self):
        p = cli(self.e, ["field", "morning", "id"])
        self.assertEqual((p.returncode, p.stdout), (0, "morning\n"))

    def test_list_with_a_non_object_entry_has_no_traceback(self):
        """PR claims list reports a bad registry with a message instead of a traceback; a non-object entry still crashes."""
        open(self.e.cfg["jobs_file"], "w").write("[1]")
        p = cli(self.e, ["list"])
        self.assertNotIn("Traceback", p.stderr)

    def test_a_jobs_file_in_a_missing_directory_is_still_reported(self):
        """A jobs_file whose parent directory does not exist is a misconfiguration (typo, unmounted volume, sync folder not
        there), not a new install: the installer always creates the config directory. Before this PR `chronos due` exited
        3 and the tick logged TICK-ERROR; now it is silent and every job quietly stops running."""
        c = json.load(open(self.e.cfg_path))
        c["jobs_file"] = os.path.join(self.e.tmp, "nowhere", "jobs.json")
        json.dump(c, open(self.e.cfg_path, "w"))
        p = cli(self.e, ["due"])
        self.assertNotEqual(p.returncode, 0, "due exited 0 with jobs_file in a missing directory: " + p.stdout + p.stderr)

    def test_tick_message_does_not_mention_a_case_that_can_no_longer_happen(self):
        src = open(os.path.join(ROOT, "bin", "chronos-tick.sh")).read()
        self.assertFalse("jobs file missing or unparsable" in src, "bin/chronos-tick.sh:38 still says 'missing or unparsable'")


# ----------------------------------------------------------------------------------------------- (4)/(5) hygiene, version
class Hygiene(unittest.TestCase):
    def test_pr_diff_and_messages_have_no_home_paths_or_emails(self):
        diff = subprocess.run(["git", "log", "-p", "origin/main..HEAD"], cwd=ROOT, capture_output=True, text=True).stdout
        import re
        self.assertIsNone(re.search(r"/Users/[A-Za-z]|/home/[a-z]+/", diff))
        emails = set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", diff)) - {"noreply@anthropic.com"}
        self.assertEqual(emails, set())

    def test_changelog_unreleased_and_version_still_0_3_1(self):
        e = Env([])
        try:
            out, _ = api(e, ["version"])
            self.assertEqual(out["chronos"], "0.3.1")
        finally:
            e.cleanup()
        ch = open(os.path.join(ROOT, "CHANGELOG.md")).read()
        self.assertLess(ch.index("tests/test_no_jobs_file.py"), ch.index("## 0.3.1"))


if __name__ == "__main__":
    unittest.main(verbosity=2, warnings="ignore")
