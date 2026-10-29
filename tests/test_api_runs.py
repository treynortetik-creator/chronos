#!/usr/bin/env python3
"""WP-C2: the run verbs. `claim` (with the sibling owner file), `finish` (owner check, `late`), `orphans`, per-run reports and run ids.
Every case goes through the real `chronos api` CLI against a throwaway home with a fixed clock."""
import os
import sys
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_helpers import *  # noqa: E402,F401,F403

NOW = "2026-10-05T07:45"           # a Monday; the job fires 07:30, window 180 min
DS = "2026-10-05"
RID1 = "r_" + "0" * 25 + "1"
RID2 = "r_" + "0" * 25 + "2"
RID3 = "r_" + "0" * 25 + "3"


def claim_req(rid=RID1, ds=DS, **kw):
    r = {"run_id": rid, "ds": ds, "fire_at": ds + "T07:30:00-04:00", "manual": False, "again": False, "runner": "session", "bridge_instance": "inst-a"}
    r.update(kw)
    return r


class Base(unittest.TestCase):
    cfg = {}

    def setUp(self):
        self.e = Env([job("morning", time="07:30", in_session=True, headless=False)], **self.cfg)
        self.st = self.e.cfg["state_dir"]

    def tearDown(self):
        self.e.cleanup()

    def claim(self, req=None, now=NOW, expect=0, jid="morning"):
        return api(self.e, ["claim", jid], req if req is not None else claim_req(), now=now, expect=expect)[0]

    def finish(self, rid=RID1, status="ok", report="All good\n", now=NOW, expect=0, jid="morning"):
        return api(self.e, ["finish", jid], {"run_id": rid, "status": status, "report": report}, now=now, expect=expect)[0]

    def p(self, *parts):
        return os.path.join(self.st, *parts)

    def rows(self):
        p = self.e.cfg["runs_file"]
        return [json.loads(l) for l in open(p)] if os.path.exists(p) else []


class Claims(Base):
    def test_two_claims_give_ok_then_running_elsewhere(self):
        self.assertEqual(self.claim(), {"ok": True, "run_id": RID1, "ds": DS})
        self.assertEqual(self.claim(claim_req(RID2)), {"ok": False, "reason": "running_elsewhere"})
        self.assertTrue(os.path.isdir(self.p("claim-morning-" + DS)))

    def test_the_owner_file_is_a_sibling_written_in_the_same_call(self):
        self.claim()
        own = json.load(open(self.p("claim-morning-%s.owner.json" % DS)))
        self.assertEqual({k: own[k] for k in ("run_id", "ds", "runner", "bridge_instance", "fire_at", "manual")},
                         {"run_id": RID1, "ds": DS, "runner": "session", "bridge_instance": "inst-a", "fire_at": DS + "T07:30:00-04:00", "manual": False})
        self.assertEqual(own["claimed_at"], DS + "T07:45:00-04:00")
        self.assertEqual([n for n in os.listdir(self.st) if ".tmp" in n], [])           # written by tmp + rename

    def test_a_dead_claim_is_taken_again_after_stale_claim_min_and_the_sibling_file_is_swept(self):
        self.claim()                                                                    # then the process "dies": nothing finishes it
        self.assertEqual(self.claim(claim_req(RID2), now="2026-10-05T08:30")["reason"], "running_elsewhere")      # 45 min: not stale yet
        out = self.claim(claim_req(RID2, bridge_instance="inst-b"), now="2026-10-05T08:31")                      # 46 min
        self.assertEqual(out, {"ok": True, "run_id": RID2, "ds": DS})
        own = json.load(open(self.p("claim-morning-%s.owner.json" % DS)))
        self.assertEqual((own["run_id"], own["bridge_instance"]), (RID2, "inst-b"))

    def test_the_stale_sweep_removes_both_files(self):
        self.claim()
        c = C.load_config()
        C.sweep_stale_claim(c, C.claim_path(c, "morning", DS), at=C.datetime.datetime(2026, 10, 5, 9, 0))
        self.assertFalse(os.path.exists(self.p("claim-morning-" + DS)))
        self.assertFalse(os.path.exists(self.p("claim-morning-%s.owner.json" % DS)))

    def test_every_refusal_is_a_reason_from_the_claim_result_union(self):
        self.assertEqual(self.claim(now="2026-10-05T07:00")["reason"], "not_due")                         # before the fire
        self.assertEqual(self.claim(claim_req(ds="2026-10-04"))["reason"], "not_due")                     # a Sunday
        self.assertEqual(self.claim(now="2026-10-05T11:00")["reason"], "window_passed")
        self.e.touch(self.e.cfg["paused_dir"], "morning")
        self.assertEqual(self.claim()["reason"], "paused")
        os.remove(os.path.join(self.e.cfg["paused_dir"], "morning"))
        self.e.touch(self.st, "failed-morning-" + DS)
        self.assertEqual(self.claim()["reason"], "failed_today")
        os.remove(self.p("failed-morning-" + DS))
        self.e.touch(self.st, "stalled-morning-" + DS)
        self.assertEqual(self.claim()["reason"], "failed_today")
        os.remove(self.p("stalled-morning-" + DS))
        self.e.touch(self.st, "ran-morning-" + DS)
        self.assertEqual(self.claim()["reason"], "already_done")
        self.assertEqual(self.claim(claim_req(manual=True))["reason"], "already_done")                    # Run now without `again`

    def test_a_held_job_cannot_be_claimed(self):
        e = Env([job("morning", time="07:30", in_session=True, headless=False)], approval_required=True)
        try:
            self.assertEqual(api(e, ["claim", "morning"], claim_req(), now=NOW)[0], {"ok": False, "reason": "held"})
            self.assertFalse(os.path.exists(os.path.join(e.cfg["state_dir"], "claim-morning-" + DS)))
        finally:
            e.cleanup()

    def test_run_now_claims_outside_the_window_and_clears_failed_stalled_and_with_again_ran(self):
        for m in ("failed", "stalled"):
            self.e.touch(self.st, "%s-morning-%s" % (m, DS))
        out = self.claim(claim_req(manual=True, fire_at=None), now="2026-10-05T13:00")                   # past the window: manual skips it
        self.assertTrue(out["ok"], out)
        self.assertEqual([m for m in ("failed", "stalled") if os.path.exists(self.p("%s-morning-%s" % (m, DS)))], [])
        self.assertEqual(self.claim(claim_req(RID2, manual=True), now="2026-10-05T13:00")["reason"], "running_elsewhere")

    def test_run_now_with_again_removes_the_ran_marker_and_overwrites_a_leftover_owner_file_cleanly(self):
        self.e.touch(self.st, "ran-morning-" + DS)
        with open(self.p("claim-morning-%s.owner.json" % DS), "w") as fh:                                # a leftover from a crashed run
            fh.write('{"run_id": "r_LEFTOVER"}')
        out = self.claim(claim_req(manual=True, again=True))
        self.assertTrue(out["ok"], out)
        self.assertFalse(os.path.exists(self.p("ran-morning-" + DS)))
        self.assertEqual(json.load(open(self.p("claim-morning-%s.owner.json" % DS)))["run_id"], RID1)

    def test_manual_claim_defaults_ds_to_the_jobs_own_date(self):
        self.assertEqual(self.claim({"run_id": RID1, "manual": True, "runner": "session", "bridge_instance": "x"})["ds"], DS)

    def test_bad_input_is_a_bad_request(self):
        r = api(self.e, ["claim", "morning"], claim_req("not-a-run-id"), now=NOW, expect=1)[1]
        self.assertEqual(err(r)["code"], "bad_request")
        r = api(self.e, ["claim", "morning"], claim_req(ds="05/10/2026"), now=NOW, expect=1)[1]
        self.assertEqual(err(r)["code"], "bad_request")
        r = api(self.e, ["claim", "../x"], claim_req(), now=NOW, expect=1)[1]
        self.assertEqual(err(r)["code"], "bad_request")
        r = api(self.e, ["claim", "nope"], claim_req(), now=NOW, expect=1)[1]
        self.assertEqual(err(r)["code"], "not_found")

    def test_a_restricted_or_command_job_is_never_claimed_by_a_session(self):
        self.e.set_jobs([job("rest", time="07:30", restricted=True, allowed_tools=["Read"]), job("cmd", time="07:30", kind="command", command="true")])
        for jid in ("rest", "cmd"):
            r = api(self.e, ["claim", jid], claim_req(), now=NOW, expect=1)[1]
            self.assertEqual(err(r)["code"], "schedule_invalid", jid)

    def test_snapshot_shows_the_claim_as_running_and_state_rev_moves(self):
        before = sched(self.e, "morning", now=NOW)
        self.claim()
        after = sched(self.e, "morning", now=NOW)
        self.assertEqual((before["today"], after["today"]), ("waiting", "running"))
        self.assertNotEqual(before["state_rev"], after["state_rev"])
        self.finish()
        done = sched(self.e, "morning", now=NOW)
        self.assertEqual(done["today"], "ran")
        self.assertEqual((done["last_run"]["run_id"], done["last_run"]["status"], done["last_run"]["has_report"]), (RID1, "ok", True))


class Finish(Base):
    def test_ok_writes_the_marker_the_per_run_report_the_old_path_copy_and_releases_both_claim_files(self):
        self.claim()
        self.assertEqual(self.finish(), {"ok": True})
        self.assertTrue(os.path.exists(self.p("ran-morning-" + DS)))
        self.assertEqual(open(self.p("reports", "morning-%s-%s.md" % (DS, RID1))).read(), "All good\n")
        self.assertEqual(open(self.p("reports", "morning-%s.md" % DS)).read(), "All good\n")            # the old per-day path: a copy of the latest
        self.assertFalse(os.path.exists(self.p("claim-morning-" + DS)))
        self.assertFalse(os.path.exists(self.p("claim-morning-%s.owner.json" % DS)))
        row = self.rows()[-1]
        self.assertEqual((row["job"], row["run_id"], row["ds"], row["runner"], row["status"], row["ok"]), ("morning", RID1, DS, "session", "ok", True))

    def test_failed_and_timed_out_and_session_ended_write_the_failed_marker_never_ran(self):
        for i, status in enumerate(("failed", "timed_out", "session_ended")):
            os.remove(self.p("failed-morning-" + DS)) if os.path.exists(self.p("failed-morning-" + DS)) else None
            rid = "r_" + "0" * 24 + "1%d" % i
            self.assertTrue(self.claim(claim_req(rid))["ok"])
            self.assertEqual(self.finish(rid, status), {"ok": True})
            self.assertTrue(os.path.exists(self.p("failed-morning-" + DS)), status)
            self.assertFalse(os.path.exists(self.p("ran-morning-" + DS)), status)
            self.assertEqual(self.rows()[-1]["status"], status)

    def test_finish_from_a_different_run_id_is_not_owner_and_changes_nothing(self):
        self.claim()
        self.assertEqual(self.finish(RID2), {"ok": False, "reason": "not_owner"})
        self.assertTrue(os.path.isdir(self.p("claim-morning-" + DS)))
        self.assertTrue(os.path.exists(self.p("claim-morning-%s.owner.json" % DS)))
        self.assertFalse(os.path.exists(self.p("ran-morning-" + DS)))
        self.assertEqual(self.finish(RID1)["ok"], True)                                                  # the real owner still can

    def test_finish_with_no_claim_at_all_for_an_unknown_run_is_not_owner(self):
        self.assertEqual(self.finish(RID3), {"ok": False, "reason": "not_owner"})

    def test_a_finish_after_session_ended_is_late_and_leaves_the_status_as_it_was(self):
        self.claim()
        self.assertEqual(self.finish(RID1, "session_ended"), {"ok": True})
        self.assertEqual(self.finish(RID1, "ok", "I did finish after all\n"), {"ok": False, "reason": "late"})
        self.assertTrue(os.path.exists(self.p("failed-morning-" + DS)))
        self.assertFalse(os.path.exists(self.p("ran-morning-" + DS)))
        self.assertEqual([r["status"] for r in self.rows() if r.get("run_id") == RID1], ["session_ended"])
        self.assertIn("I did finish after all", open(self.p("reports", "morning-%s-%s.late.md" % (DS, RID1))).read())      # kept as the late note

    def test_run_id_is_only_a_lookup_key(self):
        self.claim()
        r = api(self.e, ["finish", "morning"], {"run_id": "../../etc/passwd", "status": "ok", "report": "x"}, now=NOW, expect=1)[1]
        self.assertEqual(err(r)["code"], "bad_request")
        r = api(self.e, ["finish", "morning"], {"run_id": RID1, "status": "weird", "report": "x"}, now=NOW, expect=1)[1]
        self.assertEqual(err(r)["code"], "bad_request")
        self.assertEqual(os.listdir(self.p("reports")) if os.path.isdir(self.p("reports")) else [], [])

    def test_finish_is_keyed_on_the_claims_ds_not_todays_date(self):
        e = Env([job("night", time="23:50", days="daily", in_session=True, headless=False, catchup_min=180)])
        try:
            now = "2026-10-06T00:05"
            self.assertTrue(api(e, ["claim", "night"], claim_req(ds="2026-10-05", fire_at="2026-10-05T23:50:00-04:00"), now=now)[0]["ok"])
            self.assertEqual(api(e, ["finish", "night"], {"run_id": RID1, "status": "ok", "report": "r\n"}, now=now)[0], {"ok": True})
            names = os.listdir(e.cfg["state_dir"])
            self.assertIn("ran-night-2026-10-05", names)
            self.assertNotIn("ran-night-2026-10-06", names)
            self.assertIn("night-2026-10-05-%s.md" % RID1, os.listdir(os.path.join(e.cfg["state_dir"], "reports")))
        finally:
            e.cleanup()


class Orphans(Base):
    def test_a_claim_from_instance_a_is_listed_for_instance_b_and_not_for_a(self):
        self.claim()
        out, _ = api(self.e, ["orphans", "--bridge-instance", "inst-b"], now=NOW)
        self.assertEqual(out, {"orphans": [{"id": "morning", "ds": DS, "run_id": RID1}]})
        self.assertEqual(api(self.e, ["orphans", "--bridge-instance", "inst-a"], now=NOW)[0], {"orphans": []})

    def test_finishing_an_orphan_as_session_ended_writes_failed_and_nothing_re_runs(self):
        self.claim()
        self.assertEqual(self.finish(RID1, "session_ended", ""), {"ok": True})
        self.assertTrue(os.path.exists(self.p("failed-morning-" + DS)))
        self.assertEqual(api(self.e, ["orphans", "--bridge-instance", "inst-b"], now=NOW)[0], {"orphans": []})
        out, _ = api(self.e, ["due", "--for", "session"], now=NOW)
        self.assertEqual(out["due"], [])

    def test_a_headless_claim_is_never_an_orphan(self):
        self.claim(claim_req(runner="headless", bridge_instance=""))
        self.assertEqual(api(self.e, ["orphans", "--bridge-instance", "inst-b"], now=NOW)[0], {"orphans": []})

    def test_orphans_requires_the_instance(self):
        r = api(self.e, ["orphans"], now=NOW, expect=1)[1]
        self.assertEqual(err(r)["code"], "bad_request")


if __name__ == "__main__":
    unittest.main()
