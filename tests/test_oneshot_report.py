#!/usr/bin/env python3
"""One-shot `date_key` in the snapshot, and the read-only `chronos api report` verb. Real CLI, throwaway home, fixed clock."""
import os
import sys
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_helpers import *  # noqa: E402,F401,F403

NOW = "2026-10-05T07:45"           # a Monday
DS = "2026-10-05"
RID1 = "r_" + "0" * 25 + "1"
RID2 = "r_" + "0" * 25 + "2"
RID3 = "r_" + "0" * 25 + "3"


def claim_req(rid, ds=DS, fire="07:30"):
    return {"run_id": rid, "ds": ds, "fire_at": ds + "T" + fire + ":00-04:00", "manual": False, "again": False, "runner": "session", "bridge_instance": "inst-a"}


def tree(root):
    """{relative path: (size, mtime_ns, bytes)} of everything under root, to prove a verb wrote nothing."""
    out = {}
    for d, _, files in os.walk(root):
        for n in files:
            p = os.path.join(d, n)
            st = os.stat(p)
            out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns, open(p, "rb").read())
    return out


class DateKey(unittest.TestCase):
    def setUp(self):
        self.e = Env([job("daily-one", time="07:30", days="daily"),
                      job("remind", once="2026-10-06T15:00", time="15:00", days="daily", in_session=True, headless=False),
                      job("ev", clock=False, triggers=[{"type": "webhook"}])], session_runner="talos")

    def tearDown(self):
        self.e.cleanup()

    def test_a_recurring_job_date_key_equals_ds(self):
        s = sched(self.e, "daily-one", now=NOW)
        self.assertEqual((s["date_key"], s["ds"]), (DS, DS))

    def test_an_event_only_job_has_a_date_key_too(self):
        self.assertEqual(sched(self.e, "ev", now=NOW)["date_key"], DS)

    def test_a_one_shot_date_key_is_the_date_it_was_set_for_not_today(self):
        s = sched(self.e, "remind", now=NOW)
        self.assertEqual(s["date_key"], "2026-10-06")
        self.assertEqual(s["ds"], DS)                                  # unchanged: the field is added, never renamed

    def test_a_one_shot_date_key_survives_the_day_passing(self):
        self.assertEqual(sched(self.e, "remind", now="2026-10-09T10:00")["date_key"], "2026-10-06")

    def test_a_one_shot_date_key_is_the_key_its_claim_is_stored_under(self):
        now = "2026-10-06T15:05"
        out, _ = api(self.e, ["claim", "remind"], claim_req(RID1, "2026-10-06", "15:00"), now=now)
        self.assertTrue(out["ok"], out)
        s = sched(self.e, "remind", now=now)
        self.assertEqual(s["date_key"], out["ds"])
        self.assertTrue(os.path.isdir(os.path.join(self.e.cfg["state_dir"], "claim-remind-" + s["date_key"])))

    def test_detail_schedule_carries_it_and_the_old_keys_are_all_still_there(self):
        out, _ = api(self.e, ["detail", "remind"], now=NOW)
        self.assertEqual(out["schedule"]["date_key"], "2026-10-06")
        for k in ("id", "today", "ds", "window_ends", "last_run", "state_rev", "next_run", "etag", "content_hash"):
            self.assertIn(k, out["schedule"])


class Report(unittest.TestCase):
    def setUp(self):
        self.e = Env([job("morning", time="07:30", in_session=True, headless=False), job("quiet", time="08:00", in_session=True, headless=False)])

    def tearDown(self):
        self.e.cleanup()

    def run_one(self, rid, report, ds=DS, status="ok", jid="morning"):
        api(self.e, ["claim", jid], claim_req(rid, ds), now=ds + "T07:45")
        api(self.e, ["finish", jid], {"run_id": rid, "status": status, "report": report}, now=ds + "T07:50")

    def rep(self, *args, **kw):
        return api(self.e, ["report"] + list(args), now="2026-10-08T09:00", **kw)

    def test_by_run_id_returns_that_runs_report(self):
        self.run_one(RID1, "First run report.\n")
        out, _ = self.rep("morning", "--run", RID1)
        self.assertEqual((out["job_id"], out["date_key"], out["run_id"], out["status"]), ("morning", DS, RID1, "ok"))
        self.assertEqual(out["report"], "First run report.\n")
        self.assertIs(out["truncated"], False)

    def test_by_date_returns_the_latest_report_of_that_day(self):
        self.run_one(RID1, "Day one.\n")
        self.run_one(RID2, "Day two.\n", ds="2026-10-06")
        out, _ = self.rep("morning", "--date", DS)
        self.assertEqual((out["date_key"], out["run_id"], out["report"]), (DS, RID1, "Day one.\n"))

    def test_with_no_selector_it_is_the_newest_run(self):
        self.run_one(RID1, "Old.\n")
        self.run_one(RID2, "New.\n", ds="2026-10-06", status="failed")
        out, _ = self.rep("morning")
        self.assertEqual((out["date_key"], out["run_id"], out["status"], out["report"]), ("2026-10-06", RID2, "failed", "New.\n"))

    def test_date_and_run_that_disagree_is_not_found(self):
        self.run_one(RID1, "x\n")
        out, r = self.rep("morning", "--date", "2026-10-06", "--run", RID1, expect=1)
        self.assertEqual(out["error"]["code"], "not_found")

    def test_not_found_cases_use_the_error_envelope(self):
        self.run_one(RID1, "x\n")
        for args in (["no-such-job"], ["quiet"], ["morning", "--run", RID3], ["morning", "--date", "2026-01-01"]):
            out, r = self.rep(*args, expect=1)
            self.assertEqual(set(out), {"error"}, args)
            self.assertEqual(out["error"]["code"], "not_found", args)
            self.assertEqual(set(out["error"]), {"code", "message", "fields"})

    def test_a_run_without_a_stored_report_is_not_found(self):
        self.run_one(RID1, "")
        out, _ = self.rep("morning", "--run", RID1, expect=1)
        self.assertEqual(out["error"]["code"], "not_found")

    def test_bad_input_is_bad_request(self):
        for args in ([], ["Bad Id"], ["../etc"], ["morning", "--run", "nope"], ["morning", "--date", "10/05/2026"], ["morning", "--date"],
                     ["morning", "--run"], ["morning", "--frob", "1"], ["morning", "extra"]):
            out, _ = self.rep(*args, expect=1)
            self.assertEqual(out["error"]["code"], "bad_request", args)

    def test_a_path_traversal_attempt_reads_nothing(self):
        self.run_one(RID1, "x\n")
        out, _ = self.rep("morning", "--date", "../../../etc/passwd", expect=1)
        self.assertEqual(out["error"]["code"], "bad_request")

    def test_a_huge_report_is_cut_and_says_so(self):
        self.run_one(RID1, "a" * (200 * 1024))
        out, _ = self.rep("morning", "--run", RID1)
        self.assertIs(out["truncated"], False)
        d = self.e.cfg["reports_dir"]
        open(os.path.join(d, "morning-%s-%s.md" % (DS, RID1)), "w").write("b" * (300 * 1024))
        out, _ = self.rep("morning", "--run", RID1)
        self.assertIs(out["truncated"], True)
        self.assertEqual(len(out["report"]), 256 * 1024)

    def test_it_is_read_only(self):
        self.run_one(RID1, "Stay put.\n")
        before = tree(self.e.tmp)
        for args in (["morning"], ["morning", "--run", RID1], ["morning", "--date", DS], ["quiet"], ["nope"]):
            self.rep(*args, expect=0 if args[0] == "morning" else 1)
        self.assertEqual(tree(self.e.tmp), before)

    def test_a_one_shot_report_is_found_under_its_date_key(self):
        e = Env([job("remind", once="2026-10-06T15:00", time="15:00", days="daily", in_session=True, headless=False)], session_runner="talos")
        try:
            api(e, ["claim", "remind"], claim_req(RID1, "2026-10-06", "15:00"), now="2026-10-06T15:05")
            api(e, ["finish", "remind"], {"run_id": RID1, "status": "ok", "report": "Reminded.\n"}, now="2026-10-06T15:06")
            key = sched(e, "remind", now="2026-10-08T09:00")["date_key"]
            out, _ = api(e, ["report", "remind", "--date", key], now="2026-10-08T09:00")
            self.assertEqual((out["date_key"], out["run_id"], out["report"]), ("2026-10-06", RID1, "Reminded.\n"))
        finally:
            e.cleanup()

    def test_a_headless_day_report_is_returned_for_its_latest_run(self):
        self.run_one(RID1, "x\n")
        os.remove(os.path.join(self.e.cfg["reports_dir"], "morning-%s-%s.md" % (DS, RID1)))      # a background run only writes the day file
        out, _ = self.rep("morning", "--run", RID1)
        self.assertEqual(out["report"], "x\n")


if __name__ == "__main__":
    unittest.main()
