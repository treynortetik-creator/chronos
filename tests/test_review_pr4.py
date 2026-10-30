#!/usr/bin/env python3
"""Adversarial review of PR #4 (one-shot date_key in the snapshot + `chronos api report`).

Tests named test_DEFECT_* fail on the PR head and pass once the defect is fixed. Tests named test_guard_* pass today and pin
behaviour the fix must keep. Real CLI, throwaway home, fixed clock, same harness as tests/test_oneshot_report.py.
"""
import json
import os
import signal
import subprocess
import sys
import time
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_helpers import *  # noqa: E402,F401,F403

DS = "2026-10-05"
RID1 = "r_" + "0" * 25 + "1"
RID2 = "r_" + "0" * 25 + "2"
LIMIT = 256 * 1024


def claim_req(rid, ds=DS, fire="07:30"):
    return {"run_id": rid, "ds": ds, "fire_at": ds + "T" + fire + ":00-04:00", "manual": False, "again": False,
            "runner": "session", "bridge_instance": "inst-a"}


def raw(e, args, now="2026-10-08T09:00", stdin=""):
    env = e.env(CHRONOS_NOW=now)
    env["TZ"] = NY
    r = subprocess.run([sys.executable, CLI, "api"] + list(args), env=env, input=stdin, capture_output=True, text=True, timeout=60)
    return json.loads(r.stdout), r


def entries(root):
    """{relative path: (kind, mtime_ns, ctime_ns)} for every file AND directory under root (lstat: symlinks are not followed)."""
    out = {}
    for d, dirs, files in os.walk(root):
        for n in dirs + files:
            p = os.path.join(d, n)
            st = os.lstat(p)
            out[os.path.relpath(p, root)] = ("d" if n in dirs else "f", st.st_mtime_ns, st.st_ctime_ns)
    return out


class _ReportBase(unittest.TestCase):
    def setUp(self):
        self.e = Env([job("morning", time="07:30", in_session=True, headless=False),
                      job("quiet", time="08:00", in_session=True, headless=False)])
        self.reports = self.e.cfg["reports_dir"]
        self.outside = os.path.join(self.e.tmp, "outside")
        os.makedirs(self.outside)
        self.secret = os.path.join(self.outside, "secret.md")
        with open(self.secret, "w") as fh:
            fh.write("SECRET-NOT-A-REPORT\n")

    def tearDown(self):
        self.e.cleanup()

    def run_one(self, rid, report, ds=DS, status="ok", jid="morning"):
        api(self.e, ["claim", jid], claim_req(rid, ds), now=ds + "T07:45")
        api(self.e, ["finish", jid], {"run_id": rid, "status": status, "report": report}, now=ds + "T07:50")

    def run_file(self, rid=RID1, ds=DS, jid="morning"):
        return os.path.join(self.reports, "%s-%s-%s.md" % (jid, ds, rid))

    def day_file(self, ds=DS, jid="morning"):
        return os.path.join(self.reports, "%s-%s.md" % (jid, ds))


# ----------------------------------------------------------------------------------------------------------------- date_key
class DateKeyEdges(unittest.TestCase):
    def tearDown(self):
        self.e.cleanup()

    def test_DEFECT_HIGH_recurring_date_key_is_the_claim_key_after_midnight(self):
        """A 23:50 daily job with a 180 min window is still open at 00:05. The tick and `claim` key it on YESTERDAY (ds), so the
        README promise 'date_key equals ds for a recurring job' must hold. On the PR head date_key is C.date_key(job, at) = TODAY,
        so a client that looks up claim-/ran-/report by date_key misses the live claim and the run's report."""
        self.e = Env([job("late", time="23:50", days="daily", in_session=True, headless=False)], session_runner="talos")
        now = "2026-10-06T00:05"
        out, _ = api(self.e, ["claim", "late"], {"run_id": RID1, "ds": DS, "fire_at": DS + "T23:50:00-04:00", "manual": False,
                                                  "again": False, "runner": "session", "bridge_instance": "i"}, now=now)
        self.assertTrue(out["ok"], out)
        s = sched(self.e, "late", now="2026-10-06T00:06")
        self.assertEqual((s["ds"], s["today"]), (DS, "running"))
        self.assertTrue(os.path.isdir(os.path.join(self.e.cfg["state_dir"], "claim-late-" + DS)))
        self.assertEqual(s["date_key"], s["ds"], "recurring job: date_key must equal ds (the key its claim is stored under)")
        self.assertTrue(os.path.isdir(os.path.join(self.e.cfg["state_dir"], "claim-late-" + s["date_key"])))

    def test_DEFECT_HIGH_recurring_date_key_while_waiting_after_midnight(self):
        self.e = Env([job("late", time="23:50", days="daily", in_session=True, headless=False)], session_runner="talos")
        s = sched(self.e, "late", now="2026-10-06T00:05")
        self.assertEqual((s["ds"], s["today"]), (DS, "waiting"))
        self.assertEqual(s["date_key"], DS)

    def test_guard_one_shot_across_midnight_keeps_its_own_date(self):
        self.e = Env([job("remind", once=DS + "T23:50", time="23:50", days="daily", in_session=True, headless=False)], session_runner="talos")
        now = "2026-10-06T00:05"
        out, _ = api(self.e, ["claim", "remind"], claim_req(RID1, DS, "23:50"), now=now)
        self.assertTrue(out["ok"], out)
        s = sched(self.e, "remind", now=now)
        self.assertEqual(s["date_key"], DS)
        self.assertTrue(os.path.isdir(os.path.join(self.e.cfg["state_dir"], "claim-remind-" + s["date_key"])))

    def test_guard_one_shot_on_a_dst_gap_keeps_its_date(self):
        # 02:30 does not exist in New York on 2026-03-08 (spring forward); the key is still the date it was set for
        self.e = Env([job("dst", once="2026-03-08T02:30", time="02:30", days="daily", in_session=True, headless=False)], session_runner="talos")
        for now in ("2026-03-08T01:00", "2026-03-08T04:00", "2026-03-09T09:00"):
            self.assertEqual(sched(self.e, "dst", now=now)["date_key"], "2026-03-08", now)

    def test_guard_rearmed_one_shot_moves_its_key_and_keeps_the_old_report(self):
        self.e = Env([job("remind", once="2026-10-06T15:00", time="15:00", days="daily", in_session=True, headless=False)], session_runner="talos")
        api(self.e, ["claim", "remind"], claim_req(RID1, "2026-10-06", "15:00"), now="2026-10-06T15:05")
        api(self.e, ["finish", "remind"], {"run_id": RID1, "status": "ok", "report": "First.\n"}, now="2026-10-06T15:06")
        self.e.set_jobs([job("remind", once="2026-10-08T15:00", time="15:00", days="daily", in_session=True, headless=False)])
        self.assertEqual(sched(self.e, "remind", now="2026-10-07T09:00")["date_key"], "2026-10-08")
        out, _ = api(self.e, ["report", "remind", "--date", "2026-10-06"], now="2026-10-07T09:00")
        self.assertEqual(out["report"], "First.\n")


# ----------------------------------------------------------------------------------------------------------------- report: files
class ReportFiles(_ReportBase):
    def test_DEFECT_MED_symlinked_run_report_pointing_outside_reports_dir_is_refused(self):
        """An unrestricted headless job writes its own report into reports/ (bin/chronos-run.sh), so a prompt-injected job can
        plant a symlink there. ui/server.py view_file() already refuses anything whose realpath leaves REPORTS_DIR; the new
        verb follows the link and hands the target to the client."""
        self.run_one(RID1, "real\n")
        os.remove(self.run_file())
        os.symlink(self.secret, self.run_file())
        out, _ = raw(self.e, ["report", "morning", "--run", RID1])
        self.assertNotIn("SECRET-NOT-A-REPORT", json.dumps(out))
        self.assertEqual(out.get("error", {}).get("code"), "not_found", out)

    def test_DEFECT_MED_symlinked_day_report_is_refused(self):
        self.run_one(RID1, "real\n")
        os.remove(self.run_file())
        os.remove(self.day_file())
        os.symlink(self.secret, self.day_file())
        out, _ = raw(self.e, ["report", "morning", "--date", DS])
        self.assertNotIn("SECRET-NOT-A-REPORT", json.dumps(out))

    def test_DEFECT_MED_a_huge_report_is_read_bounded_not_slurped(self):
        """_read_bytes() reads the whole file and only then trims to 256 KiB. Headless jobs write reports of any size. A 1 GiB
        (sparse) report must not cost the CLI ~1 GiB of RSS; reading REPORT_MAX + 4 bytes is enough."""
        self.run_one(RID1, "real\n")
        with open(self.run_file(), "wb") as fh:
            fh.truncate(1 << 30)
        env = self.e.env(CHRONOS_NOW="2026-10-08T09:00")
        env["TZ"] = NY
        probe = ("import resource, subprocess, sys\n"
                 "subprocess.run(sys.argv[1:], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                 "print(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)\n")
        r = subprocess.run([sys.executable, "-c", probe, sys.executable, CLI, "api", "report", "morning", "--run", RID1],
                           env=env, capture_output=True, text=True, timeout=120)
        rss = int(r.stdout.strip()) * (1 if sys.platform == "darwin" else 1024)
        self.assertLess(rss, 200 * 1024 * 1024, "report verb peaked at %d MiB of RSS for a 1 GiB report" % (rss >> 20))

    def test_DEFECT_LOW_run_id_read_from_runs_jsonl_is_validated_before_a_path_is_built(self):
        """The PR says 'paths are never built from unvalidated input'. --run is validated, but with --date (or no selector) the
        run id comes from runs.jsonl and goes straight into run_report_path(). A row whose run_id carries '/..' walks out of
        reports/ once the matching directory exists."""
        self.run_one(RID1, "real\n")
        evil = "x/../../../../outside/secret"
        os.makedirs(os.path.join(self.reports, "morning-%s-x" % DS))
        with open(self.e.cfg["runs_file"], "a") as fh:
            fh.write(json.dumps({"job": "morning", "kind": "schedule", "run_id": evil, "ds": DS, "start_ts": 4102444800,
                                 "duration_s": 1, "ok": True, "status": "ok", "runner": "session"}) + "\n")
        out, _ = raw(self.e, ["report", "morning", "--date", DS])
        self.assertNotIn("SECRET-NOT-A-REPORT", json.dumps(out))

    def test_guard_truncation_lands_on_a_utf8_boundary(self):
        self.run_one(RID1, "real\n")
        body = b"a" * (LIMIT - 2) + "\U0001F600".encode("utf-8") + b"tail"          # the 4-byte emoji straddles the cut
        with open(self.run_file(), "wb") as fh:
            fh.write(body)
        out, _ = raw(self.e, ["report", "morning", "--run", RID1])
        self.assertIs(out["truncated"], True)
        self.assertEqual(out["report"], "a" * (LIMIT - 2))
        self.assertLessEqual(len(out["report"].encode("utf-8")), LIMIT)

    def test_DEFECT_LOW_truncated_and_untruncated_reports_decode_bad_bytes_the_same_way(self):
        """Under the cap a stray invalid byte shows as U+FFFD ('replace'); over the cap _trim_bytes uses 'ignore', which drops
        every invalid byte in the whole 256 KiB, not just the partial character at the cut."""
        self.run_one(RID1, "real\n")
        with open(self.run_file(), "wb") as fh:
            fh.write(b"head \xff mid " + b"a" * 100)
        small, _ = raw(self.e, ["report", "morning", "--run", RID1])
        self.assertIn("�", small["report"])
        with open(self.run_file(), "wb") as fh:
            fh.write(b"head \xff mid " + b"a" * (300 * 1024))
        big, _ = raw(self.e, ["report", "morning", "--run", RID1])
        self.assertIs(big["truncated"], True)
        self.assertTrue("�" in big["report"], "the invalid byte vanished: %r" % big["report"][:16])

    def test_guard_control_characters_round_trip_escaped_and_nothing_else_leaks(self):
        self.run_one(RID1, "ok\x1b[31mred\x07\x00end\n")
        out, r = raw(self.e, ["report", "morning", "--run", RID1])
        self.assertEqual(out["report"], "ok\x1b[31mred\x07\x00end\n")
        self.assertNotIn("\x1b", r.stdout)                                          # JSON-escaped on the wire
        self.assertNotIn(self.e.tmp, r.stdout)                                      # no paths in the payload
        self.assertEqual(set(out) - {"late_note"}, {"job_id", "date_key", "run_id", "status", "runner", "started_at", "ended_at",
                                                    "report", "truncated"})

    def test_DEFECT_LOW_late_note_is_not_silently_cut_to_280_chars(self):
        """`report` reuses schedule_run(), which cuts late_note to 280 characters for the list view. The read verb is where the
        whole stored text is supposed to come back; at minimum a cut must be flagged like `truncated` is."""
        self.run_one(RID1, "on time\n")
        note = "L" * 1000
        api(self.e, ["finish", "morning"], {"run_id": RID1, "status": "ok", "report": note}, now=DS + "T08:30")
        out, _ = raw(self.e, ["report", "morning", "--run", RID1])
        self.assertTrue(out.get("late_note") == note or out.get("late_note_truncated") is True, len(out.get("late_note", "")))

    def test_guard_read_only_including_directories_and_a_live_claim(self):
        self.run_one(RID1, "Stay put.\n")
        api(self.e, ["claim", "quiet"], claim_req(RID2, "2026-10-08", "08:00"), now="2026-10-08T08:05")   # leave a live claim
        os.symlink(self.secret, self.run_file(RID2, "2026-10-08", "quiet"))
        before = entries(self.e.tmp)
        time.sleep(0.02)
        for args in (["morning"], ["morning", "--run", RID1], ["morning", "--date", DS], ["quiet"], ["quiet", "--date", "2026-10-08"],
                     ["nope"], ["morning", "--date", "2026-01-01"]):
            raw(self.e, ["report"] + args)
        self.assertEqual(entries(self.e.tmp), before)

    def test_guard_read_only_on_a_fresh_home_creates_no_directories(self):
        e = Env([job("fresh", time="08:00")])
        try:
            self.assertFalse(os.path.exists(e.cfg["reports_dir"]))
            before = entries(e.tmp)
            for args in (["fresh"], ["fresh", "--date", DS], ["fresh", "--run", RID1]):
                raw(e, ["report"] + args)
            self.assertEqual(entries(e.tmp), before)
        finally:
            e.cleanup()


# ----------------------------------------------------------------------------------------------------------------- report: input
class ReportInput(_ReportBase):
    def test_DEFECT_LOW_malformed_dates_and_run_ids_are_bad_request_not_not_found(self):
        """README: bad_request for a malformed date or run id. DS_RE / RUN_ID_RE end in `$` (matches before a trailing newline;
        ID_RE uses \\Z for exactly this reason) and `\\d` accepts any Unicode digit; neither checks the date is real."""
        self.run_one(RID1, "x\n")
        for args in (["morning", "--date", DS + "\n"], ["morning", "--date", "2026-13-45"], ["morning", "--date", "2026-02-30"],
                     ["morning", "--date", "٢٠٢٦-10-05"], ["morning", "--run", RID1 + "\n"]):
            out, _ = raw(self.e, ["report"] + args)
            self.assertEqual(out["error"]["code"], "bad_request", repr(args))

    def test_DEFECT_LOW_a_repeated_flag_is_bad_request_not_last_wins(self):
        self.run_one(RID1, "one\n")
        self.run_one(RID2, "two\n", ds="2026-10-06")
        out, _ = raw(self.e, ["report", "morning", "--date", DS, "--date", "2026-10-06"])
        self.assertEqual(out.get("error", {}).get("code"), "bad_request", out)

    def test_DEFECT_LOW_takes_the_id_on_stdin_like_detail_approved_and_disable(self):
        self.run_one(RID1, "x\n")
        d, _ = raw(self.e, ["detail"], stdin='{"id": "morning"}')
        self.assertIn("schedule", d)
        out, _ = raw(self.e, ["report"], stdin='{"id": "morning"}')
        self.assertEqual(out.get("report"), "x\n", out)

    def test_guard_error_envelope_shape(self):
        for args in (["nope"], ["BAD"], ["morning", "--run", "nope"]):
            out, r = raw(self.e, ["report"] + args)
            self.assertEqual(r.returncode, 1)
            self.assertEqual(set(out), {"error"})
            self.assertEqual(set(out["error"]), {"code", "message", "fields"})
            self.assertEqual(out["error"]["fields"], [])


# ----------------------------------------------------------------------------------------------------------------- flaky deadline test
class WatchdogScope(unittest.TestCase):
    """tests/test_api_deadlines.py::test_a_normal_tick_logs_no_deadline_and_leaves_no_watchdog_behind runs a machine-wide
    `pgrep -f "sleep 2"`. That substring also matches `sleep 20`, `sleep 25`, `sleep 200` and any shell whose command line
    contains `sleep 2;`, owned by anything on the box. It fails on main as well as on this PR whenever such a process exists."""

    def setUp(self):
        self.e = Env([job("alpha", time="08:30")], step_deadline_s=2, trigger_deadline_s=2)
        self.decoy = subprocess.Popen(["sleep", "25"])                 # an unrelated process the test does not own

    def tearDown(self):
        os.kill(self.decoy.pid, signal.SIGTERM)                         # by PID only
        self.decoy.wait()
        self.e.cleanup()

    def test_proposed_fix_scopes_the_check_to_the_ticks_process_group(self):
        """Proposed replacement: run the tick as its own session (start_new_session=True, so its pgid is its pid; ch_step never
        calls setsid, so a leaked watchdog keeps that pgid even after re-parenting to launchd) and look only inside that group,
        for an exact `sleep <deadline>` command line."""
        tick = subprocess.Popen(["/bin/bash", TICK], env=self.e.env(CHRONOS_NOW="2026-10-05T09:00"), start_new_session=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        _, err = tick.communicate(timeout=60)
        self.assertEqual(tick.returncode, 0, err)
        pgid = tick.pid
        self.assertTrue(self.e.wait_for(lambda: os.path.exists(os.path.join(self.e.cfg["state_dir"], "ran-alpha-2026-10-05"))))
        time.sleep(0.5)
        r = subprocess.run(["pgrep", "-g", str(pgid), "-f", "^sleep 2$"], capture_output=True, text=True)
        self.assertEqual(r.stdout.split(), [], "a deadline watchdog outlived the step it guarded")


if __name__ == "__main__":
    unittest.main(verbosity=2, warnings="ignore")
