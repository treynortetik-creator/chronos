#!/usr/bin/env python3
"""Review fixes for the `times` / `meta` change: named days in the API `when`, meta shown before it is approved, a time<->times switch on the
session path, Run now on a job whose slot stalled, and pruning of per-slot reports and logs. Real CLI, fixed clock (CHRONOS_NOW)."""
import json
import os
import sys
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_helpers import *  # noqa: E402,F401,F403
from test_times import Base, hb, hl, MON, K, RID, claim_req  # noqa: E402,F401


class NamedDaysRoundTrip(Base):
    jobs = []

    def test_validate_then_apply_a_times_job_on_named_days(self):
        ch = create_change("named", when={"type": "times", "days": ["mon", "wed"], "times": ["09:45", "12:15"]}, runner="headless")
        v = validated(self.e, ch)
        self.assertTrue(v["ok"], v)
        self.assertEqual(v["normalized"]["draft"]["when"], {"type": "times", "days": ["mon", "wed"], "times": ["09:45", "12:15"]})
        out, _ = api(self.e, ["apply"], {"change": v["normalized"], "expected_etag": v["expected_etag"]})
        self.assertTrue(out["ok"], out)
        s = sched(self.e, "named")
        self.assertEqual(s["when"], {"type": "times", "days": ["mon", "wed"], "times": ["09:45", "12:15"]})
        self.assertEqual(json.load(open(self.e.cfg["jobs_file"]))[0]["days"], "mon,wed")

    def test_the_snapshot_of_a_hand_written_mon_wed_job_says_names_and_feeds_back_into_validate(self):
        self.e.set_jobs([hl(id="hand", days="mon,wed", times=["09:45", "12:15"])])
        s = sched(self.e, "hand")
        self.assertEqual(s["when"]["days"], ["mon", "wed"])
        v = validated(self.e, create_change("copy", when=s["when"], runner="headless"))
        self.assertTrue(v["ok"], v)
        out, _ = api(self.e, ["apply"], {"change": v["normalized"], "expected_etag": v["expected_etag"]})
        self.assertTrue(out["ok"], out)

    def test_daily_and_weekdays_are_still_words_and_monthdays_still_numbers(self):
        sys.path.insert(0, os.path.join(ROOT, "lib"))
        for days in ("daily", "weekdays"):
            self.assertEqual(C.when_obj({"enabled": True, "days": days, "times": ["09:00"], "time": "09:00"})["days"], days)
        self.assertEqual(C.when_obj({"enabled": True, "days": "dom:1,15", "time": "09:00"}), {"type": "monthdays", "days": [1, 15], "time": "09:00"})


class SwitchTimesToPlainOnTheSessionPath(Base):
    """A session job ran its 09:45 slot, then was edited to a plain 09:45 the same day: the day must not run again."""
    jobs = [hb()]

    def switch(self):
        self.e.touch(self.st, "ran-hb-" + K[0])
        self.e.set_jobs([job("hb", time="09:45", days="weekdays", catchup_min=1440, in_session=True, headless=False)])

    def test_it_is_not_offered_to_the_session(self):
        self.switch()
        out, _ = api(self.e, ["due", "--for", "session"], now=MON + "T10:00")
        self.assertEqual(out["due"], [])

    def test_it_cannot_be_claimed_by_the_session(self):
        self.switch()
        self.assertEqual(self.claim(RID[0], MON, MON + "T10:00"), {"ok": False, "reason": "already_done"})
        self.assertFalse(os.path.isdir(self.p("claim-hb-" + MON)))

    def test_the_day_reads_as_ran(self):
        self.switch()
        self.assertEqual(sched(self.e, "hb", now=MON + "T10:00")["today"], "ran")

    def test_a_plain_session_job_with_no_slot_marker_is_still_offered(self):
        self.e.set_jobs([job("hb", time="09:45", days="weekdays", catchup_min=1440, in_session=True, headless=False)])
        out, _ = api(self.e, ["due", "--for", "session"], now=MON + "T10:00")
        self.assertEqual([d["ds"] for d in out["due"]], [MON])

    def test_run_now_is_not_blocked_by_the_slot_that_ran(self):
        self.switch()
        self.assertEqual(self.claim(RID[0], None, MON + "T10:00", manual=True)["ok"], True)


class RunNowDoesNotReopenAStalledSlot(Base):
    jobs = [hb(times=["09:45", "12:15"], catchup_min=60, in_session=True, headless=True)]
    STALLED = "stalled-hb-" + MON + "T1215"

    def due_at(self, now):
        return self.e.due(now)[1]

    def test_a_session_run_now_then_the_tick_does_not_run_the_slot_again(self):
        self.e.touch(self.st, self.STALLED)
        self.assertEqual(self.claim(RID[0], None, MON + "T12:40", manual=True)["ok"], True)
        self.assertTrue(os.path.exists(self.p(self.STALLED)))                    # not cleared at claim time: the run may still fail
        self.assertEqual(self.finish(RID[0], MON + "T12:42"), {"ok": True})
        self.assertEqual(self.due_at(MON + "T12:45"), [])                        # the slot is satisfied by the Run now
        self.assertEqual(self.cli("claim", "hb", MON + "T1215", CHRONOS_NOW=MON + "T12:45").stdout.strip(), "ALREADY_DONE")
        codes = [i["code"] for i in api(self.e, ["health"], now=MON + "T12:46")[0]["issues"] if i["schedule_id"] == "hb"]
        self.assertNotIn("launch_stalled", codes)

    def test_a_failed_run_now_leaves_the_stall_in_place(self):
        self.e.touch(self.st, self.STALLED)
        self.claim(RID[0], None, MON + "T12:40", manual=True)
        self.finish(RID[0], MON + "T12:42", status="failed")
        self.assertTrue(os.path.exists(self.p(self.STALLED)))
        self.assertEqual(self.due_at(MON + "T12:45"), [])

    def test_a_slot_whose_window_is_over_is_just_cleared(self):
        self.e.touch(self.st, "stalled-hb-" + MON + "T0945")
        self.claim(RID[0], None, MON + "T12:40", manual=True)
        self.finish(RID[0], MON + "T12:42")
        self.assertFalse(os.path.exists(self.p("stalled-hb-" + MON + "T0945")))
        self.assertFalse(os.path.exists(self.p("ran-hb-" + MON + "T0945")))      # nothing is recorded as run for a slot that never could

    def test_a_headless_run_now_through_the_cli_does_the_same(self):
        self.e.touch(self.st, self.STALLED)
        r = self.cli("run", "hb", CHRONOS_NOW=MON + "T12:40", FAKE_MODE="ok")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.e.wait_for(lambda: os.path.exists(self.p("ran-hb-" + MON + "T1240")))
        self.e.wait_for(lambda: not os.path.exists(self.p("claim-hb-" + MON + "T1240")))      # the run is over: a live claim would hold the slot back by itself
        self.assertFalse(os.path.exists(self.p(self.STALLED)))
        self.assertEqual(self.due_at(MON + "T12:45"), [])


class PruneReportsAndLogs(Base):
    jobs = [hl()]

    def test_old_slot_reports_and_logs_go_and_the_latest_per_job_stays(self):
        rep, logs = self.e.cfg["reports_dir"], self.e.cfg["logs_dir"]
        ulid = "r_" + "0" * 25 + "1"
        old = ["hb-2026-09-20T0945.md", "hb-2026-09-20T0945-%s.md" % ulid, "hb-2026-09-20T0945-%s.late.md" % ulid, "hb-2026-09-21T1215.md"]
        new = ["hb-2026-09-22T1215.md"]                                            # old too, but the latest report of hb: kept
        plain = ["hb-2026-09-01.md", "hb-2026-09-01-abcdef12.md", "other-2026-10-11T0900.md"]
        oldlogs = ["hb-2026-09-20T0945-0950.log", "hb-2026-09-21T1215-1220.log"]
        newlogs = ["hb-2026-09-22T1215-1220.log"]
        plainlogs = ["hb-2026-09-01-0900.log", "chronos.log", "hb-2026-09-01-0900-abcdef12.log"]
        for n in old + new + plain:
            self.e.touch(rep, n)
        for n in oldlogs + newlogs + plainlogs:
            self.e.touch(logs, n)
        r = self.cli("prune", CHRONOS_NOW="2026-10-12T09:00")
        self.assertEqual(r.returncode, 0, r.stderr)
        left = lambda d, ns: [n for n in ns if os.path.exists(os.path.join(d, n))]
        self.assertEqual(left(rep, old), [], "old per-slot reports remain")
        self.assertEqual(left(logs, oldlogs), [], "old per-slot logs remain")
        self.assertEqual(left(rep, new + plain), new + plain)
        self.assertEqual(left(logs, newlogs + plainlogs), newlogs + plainlogs)

    def test_a_recent_slot_report_is_kept_and_the_old_ones_of_that_job_still_go(self):
        rep = self.e.cfg["reports_dir"]
        for n in ("hb-2026-09-20T0945.md", "hb-2026-10-10T0945.md"):
            self.e.touch(rep, n)
        self.cli("prune", CHRONOS_NOW="2026-10-12T09:00")
        self.assertFalse(os.path.exists(os.path.join(rep, "hb-2026-09-20T0945.md")))
        self.assertTrue(os.path.exists(os.path.join(rep, "hb-2026-10-10T0945.md")))

    def test_the_only_report_of_a_job_is_never_pruned(self):
        rep = self.e.cfg["reports_dir"]
        self.e.touch(rep, "hb-2026-08-01T0945.md")
        self.cli("prune", CHRONOS_NOW="2026-10-12T09:00")
        self.assertTrue(os.path.exists(os.path.join(rep, "hb-2026-08-01T0945.md")))


class TheUiShowsMetaItApproves(Base):
    jobs = [hl(meta={"sources": ["<script>alert(1)</script>", "inbox"]})]
    cfg = {"session_runner": "talos", "approval_required": True}

    def setUp(self):
        Base.setUp(self)
        self.ui = UIServer(self.e)

    def tearDown(self):
        self.ui.stop()
        Base.tearDown(self)

    def test_the_job_view_carries_meta_and_the_hash_that_covers_it(self):
        st, d = self.ui.req("GET", "/api/jobs/hb")
        self.assertEqual(st, 200)
        self.assertEqual(d["meta"], {"sources": ["<script>alert(1)</script>", "inbox"]})
        self.assertEqual(d["content_hash"], C.job_content_hash(self.e.cfg, C.load_jobs(self.e.cfg)[0]))

    def test_a_job_without_meta_gets_no_meta_key(self):
        self.e.set_jobs([hl()])
        st, d = self.ui.req("GET", "/api/jobs/hb")
        self.assertNotIn("meta", d)

    def test_approving_the_hash_that_was_shown_works_and_a_changed_meta_is_refused(self):
        st, d = self.ui.req("GET", "/api/jobs/hb")
        recs = json.load(open(self.e.cfg["jobs_file"]))
        recs[0]["meta"] = {"sources": ["exfiltrate"]}
        json.dump(recs, open(self.e.cfg["jobs_file"], "w"))
        st, r = self.ui.req("POST", "/api/jobs/hb/approve", {"content_hash": d["content_hash"]})
        self.assertEqual(st, 409, r)
        st, d2 = self.ui.req("GET", "/api/jobs/hb")
        self.assertEqual(d2["meta"], {"sources": ["exfiltrate"]})
        st, r = self.ui.req("POST", "/api/jobs/hb/approve", {"content_hash": d2["content_hash"]})
        self.assertEqual(st, 200, r)

    def test_the_page_script_renders_meta_as_text_in_the_job_view_and_the_approve_dialog(self):
        js = open(os.path.join(ROOT, "ui", "static", "app.js")).read()
        self.assertIn("function metaBlock", js)
        body = js[js.index("function metaBlock"):]
        body = body[:body.index("\n  }\n")]
        self.assertIn("JSON.stringify(j.meta, null, 2)", body)
        self.assertNotIn("innerHTML", body)
        a = js[js.index("function approveOnDisk"):]
        a = a[:a.index("\n  }\n")]
        self.assertIn("metaBlock(j)", a)
        self.assertIn("metaBlock(j)", js[js.index("function renderJob"):])


if __name__ == "__main__":
    unittest.main(verbosity=1)
