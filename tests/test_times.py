#!/usr/bin/env python3
"""`times` (several fire times a day for one job) and `meta` (an opaque object on the job record).
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
    """A session job with four slots and a window long enough that every slot stays open all day (only the newest one runs; older ones are superseded)."""
    j = job("hb", time="09:45", times=list(SLOTS), days="weekdays", catchup_min=1440, in_session=True, headless=False)
    j.update(kw)
    if "times" in kw and "time" not in kw:
        j["time"] = kw["times"][0]
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

    def claim(self, rid, ds, now, jid="hb", expect=0, tz=NY, **kw):
        return api(self.e, ["claim", jid], claim_req(rid, ds, **kw), now=now, expect=expect, tz=tz)[0]

    def finish(self, rid, now, status="ok", report="done\n", jid="hb"):
        return api(self.e, ["finish", jid], {"run_id": rid, "status": status, "report": report}, now=now)[0]

    def cli(self, *args, **env):
        return self.e.run([sys.executable, CLI] + list(args), **env)


NOWS = [MON + "T09:50", MON + "T12:20", MON + "T14:50", MON + "T17:20"]       # just after each slot starts


class SlotsAreIndependent(Base):
    NOW = MON + "T17:20"

    def test_four_slots_claim_finish_and_report_independently_in_one_day(self):
        for i, k in enumerate(K):
            self.assertEqual(self.claim(RID[i], k, NOWS[i]), {"ok": True, "run_id": RID[i], "ds": k})
            self.assertTrue(os.path.isdir(self.p("claim-hb-" + k)))
            self.assertFalse(os.path.exists(self.p("claim-hb-" + MON)))                  # never the date key
            self.assertEqual(self.finish(RID[i], NOWS[i], report="report %d\n" % i), {"ok": True})
        for i, k in enumerate(K):
            self.assertTrue(os.path.exists(self.p("ran-hb-" + k)), k)
            self.assertEqual(open(self.p("reports", "hb-%s.md" % k)).read(), "report %d\n" % i)
            self.assertEqual(open(self.p("reports", "hb-%s-%s.md" % (k, RID[i]))).read(), "report %d\n" % i)
            self.assertFalse(os.path.exists(self.p("claim-hb-" + k)))
        self.assertFalse(os.path.exists(self.p("ran-hb-" + MON)))
        rep, _ = api(self.e, ["report", "hb", "--date", K[1]], now=self.NOW)
        self.assertEqual((rep["date_key"], rep["report"]), (K[1], "report 1\n"))

    def test_a_second_claim_on_one_slot_is_running_elsewhere_and_a_job_runs_one_slot_at_a_time(self):
        self.e.set_jobs([hb(times=["09:45", "10:00"])])
        k0, k1 = MON + "T0945", MON + "T1000"
        self.claim(RID[0], k0, MON + "T09:50")
        self.assertEqual(self.claim(RID[1], k0, MON + "T09:50")["reason"], "running_elsewhere")
        self.assertEqual(self.claim(RID[1], k1, MON + "T10:05")["reason"], "running_elsewhere")      # slot 2 starts while slot 1 still runs: it waits
        self.finish(RID[0], MON + "T10:06")
        self.assertTrue(self.claim(RID[1], k1, MON + "T10:07")["ok"])

    def test_a_failed_marker_on_slot_2_does_not_block_slot_3(self):
        self.assertTrue(self.claim(RID[1], K[1], NOWS[1])["ok"])
        self.finish(RID[1], NOWS[1], status="failed", report="boom\n")
        self.assertTrue(os.path.exists(self.p("failed-hb-" + K[1])))
        self.assertEqual(self.claim(RID[4], K[1], NOWS[1])["reason"], "failed_today")
        due = [d["ds"] for d in api(self.e, ["due", "--for", "session"], now=NOWS[2])[0]["due"]]
        self.assertEqual(due, [K[2]])                                                # the newest open slot only
        self.assertEqual(self.claim(RID[2], K[2], NOWS[2]), {"ok": True, "run_id": RID[2], "ds": K[2]})

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
            self.claim(RID[i], K[i], NOWS[i])
            self.finish(RID[i], NOWS[i])
        s = sched(self.e, "hb", now=NOWS[1])
        self.assertEqual(s["last_run"]["ds"], K[1])
        d, _ = api(self.e, ["detail", "hb"], now=NOWS[1])
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

    def test_due_lists_only_the_newest_open_slot_and_a_ran_slot_drops_out(self):
        r, lines = self.e.due(MON + "T12:30")
        self.assertEqual(lines, ["hb|" + K[1]], r.stderr)                          # slot 1 is open too (window 1440) but superseded
        self.e.touch(self.st, "ran-hb-" + K[1])
        r, lines = self.e.due(MON + "T12:30")
        self.assertEqual(lines, [])                                                  # the older slot does not come back when the newer one is done

    def test_the_grace_period_applies_to_each_slot(self):
        r, lines = self.e.due(MON + "T12:20")                      # 5 minutes after 12:15, grace is 10: slot 2 not yet, slot 1 is superseded
        self.assertEqual(lines, [])
        r, lines = self.e.due(MON + "T12:26")
        self.assertEqual(lines, ["hb|" + K[1]])

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

    def test_slot_markers_older_than_15_days_are_pruned_and_nothing_else(self):
        keep = ["ran-hb-2026-10-05T0945", "ran-hb-2026-10-05", "ran-other-2026-09-01", "claim-hb-2026-09-20T0945", "approved", "headless.blocked"]
        gone = ["ran-hb-2026-09-25T0945", "failed-hb-2026-09-25T1215", "stalled-hb-2026-09-25T1445", "waitnotified-hb-2026-09-25T1715"]
        os.makedirs(self.st, exist_ok=True)
        for n in keep + gone:
            self.e.touch(self.st, n)
        r = self.cli("prune", CHRONOS_NOW="2026-10-12T09:00")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(n for n in gone if os.path.exists(self.p(n))), [])
        self.assertEqual(sorted(n for n in keep if not os.path.exists(self.p(n))), [])

    def test_the_prune_edge(self):
        os.makedirs(self.st, exist_ok=True)
        for n in ("ran-hb-2026-10-01T0945", "ran-hb-2026-09-30T0945"):
            self.e.touch(self.st, n)
        self.cli("prune", CHRONOS_NOW="2026-10-16T09:00")                        # 15 days after 10-01 is 10-16: kept; 09-30 and older go
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
        self.e.wait_for(lambda: os.path.exists(self.p("ran-hb-" + K[1])))
        self.assertTrue(os.path.exists(self.p("reports", "hb-%s.md" % K[1])))
        self.assertFalse(os.path.exists(self.p("ran-hb-" + K[0])))                    # the older open slot was superseded, not run
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
        self.assertEqual(self.cli("claim", "morning", MON + "T0730", CHRONOS_NOW=MON + "T07:45").stdout.strip(), "NOT_DUE")
        self.assertFalse(os.path.exists(self.p("claim-morning-" + MON)))          # never the date key's claim under a slot key (the job was edited since `due` printed it)
        self.assertEqual(self.cli("claim", "morning", "garbage", CHRONOS_NOW=MON + "T07:45").stdout.strip(), "CLAIMED")      # an unrecognised ds still falls back, as before
        self.assertTrue(os.path.isdir(self.p("claim-morning-" + MON)))

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


NY = "America/New_York"


def hl(**kw):
    """A headless job with the given slots."""
    j = hb(in_session=False, headless=True)
    j.update(kw)
    if "times" in kw and "time" not in kw:
        j["time"] = kw["times"][0]
    return j


class CatchUpRunsOnlyTheNewestSlot(Base):
    jobs = [hl(times=["09:00", "09:15", "09:30", "09:45", "10:00", "10:15", "10:30"], catchup_min=180)]

    def test_a_mac_that_wakes_late_runs_one_slot_not_seven(self):
        r, lines = self.e.due(MON + "T11:00")
        self.assertEqual(lines, ["hb|" + MON + "T1030"], r.stderr)

    def test_an_older_open_slot_cannot_be_claimed(self):
        self.assertEqual(self.cli("claim", "hb", MON + "T0900", CHRONOS_NOW=MON + "T11:00").stdout.strip(), "NOT_DUE")
        self.assertEqual(self.cli("claim", "hb", MON + "T1030", CHRONOS_NOW=MON + "T11:00").stdout.strip(), "CLAIMED")

    def test_the_tick_starts_one_run(self):
        self.e.run(["/bin/bash", TICK], CHRONOS_NOW=MON + "T11:00", FAKE_MODE="ok")
        self.e.wait_for(lambda: os.path.exists(self.p("ran-hb-" + MON + "T1030")))
        self.assertEqual([n for n in os.listdir(self.st) if n.startswith("ran-hb-")], ["ran-hb-" + MON + "T1030"])

    def test_the_session_offer_and_the_api_claim_agree(self):
        self.e.set_jobs([hb(times=["09:00", "09:15", "09:30"], catchup_min=180)])
        out, _ = api(self.e, ["due", "--for", "session"], now=MON + "T10:00")
        self.assertEqual([d["ds"] for d in out["due"]], [MON + "T0930"])
        self.assertEqual(self.claim(RID[0], MON + "T0900", MON + "T10:00")["reason"], "window_passed")
        self.assertEqual(sched(self.e, "hb", now=MON + "T10:00")["ds"], MON + "T0930")

    def test_the_slot_that_was_skipped_reads_as_missed_not_waiting(self):
        sys.path.insert(0, os.path.join(ROOT, "lib"))
        j = self.e.cfg and C.load_jobs(self.e.cfg)[0]
        at = C.datetime.datetime(2026, 10, 5, 11, 0)
        d = at.date()
        self.assertEqual(C.slot_state(self.e.cfg, j, d, MON + "T0900", C.slots(j, d)[0][1], at), "missed")
        self.assertEqual(C.slot_state(self.e.cfg, j, d, MON + "T1030", C.slots(j, d)[-1][1], at), "waiting")


class OneRunOfAJobAtATime(Base):
    jobs = [hl(times=["09:45", "10:00"], catchup_min=30)]

    def test_a_slow_run_holds_back_the_next_slot(self):
        self.assertEqual(self.cli("claim", "hb", MON + "T0945", CHRONOS_NOW=MON + "T09:56").stdout.strip(), "CLAIMED")
        self.assertEqual(self.cli("claim", "hb", MON + "T1000", CHRONOS_NOW=MON + "T10:10").stdout.strip(), "RUNNING_ELSEWHERE")
        r, lines = self.e.due(MON + "T10:10")
        self.assertEqual(lines, [])
        self.cli("done", "hb", MON + "T0945", CHRONOS_NOW=MON + "T10:12")
        r, lines = self.e.due(MON + "T10:12")
        self.assertEqual(lines, ["hb|" + MON + "T1000"])

    def test_done_with_no_key_closes_the_slot_that_is_running(self):
        self.cli("claim", "hb", MON + "T0945", CHRONOS_NOW=MON + "T09:56")
        r = self.cli("done", "hb", CHRONOS_NOW=MON + "T10:05")           # slot 2 has started, slot 1 is the one running
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(self.p("ran-hb-" + MON + "T0945")))
        self.assertFalse(os.path.exists(self.p("ran-hb-" + MON + "T1000")))
        self.assertFalse(os.path.exists(self.p("claim-hb-" + MON + "T0945")))


class RunNowIsNeverASlot(Base):
    jobs = [hl(times=["09:45", "12:15"], catchup_min=60)]

    def test_run_now_in_a_slots_own_minute_gets_another_key(self):
        sys.path.insert(0, os.path.join(ROOT, "lib"))
        j = C.load_jobs(self.e.cfg)[0]
        for hh, mm, want in ((9, 45, "T0946"), (12, 15, "T1216"), (10, 30, "T1030")):
            self.assertEqual(C.run_now_key(j, C.datetime.datetime(2026, 10, 5, hh, mm, 30)), MON + want)

    def test_a_manual_claim_at_09_45_leaves_the_slot_to_run(self):
        out = self.claim(RID[0], None, MON + "T09:45", manual=True)
        self.assertEqual(out["ds"], MON + "T0946")
        self.finish(RID[0], MON + "T09:50")
        self.assertFalse(os.path.exists(self.p("ran-hb-" + MON + "T0945")))
        r, lines = self.e.due(MON + "T09:56")
        self.assertEqual(lines, ["hb|" + MON + "T0945"])


class ARunNowIsVisibleAndExclusive(Base):
    jobs = [hl(times=["09:45", "12:15"], catchup_min=60)]

    def test_snapshot_shows_running_and_a_second_run_now_is_refused(self):
        self.assertTrue(self.claim(RID[0], None, MON + "T10:30", manual=True)["ok"])
        s = sched(self.e, "hb", now=MON + "T10:31")
        self.assertEqual((s["today"], s["ds"]), ("running", MON + "T1030"))
        self.assertEqual(self.claim(RID[1], None, MON + "T10:31", manual=True)["reason"], "running_elsewhere")

    def test_a_slot_waits_while_a_run_now_is_going(self):
        self.claim(RID[0], None, MON + "T12:10", manual=True)
        self.assertEqual(self.claim(RID[1], MON + "T1215", MON + "T12:20")["reason"], "running_elsewhere")
        r, lines = self.e.due(MON + "T12:26", TZ=NY)
        self.assertEqual(lines, [])

    def test_run_now_is_refused_while_a_slot_is_running(self):
        self.assertTrue(self.claim(RID[0], MON + "T0945", MON + "T09:56")["ok"])
        self.assertEqual(self.claim(RID[1], None, MON + "T10:00", manual=True)["reason"], "running_elsewhere")

    def test_the_ui_listing_says_running(self):
        import datetime as _dt
        self.claim(RID[0], None, _dt.datetime.now().strftime("%Y-%m-%dT%H:%M"), manual=True, tz=None)       # the UI reads the real clock
        ui = UIServer(self.e)
        try:
            st, jobs = ui.req("GET", "/api/jobs")
            self.assertEqual(st, 200)
            self.assertTrue(next(x for x in jobs["jobs"] if x["id"] == "hb")["running"])
        finally:
            ui.stop()


class RunNowClearsAStalledSlot(Base):
    jobs = [hl(times=["09:45", "12:15"], catchup_min=60)]

    def test_a_successful_manual_start_clears_launch_stalled(self):
        self.e.touch(self.st, "stalled-hb-" + MON + "T0945")
        codes = lambda now: [i["code"] for i in api(self.e, ["health"], now=now)[0]["issues"] if i["schedule_id"] == "hb"]
        self.assertIn("launch_stalled", codes(MON + "T10:20"))
        self.claim(RID[0], None, MON + "T10:30", manual=True)
        self.assertTrue(os.path.exists(self.p("stalled-hb-" + MON + "T0945")))      # not at claim time: it clears once the run has finished OK
        self.finish(RID[0], MON + "T10:35")
        self.assertFalse(os.path.exists(self.p("stalled-hb-" + MON + "T0945")))
        self.assertNotIn("launch_stalled", codes(MON + "T10:40"))


class OneBadRecordDoesNotTakeTheListDown(Base):
    def test_health_and_the_ui_list_survive_each_kind_of_bad_times(self):
        for bad in (["09:45", "09:50"], [], "09:45", ["9am"]):
            self.e.set_jobs([hl(id="good", times=["09:45", "12:15"]), hl(id="bad", times=bad, time="09:45")])
            out, r = api(self.e, ["health"], now=MON + "T10:00")
            self.assertEqual(r.returncode, 0)
            ui = UIServer(self.e)
            try:
                st, jobs = ui.req("GET", "/api/jobs")
                self.assertEqual(st, 200, bad)
                ids = [x["id"] for x in jobs["jobs"]]
                self.assertEqual(sorted(ids), ["bad", "good"])
                self.assertTrue(next(x for x in jobs["jobs"] if x["id"] == "good").get("schedule_ok"))
            finally:
                ui.stop()


class SwitchingBetweenTimeAndTimes(Base):
    jobs = [job("cv", time="09:45", days="daily", catchup_min=180)]

    def test_adding_times_after_the_plain_run_does_not_run_the_first_slot_again(self):
        self.e.touch(self.st, "ran-cv-" + MON)
        self.e.set_jobs([job("cv", time="09:45", times=["09:45", "14:00"], days="daily", catchup_min=180)])
        r, lines = self.e.due(MON + "T10:00")
        self.assertEqual(lines, [])
        self.assertEqual(self.cli("claim", "cv", MON + "T0945", CHRONOS_NOW=MON + "T10:00").stdout.strip(), "ALREADY_DONE")
        r, lines = self.e.due(MON + "T14:11")
        self.assertEqual(lines, ["cv|" + MON + "T1400"])

    def test_going_back_to_plain_after_a_slot_ran_does_not_run_the_day_again(self):
        self.e.touch(self.st, "ran-cv-" + MON + "T0945")
        r, lines = self.e.due(MON + "T10:00")
        self.assertEqual(lines, [])
        self.assertEqual(self.cli("claim", "cv", CHRONOS_NOW=MON + "T10:00").stdout.strip(), "ALREADY_DONE")

    def test_a_plain_job_with_no_slot_marker_still_runs(self):
        r, lines = self.e.due(MON + "T10:00")
        self.assertEqual(lines, ["cv|" + MON])


class SpringForwardGap(Base):
    jobs = [hl(times=["01:45", "02:15", "02:45", "03:00"], days="daily", catchup_min=120)]

    def test_slots_folded_onto_one_instant_start_once(self):
        r, lines = self.e.due("2026-03-08T03:11", TZ=NY)
        self.assertEqual(lines, ["hb|2026-03-08T0215"], r.stderr)


class HeldAndWeekendKeys(Base):
    cfg = {"session_runner": "talos", "approval_required": True}
    jobs = [hb(in_session=False, headless=True, times=["09:45", "12:15"], catchup_min=180)]

    def test_a_held_job_reports_today_not_yesterdays_slot(self):
        s = sched(self.e, "hb", now="2026-10-06T08:00")
        self.assertEqual((s["today"], s["ds"]), ("held", "2026-10-06T0945"))

    def test_a_weekend_is_not_due_and_not_fridays_result(self):
        self.e.touch(self.st, "ran-hb-2026-10-09T0945")
        self.e.touch(self.st, "ran-hb-2026-10-09T1215")
        c = json.load(open(self.e.cfg_path))
        c["approval_required"] = False
        json.dump(c, open(self.e.cfg_path, "w"))
        s = sched(self.e, "hb", now="2026-10-10T20:00")
        self.assertEqual((s["today"], s["ds"]), ("not_due", "2026-10-10"))


class CliClaimRefusesNonSlots(Base):
    jobs = [hl()]

    def test_a_key_that_is_not_a_slot_is_not_due_without_unattended(self):
        for k in (MON + "T0946", MON, MON + "T0300"):
            self.assertEqual(self.cli("claim", "hb", k, CHRONOS_NOW=MON + "T10:00").stdout.strip(), "NOT_DUE", k)
        self.assertEqual([n for n in os.listdir(self.st) if n.startswith("claim-")] if os.path.isdir(self.st) else [], [])


class MidnightGap(unittest.TestCase):
    def test_the_gap_across_midnight_is_checked_when_days_can_follow_each_other(self):
        sys.path.insert(0, os.path.join(ROOT, "lib"))
        bad = ["00:05", "23:55"]
        for days in ("daily", "weekdays", "mon,tue", "sun,mon"):
            _c, errs = C.validate_schedule({"times": bad, "days": days, "catchup_min": 60})
            self.assertTrue(errs, days)
        for days in ("sat", "sat,mon", "mon,wed,fri"):
            _c, errs = C.validate_schedule({"times": bad, "days": days, "catchup_min": 60})
            self.assertEqual(errs, [], days)
        _c, errs = C.validate_schedule({"times": ["00:05", "23:50"], "days": "daily", "catchup_min": 60})
        self.assertEqual(errs, [])                                                    # exactly 15 minutes
        with self.assertRaises(ValueError):
            C.validate_job_record({"id": "wr", "times": bad, "days": "daily"})

    def test_days_of_the_month_with_times_is_refused_in_a_record(self):
        with self.assertRaises(ValueError):
            C.validate_job_record({"id": "dm", "times": ["09:00", "10:00"], "days": "dom:5"})


class MetaIsPlainJson(unittest.TestCase):
    def test_nan_and_infinity_are_refused(self):
        for bad in (float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                C.check_meta({"x": bad})
        self.assertEqual(C.check_meta({"x": 1.5}), {"x": 1.5})


class ABadMetaDoesNotBreakTheSnapshot(Base):
    def test_a_hand_edited_nan_gives_that_job_an_error_row_and_the_output_stays_strict_json(self):
        raw = json.dumps([hl(id="good"), hl(id="nanny")]).replace('"id": "nanny"', '"id": "nanny", "meta": {"x": NaN}')
        open(self.e.cfg["jobs_file"], "w").write(raw)
        out, r = api(self.e, ["snapshot"], now=MON + "T10:00")
        json.loads(r.stdout, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
        rows = {x["id"]: x for x in out["schedules"]}
        self.assertIn("error", rows["nanny"])
        self.assertNotIn("error", rows["good"])


class SessionTimesJobsNeedTheTalosRunner(Base):
    jobs = [hb()]

    def test_switching_to_the_hook_runner_is_refused_while_a_times_job_runs_in_a_session(self):
        out, r = api(self.e, ["config", "set"], {"session_runner": "hook"}, expect=1)
        e = err(r)
        self.assertEqual(e["code"], "schedule_invalid")
        self.assertIn("hb", e["message"])
        self.assertEqual(json.load(open(self.e.cfg_path)).get("session_runner"), "talos")

    def test_it_is_fine_for_a_headless_times_job(self):
        self.e.set_jobs([hl()])
        out, r = api(self.e, ["config", "set"], {"session_runner": "hook"})
        self.assertEqual(r.returncode, 0)


class PruneKeepsTheUiStrip(unittest.TestCase):
    def test_prune_reaches_further_back_than_the_14_day_strip(self):
        m = __import__("re").search(r"^STRIP_DAYS = (\d+)", open(os.path.join(ROOT, "ui", "server.py")).read(), __import__("re").M)
        self.assertIsNotNone(m)
        self.assertGreater(C.PRUNE_DAYS, int(m.group(1)))


class TheUiCanRevertATimesJob(Base):
    jobs = [hl(name="Multi")]

    def setUp(self):
        Base.setUp(self)
        self.ui = UIServer(self.e)

    def tearDown(self):
        self.ui.stop()
        Base.tearDown(self)

    def rec(self):
        return json.load(open(self.e.cfg["jobs_file"]))[0]

    def test_times_null_turns_the_job_back_into_a_plain_one(self):
        st, r = self.ui.req("POST", "/api/jobs/hb/schedule", {"times": None, "time": "08:30", "days": "weekdays", "catchup_min": 120})
        self.assertEqual(st, 200, r)
        self.assertNotIn("times", self.rec())
        self.assertEqual(self.rec()["time"], "08:30")

    def test_times_null_without_a_time_keeps_the_first_time(self):
        st, r = self.ui.req("POST", "/api/jobs/hb/schedule", {"times": None, "days": "weekdays", "catchup_min": 120})
        self.assertEqual(st, 200, r)
        self.assertEqual((self.rec().get("times"), self.rec()["time"]), (None, "09:45"))

    def test_restoring_a_snapshot_from_before_times_gives_the_plain_job_back(self):
        self.e.set_jobs([job("pl", time="07:30", days="weekdays")])
        st, r = self.ui.req("POST", "/api/jobs/pl/schedule", {"times": ["07:30", "11:00"], "days": "weekdays", "catchup_min": 180})
        self.assertEqual(st, 200, r)
        st, d = self.ui.req("GET", "/api/jobs/pl")
        files = [x["file"] for x in d["history"]["jobs"]]
        self.assertTrue(files, d["history"])
        st, r = self.ui.req("POST", "/api/jobs/pl/restore", {"kind": "schedule", "file": files[-1]})
        self.assertEqual(st, 200, r)
        rec = next(j for j in json.load(open(self.e.cfg["jobs_file"])) if j["id"] == "pl")
        self.assertNotIn("times", rec)
        self.assertEqual(rec["time"], "07:30")


if __name__ == "__main__":
    unittest.main(verbosity=1)
