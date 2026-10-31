#!/usr/bin/env python3
"""HB0: `times` (several fire times a day for one job) and `meta` (an opaque object on the job record).
Every case goes through the real `chronos` / `chronos api` CLI against a throwaway home with a fixed clock (CHRONOS_NOW).
A job with a plain `time` must behave exactly as before: the last class pins that."""
import json
import os
import subprocess
import sys
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_helpers import *  # noqa: E402,F401,F403

MON = "2026-10-05"                      # a Monday
SLOTS = ["09:45", "12:15", "14:45", "17:15"]
K = [MON + "T0945", MON + "T1215", MON + "T1445", MON + "T1715"]
RID = ["r_" + "0" * 25 + str(i) for i in range(1, 9)]


def hb(**kw):
    """A session job with four slots and a window long enough that all four are open at once."""
    j = job("hb", time="09:45", times=list(SLOTS), days="weekdays", catchup_min=1440, in_session=True, headless=False)
    j.update(kw)
    return j


def claim_req(rid, ds, **kw):
    r = {"run_id": rid, "ds": ds, "fire_at": None, "manual": False, "again": False, "runner": "session", "bridge_instance": "inst"}
    r.update(kw)
    return r


class Base(unittest.TestCase):
    jobs = None
    cfg = {"session_runner": "talos"}

    def setUp(self):
        self.e = Env(self.jobs if self.jobs is not None else [hb()], **self.cfg)
        self.st = self.e.cfg["state_dir"]

    def tearDown(self):
        self.e.cleanup()

    def p(self, *parts):
        return os.path.join(self.st, *parts)

    def claim(self, rid, ds, now, jid="hb", expect=0, **kw):
        return api(self.e, ["claim", jid], claim_req(rid, ds, **kw), now=now, expect=expect)[0]

    def finish(self, rid, now, status="ok", report="done\n", jid="hb"):
        return api(self.e, ["finish", jid], {"run_id": rid, "status": status, "report": report}, now=now)[0]

    def cli(self, *args, **env):
        return self.e.run([sys.executable, CLI] + list(args), **env)


class SlotsAreIndependent(Base):
    NOW = MON + "T17:20"

    def test_four_slots_claim_finish_and_report_independently_in_one_day(self):
        for i, k in enumerate(K):
            self.assertEqual(self.claim(RID[i], k, self.NOW), {"ok": True, "run_id": RID[i], "ds": k})
        for i, k in enumerate(K):
            self.assertTrue(os.path.isdir(self.p("claim-hb-" + k)))
        self.assertFalse(os.path.exists(self.p("claim-hb-" + MON)))                  # never the date key
        for i, k in enumerate(K):
            self.assertEqual(self.finish(RID[i], self.NOW, report="report %d\n" % i), {"ok": True})
        for i, k in enumerate(K):
            self.assertTrue(os.path.exists(self.p("ran-hb-" + k)), k)
            self.assertEqual(open(self.p("reports", "hb-%s.md" % k)).read(), "report %d\n" % i)
            self.assertEqual(open(self.p("reports", "hb-%s-%s.md" % (k, RID[i]))).read(), "report %d\n" % i)
            self.assertFalse(os.path.exists(self.p("claim-hb-" + k)))
        self.assertFalse(os.path.exists(self.p("ran-hb-" + MON)))
        rep, _ = api(self.e, ["report", "hb", "--date", K[1]], now=self.NOW)
        self.assertEqual((rep["date_key"], rep["report"]), (K[1], "report 1\n"))

    def test_a_second_claim_on_one_slot_is_running_elsewhere_and_other_slots_are_free(self):
        self.claim(RID[0], K[0], self.NOW)
        self.assertEqual(self.claim(RID[1], K[0], self.NOW)["reason"], "running_elsewhere")
        self.assertTrue(self.claim(RID[1], K[1], self.NOW)["ok"])

    def test_a_failed_marker_on_slot_2_does_not_block_slot_3(self):
        self.assertTrue(self.claim(RID[1], K[1], self.NOW)["ok"])
        self.finish(RID[1], self.NOW, status="failed", report="boom\n")
        self.assertTrue(os.path.exists(self.p("failed-hb-" + K[1])))
        self.assertEqual(self.claim(RID[4], K[1], self.NOW)["reason"], "failed_today")
        self.assertEqual(self.claim(RID[2], K[2], self.NOW), {"ok": True, "run_id": RID[2], "ds": K[2]})
        due = [d["ds"] for d in api(self.e, ["due", "--for", "session"], now=self.NOW)[0]["due"]]
        self.assertEqual(due, [K[0], K[3]])                                          # 2 failed, 3 claimed; 1 and 4 still offered

    def test_an_unknown_slot_is_not_due_unless_the_run_is_manual(self):
        self.assertEqual(self.claim(RID[0], MON + "T1000", self.NOW)["reason"], "not_due")
        self.assertEqual(self.claim(RID[0], MON, self.NOW)["reason"], "not_due")      # the date key is not a slot of a times job
        out = self.claim(RID[1], MON + "T1337", self.NOW, manual=True)                # Run now: an ad-hoc key
        self.assertEqual(out, {"ok": True, "run_id": RID[1], "ds": MON + "T1337"})
        for k in K:
            self.assertFalse(os.path.exists(self.p("ran-hb-" + k)))                   # a slot was not consumed

    def test_a_slot_not_yet_reached_is_not_due(self):
        self.assertEqual(self.claim(RID[0], K[3], MON + "T17:00")["reason"], "not_due")

    def test_last_run_is_the_latest_slot(self):
        for i in (0, 1):
            self.claim(RID[i], K[i], self.NOW)
            self.finish(RID[i], self.NOW)
        s = sched(self.e, "hb", now=self.NOW)
        self.assertEqual(s["last_run"]["ds"], K[1])
        d, _ = api(self.e, ["detail", "hb"], now=self.NOW)
        self.assertEqual([r["ds"] for r in d["runs"]], [K[1], K[0]])


class Midnight(Base):
    jobs = [hb(times=["09:45", "23:30"], catchup_min=60)]

    def test_a_2330_slot_with_a_60_minute_window_is_offered_at_0005_on_yesterdays_key(self):
        out, _ = api(self.e, ["due", "--for", "session"], now="2026-10-06T00:05")
        self.assertEqual([(d["id"], d["ds"]) for d in out["due"]], [("hb", MON + "T2330")])
        self.assertTrue(self.claim(RID[0], MON + "T2330", "2026-10-06T00:05")["ok"])
        self.assertTrue(os.path.isdir(self.p("claim-hb-" + MON + "T2330")))

    def test_it_is_gone_once_the_window_has_passed(self):
        out, _ = api(self.e, ["due", "--for", "session"], now="2026-10-06T00:31")
        self.assertEqual(out["due"], [])
        self.assertEqual(self.claim(RID[0], MON + "T2330", "2026-10-06T00:31")["reason"], "window_passed")

    def test_the_headless_tick_sees_the_same_slot(self):
        self.e.set_jobs([hb(times=["09:45", "23:30"], catchup_min=60, in_session=False, headless=True)])
        r, lines = self.e.due("2026-10-06T00:15")
        self.assertEqual(lines, ["hb|" + MON + "T2330"], r.stderr)


class CliClaim(Base):
    jobs = [hb(in_session=False, headless=True)]

    def test_a_slot_key_claim_never_falls_back_to_the_date_key(self):
        r = self.cli("claim", "hb", K[1], CHRONOS_NOW=MON + "T12:20")
        self.assertEqual((r.stdout.strip(), r.returncode), ("CLAIMED", 0), r.stderr)
        self.assertTrue(os.path.isdir(self.p("claim-hb-" + K[1])))
        self.assertFalse(os.path.exists(self.p("claim-hb-" + MON)))
        r = self.cli("claim", "hb", K[1], CHRONOS_NOW=MON + "T12:20")
        self.assertEqual(r.stdout.strip(), "RUNNING_ELSEWHERE")

    def test_a_garbled_key_on_a_times_job_is_refused_not_rewritten_to_today(self):
        r = self.cli("claim", "hb", MON + "T12", CHRONOS_NOW=MON + "T12:20")
        self.assertNotEqual(r.stdout.strip(), "CLAIMED")
        self.assertEqual(os.listdir(self.st) if os.path.isdir(self.st) else [], [])

    def test_unattended_refuses_a_key_that_is_not_a_slot(self):
        r = self.cli("claim", "hb", MON + "T1300", "--unattended", CHRONOS_NOW=MON + "T13:05")
        self.assertEqual(r.stdout.strip(), "NOT_DUE")

    def test_due_lists_each_open_slot_and_a_ran_slot_drops_out(self):
        r, lines = self.e.due(MON + "T12:30")
        self.assertEqual(lines, ["hb|" + K[0], "hb|" + K[1]], r.stderr)
        self.e.touch(self.st, "ran-hb-" + K[0])
        r, lines = self.e.due(MON + "T12:30")
        self.assertEqual(lines, ["hb|" + K[1]])

    def test_the_grace_period_applies_to_each_slot(self):
        r, lines = self.e.due(MON + "T12:20")                      # 5 minutes after 12:15, grace is 10
        self.assertEqual(lines, ["hb|" + K[0]])

    def test_a_weekend_has_no_slots(self):
        r, lines = self.e.due("2026-10-11T12:30")          # a Sunday; Friday's windows are over
        self.assertEqual(lines, [])

    def test_done_and_release_take_the_slot(self):
        self.cli("claim", "hb", K[1], CHRONOS_NOW=MON + "T12:20")
        r = self.cli("done", "hb", K[1], CHRONOS_NOW=MON + "T12:20")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(self.p("ran-hb-" + K[1])))
        self.assertFalse(os.path.exists(self.p("claim-hb-" + K[1])))
        self.assertFalse(os.path.exists(self.p("ran-hb-" + MON)))

    def test_list_and_english(self):
        r = self.cli("list", CHRONOS_NOW=MON + "T08:00")
        self.assertIn("Weekdays at 9:45 AM, 12:15 PM, 2:45 PM and 5:15 PM", r.stdout)
        self.assertIn("next: 2026-10-05T09:45", r.stdout)

    def test_next_fires_walks_the_slots_then_the_next_day(self):
        sys.path.insert(0, os.path.join(ROOT, "lib"))
        j = hb()
        at = C.datetime.datetime(2026, 10, 9, 12, 0)                  # a Friday
        got = [x.strftime("%a %H:%M") for x in C.next_fires(j, at, 4)]
        self.assertEqual(got, ["Fri 12:15", "Fri 14:45", "Fri 17:15", "Mon 09:45"])


class Validation(Base):
    jobs = []

    def check(self, **kw):
        return api(self.e, ["validate"], create_change("hb2", **kw), now=MON + "T08:00", expect=kw.pop("expect", 0))[0]

    def when(self, times, days="weekdays", **kw):
        w = {"type": "times", "days": days, "times": times}
        w.update(kw)
        return w

    def test_a_good_times_job_validates_and_round_trips_through_when(self):
        v = api(self.e, ["validate"], create_change("hb2", when=self.when(SLOTS), runner="headless"), now=MON + "T08:00")[0]
        self.assertTrue(v["ok"], v)
        self.assertEqual(v["normalized"]["draft"]["when"], {"type": "times", "days": "weekdays", "times": SLOTS})
        self.assertEqual(v["preview"]["when_text"], "Weekdays at 9:45 AM, 12:15 PM, 2:45 PM and 5:15 PM")
        self.assertEqual(v["preview"]["next_runs"][0], "2026-10-05T09:45:00-04:00")
        out = apply_ok(self.e, create_change("hb2", when=self.when(SLOTS), runner="headless"), now=MON + "T08:00")
        rec = json.load(open(self.e.cfg["jobs_file"]))[0]
        self.assertEqual((rec["times"], rec["time"], rec["days"], rec["once"]), (SLOTS, "09:45", "weekdays", None))   # `time` mirrors times[0]
        s = sched(self.e, "hb2", now=MON + "T08:00")
        self.assertEqual(s["when"], {"type": "times", "days": "weekdays", "times": SLOTS})

    def bad(self, times, msg, days="weekdays", when_extra=None, **kw):
        out, r = api(self.e, ["validate"], create_change("hb2", when=self.when(times, days, **(when_extra or {})), runner="headless", **kw), now=MON + "T08:00", expect=1)
        e = err(r)
        self.assertEqual(e["code"], "schedule_invalid")
        self.assertIn(msg, e["message"])

    def test_refusals(self):
        self.bad([], "at least")
        self.bad(["09:45", "09:45"], "ascending")
        self.bad(["12:15", "09:45"], "ascending")
        self.bad(["09:45", "09:59"], "15 minutes")
        self.bad(["09:45", "9:50"], "HH:MM")
        self.bad(["09:45", 1215], "HH:MM")
        self.bad(["%02d:00" % h for h in range(0, 24)] + ["23:30"], "at most")
        self.bad("09:45", "list")

    def test_exactly_15_minutes_apart_is_fine(self):
        v = api(self.e, ["validate"], create_change("hb2", when=self.when(["09:00", "09:15"]), runner="headless"), now=MON + "T08:00")[0]
        self.assertTrue(v["ok"])

    def test_times_never_with_time_or_once_in_the_when_object(self):
        self.bad(SLOTS, "exactly", when_extra={"time": "09:45"})
        self.bad(SLOTS, "exactly", when_extra={"at": "2026-10-06T09:00"})

    def test_a_times_job_needs_talos_or_headless_when_it_runs_in_a_session(self):
        out, r = api(self.e, ["validate"], create_change("hb2", when=self.when(SLOTS), runner="session"), now=MON + "T08:00")
        self.assertTrue(out["ok"])                         # self.e runs session_runner talos: a session times job is fine
        e2 = Env([], session_runner="hook")
        try:
            out, r = api(e2, ["validate"], create_change("hb2", when=self.when(SLOTS), runner="session"), now=MON + "T08:00", expect=1)
            self.assertEqual(err(r)["code"], "schedule_invalid")
            self.assertIn("talos", err(r)["message"])
            out, r = api(e2, ["validate"], create_change("hb2", when=self.when(SLOTS), runner="headless"), now=MON + "T08:00")
            self.assertTrue(out["ok"])
        finally:
            e2.cleanup()
            os.environ["CHRONOS_CONFIG"] = self.e.cfg_path

    def test_validate_job_record_rejects_a_hand_written_bad_record(self):
        for bad in ({"times": ["09:45", "09:50"]}, {"times": "09:45"}, {"times": ["09:45", "12:15"], "time": "12:15"}, {"times": [], "time": "09:45"},
                    {"times": ["09:45"], "once": "2026-10-06T09:45"}):
            j = job("x1", **bad)
            with self.assertRaises(ValueError, msg=str(bad)):
                C.validate_job_record(j)
        C.validate_job_record(job("x1", times=["09:45", "12:15"], time="09:45"))
        C.validate_job_record(job("x1", times=["09:45", "12:15"], time=None))

    def test_a_hand_written_invalid_times_job_is_skipped_by_the_tick_with_a_note(self):
        self.e.set_jobs([job("x1", times=["09:45", "09:50"], time="09:45", days="daily")])
        r, lines = self.e.due(MON + "T10:00")
        self.assertEqual(lines, [])
        self.assertIn("skip job 'x1'", r.stderr)


class Meta(Base):
    jobs = []
    cfg = {"session_runner": "talos", "approval_required": True}

    def make(self, **kw):
        apply_ok(self.e, create_change("hb2", when={"type": "times", "days": "weekdays", "times": SLOTS}, runner="headless", **kw), now=MON + "T08:00")

    def hash(self):
        return sched(self.e, "hb2", now=MON + "T08:00")["content_hash"]

    def edit_job(self, **kw):
        jobs = json.load(open(self.e.cfg["jobs_file"]))
        jobs[0].update(kw)
        json.dump(jobs, open(self.e.cfg["jobs_file"], "w"))

    def test_a_meta_edit_holds_the_job_and_a_times_edit_does_not(self):
        self.make(meta={"sources": ["a"]})
        self.assertEqual(sched(self.e, "hb2", now=MON + "T08:00")["today"], "pending")
        h0 = self.hash()
        self.edit_job(times=["09:45", "12:15", "14:45"])
        self.assertEqual(self.hash(), h0)
        self.assertEqual(sched(self.e, "hb2", now=MON + "T08:00")["today"], "pending")        # not held
        self.edit_job(meta={"sources": ["a", "b"]})
        self.assertNotEqual(self.hash(), h0)
        self.assertEqual(sched(self.e, "hb2", now=MON + "T08:00")["today"], "held")

    def test_a_job_without_meta_hashes_as_before(self):
        self.make()
        self.assertEqual(self.hash(), expected_content_hash(self.e, "hb2"))
        self.edit_job(meta={"x": 1})
        self.assertNotEqual(self.hash(), expected_content_hash_plain(self.e, "hb2"))

    def test_meta_over_4_kib_is_refused_and_exactly_the_cap_is_accepted(self):
        pad = 4096 - len(json.dumps({"p": ""}, separators=(",", ":")))
        self.make(meta={"p": "x" * pad})
        rec = json.load(open(self.e.cfg["jobs_file"]))[0]
        self.assertEqual(len(json.dumps(rec["meta"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()), 4096)
        out, r = api(self.e, ["validate"], create_change("hb3", meta={"p": "x" * (pad + 1)}), now=MON + "T08:00", expect=1)
        self.assertEqual(err(r)["code"], "schedule_invalid")
        self.assertIn("4 KiB", err(r)["message"])

    def test_meta_must_be_an_object(self):
        out, r = api(self.e, ["validate"], create_change("hb3", meta=["a"]), now=MON + "T08:00", expect=1)
        self.assertIn("object", err(r)["message"])

    def test_meta_can_be_updated_and_cleared_with_an_update_change(self):
        self.make(meta={"a": 1})
        e0 = sched(self.e, "hb2", now=MON + "T08:00")["etag"]
        apply_ok(self.e, {"op": "update", "id": "hb2", "base_etag": e0, "set": {"meta": {"a": 2}}}, now=MON + "T08:00")
        self.assertEqual(json.load(open(self.e.cfg["jobs_file"]))[0]["meta"], {"a": 2})
        e1 = sched(self.e, "hb2", now=MON + "T08:00")["etag"]
        apply_ok(self.e, {"op": "update", "id": "hb2", "base_etag": e1, "set": {"meta": None}}, now=MON + "T08:00")
        self.assertNotIn("meta", json.load(open(self.e.cfg["jobs_file"]))[0])

    def test_snapshot_carries_meta(self):
        self.make(meta={"a": 1})
        self.assertEqual(sched(self.e, "hb2", now=MON + "T08:00")["meta"], {"a": 1})


def expected_content_hash_plain(e, jid):
    return expected_content_hash(e, jid)


class Features(Base):
    jobs = []

    def test_api_version_lists_the_features_and_keeps_api_1(self):
        out, _ = api(self.e, ["version"])
        self.assertEqual(out, {"api": 1, "chronos": C.VERSION, "session_runner": "talos", "approval_required": False, "features": ["times", "meta"]})


class WaitNotifyAndMissed(Base):
    cfg = {"session_runner": "talos", "session_wait_notify_min": 10}

    def wait(self, now):
        return self.e.run([sys.executable, CLI, "runlog", "wait-notify"], CHRONOS_NOW=now)

    def test_wait_notice_is_once_per_date_for_a_times_job(self):
        r = self.wait(MON + "T10:00")                       # 15 min after 09:45, no session beat
        self.assertEqual(len(r.stdout.splitlines()), 1, r.stderr)
        self.assertTrue(r.stdout.startswith("hb|" + K[0] + "|"))
        self.assertTrue(os.path.exists(self.p("waitnotified-hb-" + MON)))
        r = self.wait(MON + "T12:30")                       # slot 2 waits too: still the same date
        self.assertEqual(r.stdout.strip(), "")

    def test_a_new_date_gets_its_own_notice(self):
        self.wait(MON + "T10:00")
        r = self.wait("2026-10-06T10:00")
        self.assertEqual(len(r.stdout.splitlines()), 1)

    def test_a_plain_time_job_keeps_its_old_marker_name(self):
        self.e.set_jobs([job("pl", time="09:45", in_session=True, headless=False, days="daily")])
        self.wait(MON + "T10:00")
        self.assertTrue(os.path.exists(self.p("waitnotified-pl-" + MON)))

    def health(self, now):
        return api(self.e, ["health"], now=now)[0]

    def test_two_consecutive_missed_slots_raise_it_and_one_does_not(self):
        self.e.set_jobs([hb(catchup_min=30, in_session=False, headless=True)])
        codes = lambda now: [i["code"] for i in self.health(now)["issues"] if i["schedule_id"] == "hb"]
        self.assertNotIn("missed_recently", codes(MON + "T11:00"))
        self.e.touch(self.st, "ran-hb-" + K[0])                                  # slot 1 ran; only slot 2 is missed at 13:00
        self.assertNotIn("missed_recently", codes(MON + "T13:00"))
        os.remove(self.p("ran-hb-" + K[0]))
        self.assertIn("missed_recently", codes(MON + "T13:00"))                   # 1 and 2 both missed


class Prune(Base):
    jobs = [hb(in_session=False, headless=True)]

    def test_slot_markers_older_than_8_days_are_pruned_and_nothing_else(self):
        keep = ["ran-hb-2026-10-05T0945", "ran-hb-2026-10-05", "ran-other-2026-09-01", "claim-hb-2026-09-20T0945", "approved", "headless.blocked"]
        gone = ["ran-hb-2026-09-25T0945", "failed-hb-2026-09-25T1215", "stalled-hb-2026-09-25T1445", "waitnotified-hb-2026-09-25T1715"]
        os.makedirs(self.st, exist_ok=True)
        for n in keep + gone:
            self.e.touch(self.st, n)
        r = self.cli("prune", CHRONOS_NOW="2026-10-06T09:00")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(n for n in gone if os.path.exists(self.p(n))), [])
        self.assertEqual(sorted(n for n in keep if not os.path.exists(self.p(n))), [])

    def test_the_8_day_edge(self):
        os.makedirs(self.st, exist_ok=True)
        for n in ("ran-hb-2026-10-01T0945", "ran-hb-2026-09-30T0945"):
            self.e.touch(self.st, n)
        self.cli("prune", CHRONOS_NOW="2026-10-09T09:00")                        # 8 days after 10-01 is 10-09: kept; 10-08 and older go
        self.assertTrue(os.path.exists(self.p("ran-hb-2026-10-01T0945")))
        self.assertFalse(os.path.exists(self.p("ran-hb-2026-09-30T0945")))

    def test_the_tick_prunes(self):
        os.makedirs(self.st, exist_ok=True)
        self.e.touch(self.st, "ran-hb-2026-09-20T0945")
        r = self.e.run(["/bin/bash", TICK], CHRONOS_NOW="2026-10-06T03:00")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.p("ran-hb-2026-09-20T0945")))


class RunScript(Base):
    jobs = [hb(in_session=False, headless=True, catchup_min=1440)]

    def test_a_headless_slot_runs_with_its_own_markers_log_and_report(self):
        r = self.e.run(["/bin/bash", TICK], CHRONOS_NOW=MON + "T12:30", FAKE_MODE="ok")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.e.wait_for(lambda: os.path.exists(self.p("ran-hb-" + K[0])) and os.path.exists(self.p("ran-hb-" + K[1])))
        for k in K[:2]:
            self.assertTrue(os.path.exists(self.p("ran-hb-" + k)), k)
            self.assertTrue(os.path.exists(self.p("reports", "hb-%s.md" % k)), k)
        self.assertFalse(os.path.exists(self.p("ran-hb-" + K[2])))
        self.assertFalse(os.path.exists(self.p("ran-hb-" + MON)))
        self.assertEqual(json.load(open(self.e.cfg["jobs_file"]))[0]["id"], "hb")


class TheUiKeepsTimes(Base):
    jobs = [hb(in_session=False, headless=True, name="Multi Run")]

    def setUp(self):
        Base.setUp(self)
        self.ui = UIServer(self.e)

    def tearDown(self):
        self.ui.stop()
        Base.tearDown(self)

    def rec(self):
        return json.load(open(self.e.cfg["jobs_file"]))[0]

    def test_the_job_listing_shows_times(self):
        st, jobs = self.ui.req("GET", "/api/jobs")
        self.assertEqual(st, 200)
        j = next(x for x in (jobs["jobs"] if isinstance(jobs, dict) else jobs) if x["id"] == "hb")
        self.assertEqual(j["times"], SLOTS)
        self.assertIn("12:15 PM", j["schedule_text"])

    def test_saving_the_form_with_the_mirrored_time_keeps_times(self):
        st, r = self.ui.req("POST", "/api/jobs/hb/schedule", {"time": "09:45", "days": "weekdays", "catchup_min": 120, "name": "Multi Run 2"})
        self.assertEqual(st, 200, r)
        self.assertEqual(self.rec()["times"], SLOTS)
        self.assertEqual((self.rec()["time"], self.rec()["catchup_min"], self.rec()["name"]), ("09:45", 120, "Multi Run 2"))

    def test_saving_with_a_different_single_time_is_refused_not_silently_dropped(self):
        st, r = self.ui.req("POST", "/api/jobs/hb/schedule", {"time": "08:00", "days": "weekdays", "catchup_min": 120})
        self.assertEqual(st, 422, r)
        self.assertEqual(self.rec()["times"], SLOTS)

    def test_a_times_edit_is_saved_and_time_follows_times0(self):
        st, r = self.ui.req("POST", "/api/jobs/hb/schedule", {"times": ["08:00", "11:00"], "days": "weekdays", "catchup_min": 120})
        self.assertEqual(st, 200, r)
        self.assertEqual((self.rec()["times"], self.rec()["time"]), (["08:00", "11:00"], "08:00"))

    def test_the_preview_takes_times(self):
        st, r = self.ui.req("GET", "/api/preview?times=08:00,11:00&days=weekdays&catchup_min=120")
        self.assertEqual(st, 200, r)
        self.assertTrue(r["ok"], r)
        self.assertIn("8:00 AM and 11:00 AM", r["text"])


class PlainTimeIsByteIdentical(Base):
    jobs = [job("morning", time="07:30", days="weekdays", in_session=True, headless=False)]

    def test_no_new_keys_appear_for_a_plain_job(self):
        s = sched(self.e, "morning", now=MON + "T07:45")
        for k in ("times", "meta", "slot"):
            self.assertNotIn(k, s)
        self.assertEqual(s["when"], {"type": "weekdays", "time": "07:30"})
        self.assertEqual(s["ds"], MON)

    def test_content_hash_and_etag_are_the_old_formula(self):
        s = sched(self.e, "morning", now=MON + "T07:45")
        self.assertEqual(s["content_hash"], expected_content_hash(self.e, "morning"))
        self.assertEqual(s["etag"], expected_etag(self.e, "morning"))

    def test_cli_output_is_unchanged(self):
        r = self.cli("list", CHRONOS_NOW=MON + "T07:00")
        self.assertIn("Weekdays at 7:30 AM", r.stdout.splitlines()[0])
        self.e.set_jobs([job("morning", time="07:30", days="weekdays")])
        r, lines = self.e.due(MON + "T07:45")
        self.assertEqual(lines, ["morning|" + MON])
        self.assertEqual(self.cli("claim", "morning", MON, CHRONOS_NOW=MON + "T07:45").stdout.strip(), "CLAIMED")
        self.assertTrue(os.path.isdir(self.p("claim-morning-" + MON)))

    def test_a_slot_shaped_ds_is_not_a_date_for_a_plain_job(self):
        self.e.set_jobs([job("morning", time="07:30", days="weekdays")])
        self.assertEqual(self.cli("claim", "morning", MON + "T0730", CHRONOS_NOW=MON + "T07:45").stdout.strip(), "CLAIMED")
        self.assertTrue(os.path.isdir(self.p("claim-morning-" + MON)))            # old behaviour: an unrecognised ds falls back to today's date key

    def test_the_session_claim_of_a_plain_job_rejects_a_slot_key(self):
        out, r = api(self.e, ["claim", "morning"], claim_req(RID[0], MON + "T0730"), now=MON + "T07:45", expect=1)
        self.assertEqual(err(r)["code"], "bad_request")

    def test_a_plain_job_with_a_times_key_absent_round_trips_in_the_ui(self):
        ui = UIServer(self.e)
        try:
            st, r = ui.req("POST", "/api/jobs/morning/schedule", {"time": "08:00", "days": "weekdays", "catchup_min": 180})
            self.assertEqual(st, 200, r)
            rec = json.load(open(self.e.cfg["jobs_file"]))[0]
            self.assertNotIn("times", rec)
            self.assertEqual(rec["time"], "08:00")
        finally:
            ui.stop()


if __name__ == "__main__":
    unittest.main(verbosity=1)
