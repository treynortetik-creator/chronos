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

    def test_finish_never_closes_a_headless_run(self):
        self.claim(claim_req(runner="headless", bridge_instance=""))
        self.assertEqual(self.finish(), {"ok": False, "reason": "not_owner"})
        self.assertTrue(os.path.isdir(self.p("claim-morning-" + DS)))

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


class SessionPrompt(Base):
    def test_returns_the_guard_the_prompt_and_the_content_hash(self):
        out, _ = api(self.e, ["session-prompt", "morning"], now=NOW)
        self.assertEqual(out, {"guard": None, "prompt": "Say hello. TASK-BODY-morning\n", "content_hash": expected_content_hash(self.e, "morning")})
        with open(os.path.join(self.e.cfg["jobs_dir"], "morning", "guard.md"), "w") as fh:
            fh.write("Never delete anything.\n")
        out, _ = api(self.e, ["session-prompt", "morning"], now=NOW)
        self.assertEqual(out["guard"], "Never delete anything.\n")
        self.assertEqual(out["content_hash"], expected_content_hash(self.e, "morning"))

    def test_a_held_job_is_refused_with_held(self):
        e = Env([job("morning", time="07:30", in_session=True, headless=False)], approval_required=True)
        try:
            self.assertEqual(err(api(e, ["session-prompt", "morning"], now=NOW, expect=1)[1])["code"], "held")
        finally:
            e.cleanup()

    def test_restricted_command_unknown_and_bad_ids(self):
        self.e.set_jobs([job("rest", restricted=True, allowed_tools=["Read"]), job("cmd", kind="command", command="true")])
        self.assertEqual(err(api(self.e, ["session-prompt", "rest"], now=NOW, expect=1)[1])["code"], "schedule_invalid")
        self.assertEqual(err(api(self.e, ["session-prompt", "cmd"], now=NOW, expect=1)[1])["code"], "schedule_invalid")
        self.assertEqual(err(api(self.e, ["session-prompt", "nope"], now=NOW, expect=1)[1])["code"], "not_found")
        self.assertEqual(err(api(self.e, ["session-prompt", "../x"], now=NOW, expect=1)[1])["code"], "bad_request")


class StartHeadless(unittest.TestCase):
    def setUp(self):
        self.e = Env([job("bg", time="07:30"), job("sess", time="07:30", in_session=True, headless=False)])
        self.st = self.e.cfg["state_dir"]

    def tearDown(self):
        self.e.cleanup()

    def start(self, jid="bg", payload=None, now=NOW, **kw):
        return api(self.e, ["start", jid], payload if payload is not None else {"again": False}, now=now, **kw)[0]

    def test_start_runs_the_job_with_a_run_id_a_row_and_a_per_run_report(self):
        out = self.start()
        self.assertTrue(out["ok"], out)
        rid = out["run_id"]
        self.assertRegex(rid, r"^r_[0-9A-HJKMNP-TV-Z]{26}$")
        self.assertEqual(out["ds"], DS)
        self.assertTrue(self.e.wait_for(lambda: os.path.exists(os.path.join(self.st, "ran-bg-" + DS)) and not os.path.isdir(os.path.join(self.st, "claim-bg-" + DS))), "run did not finish")
        self.assertTrue(self.e.wait_for(lambda: os.path.exists(self.e.cfg["runs_file"])))
        row = [json.loads(l) for l in open(self.e.cfg["runs_file"])][-1]
        self.assertEqual((row["job"], row["run_id"], row["ds"], row["runner"], row["status"], row["trigger_kind"]), ("bg", rid, DS, "headless", "ok", "run_now"))
        self.assertTrue(os.path.realpath(row["claude_path"]).endswith("fake-claude.sh"), row["claude_path"])
        self.assertTrue(os.path.exists(os.path.join(self.st, "reports", "bg-%s-%s.md" % (DS, rid))))
        self.assertTrue(os.path.exists(os.path.join(self.st, "reports", "bg-%s.md" % DS)))
        self.assertFalse(os.path.exists(os.path.join(self.st, "claim-bg-%s.owner.json" % DS)))
        self.assertEqual(sched(self.e, "bg", now=NOW)["last_run"]["run_id"], rid)

    def test_refusals_use_the_start_codes(self):
        os.makedirs(os.path.join(self.st, "claim-bg-" + DS))
        self.assertEqual(self.start()["code"], "already_running")
        os.rmdir(os.path.join(self.st, "claim-bg-" + DS))
        self.assertEqual(self.start("sess")["code"], "schedule_invalid")                                    # a session-only job has no background run
        self.e.touch(self.st, "ran-bg-" + DS)
        out = self.start()
        self.assertEqual((out["ok"], out["code"]), (False, "schedule_invalid"))                              # already ran: needs `again`
        open(self.e.cfg["blocked_file"], "w").write('{"reason": "launch_stalled"}')
        self.assertEqual(self.start()["code"], "headless_blocked")
        self.assertFalse(os.path.isdir(os.path.join(self.st, "claim-bg-" + DS)))

    def test_a_held_job_is_not_started(self):
        e = Env([job("bg", time="07:30")], approval_required=True)
        try:
            self.assertEqual(api(e, ["start", "bg"], {"again": False}, now=NOW)[0]["code"], "held")
        finally:
            e.cleanup()


class Health(Base):
    cfg = {}

    def health(self, now=NOW, **kw):
        return api(self.e, ["health"], now=now, **kw)[0]

    def codes(self, h):
        return sorted(i["code"] for i in h["issues"])

    def test_a_quiet_install_has_no_issues_and_reports_the_raw_signals(self):
        self.e.set_jobs([job("bg", time="07:30")])
        h = self.health()
        self.assertEqual((h["issues"], h["headless"], h["paused_all"], h["tz"], h["session_runner"], h["tcc_suspected"]), ([], "ok", False, NY, "hook", False))
        self.assertIsNone(h["tick_age_s"])
        self.assertRegex(h["as_of"], RFC3339)
        self.e.touch(self.e.cfg["home_dir"], "chronos.beat")
        open(self.e.cfg["beat_file"], "w").write("2026-10-05T07:40:00\n")
        self.assertEqual(self.health()["tick_age_s"], 300)

    def test_headless_is_unused_when_no_job_runs_in_the_background(self):
        self.assertEqual(self.health()["headless"], "unused")           # the only job is session-only

    def test_paused_held_both_and_missed(self):
        self.e.touch(self.e.cfg["pause_all"])
        self.e.set_jobs([job("both", time="07:30", in_session=True), job("gone", time="05:00")])
        h = self.health(now="2026-10-05T09:00")
        self.assertEqual(self.codes(h), ["legacy_both_runner", "missed_recently", "paused_all"])
        self.assertEqual([i["schedule_id"] for i in h["issues"] if i["code"] == "legacy_both_runner"], ["both"])
        self.assertEqual([i["schedule_id"] for i in h["issues"] if i["code"] == "missed_recently"], ["gone"])      # 05:00 + 180 min = 08:00
        e = Env([job("hh", time="07:30")], approval_required=True)
        try:
            self.assertEqual(self.codes(api(e, ["health"], now=NOW)[0]), ["held_jobs"])
        finally:
            e.cleanup()

    def test_a_job_created_after_yesterdays_window_did_not_miss_it(self):
        self.e.set_jobs([job("fresh", time="23:50", days="daily", created="2026-10-05T08:00:00", catchup_min=60)])
        self.assertEqual(self.health(now="2026-10-06T00:30")["issues"], [])

    def test_a_blocked_headless_with_the_workspace_under_a_protected_folder_is_tcc_suspected(self):
        home = self.e.tmp
        ws = os.path.join(home, "Desktop", "agent")
        e = Env([job("bg", time="07:30")], workspace=ws)
        try:
            open(e.cfg["blocked_file"], "w").write(json.dumps({"reason": "launch_stalled", "id": "bg", "ds": DS, "run_id": RID1}))
            h = api(e, ["health"], now=NOW, HOME=home)[0]
            self.assertEqual(self.codes(h), ["headless_blocked", "launch_stalled", "tcc_suspected"])
            self.assertEqual((h["headless"], h["tcc_suspected"]), ("blocked", True))
            h = api(e, ["health"], now=NOW, HOME=os.path.join(home, "elsewhere"))[0]                      # same block, no protected path
            self.assertEqual(self.codes(h), ["headless_blocked", "launch_stalled"])
            self.assertFalse(h["tcc_suspected"])
        finally:
            e.cleanup()

    def test_a_changed_claude_path_since_the_last_good_headless_run_is_tcc_suspected(self):
        e = Env([job("bg", time="07:30")])
        try:
            open(e.cfg["blocked_file"], "w").write(json.dumps({"reason": "launch_stalled"}))
            with open(e.cfg["runs_file"], "w") as fh:
                fh.write(json.dumps({"job": "bg", "run_id": RID1, "ds": DS, "runner": "headless", "status": "ok", "ok": True, "claude_path": "/old/versions/1.0/claude", "start_ts": 1}) + "\n")
            self.assertTrue(api(e, ["health"], now=NOW)[0]["tcc_suspected"])
        finally:
            e.cleanup()

    def test_a_stalled_day_is_reported_and_a_run_past_its_timeout_is_run_hung(self):
        self.e.set_jobs([job("bg", time="07:30"), job("stuck", time="06:00")])
        self.e.touch(self.st, "stalled-bg-" + DS)
        os.makedirs(os.path.join(self.st, "claim-stuck-" + DS))
        with open(os.path.join(self.st, "claim-stuck-%s.owner.json" % DS), "w") as fh:
            json.dump({"run_id": RID2, "ds": DS, "runner": "headless", "claimed_at": DS + "T06:00:00-04:00"}, fh)
        h = self.health(now="2026-10-05T07:00")                                       # 60 min in, timeout_min 40 + 5
        self.assertEqual([i["schedule_id"] for i in h["issues"] if i["code"] == "run_hung"], ["stuck"])
        self.assertEqual([i["schedule_id"] for i in h["issues"] if i["code"] == "launch_stalled"], ["bg"])


class Probe(unittest.TestCase):
    def setUp(self):
        self.e = Env([job("bg", time="07:30")], probe_timeout_s=3)
        self.ld = os.path.join(self.e.tmp, "launchd")
        self.kw = {"CHRONOS_LAUNCHCTL": os.path.join(ROOT, "tests", "fake-launchctl.sh"), "FAKE_LAUNCHCTL_DIR": self.ld}

    def tearDown(self):
        self.e.cleanup()

    def calls(self):
        p = os.path.join(self.ld, "calls.log")
        return open(p).read() if os.path.exists(p) else ""

    def block(self):
        open(self.e.cfg["blocked_file"], "w").write(json.dumps({"reason": "launch_stalled", "id": "bg", "ds": DS}))

    def test_probe_ok_clears_the_block_and_runs_claude_the_way_a_real_run_does(self):
        self.block()
        out, _ = api(self.e, ["probe"], {}, now=NOW, **self.kw)
        self.assertEqual((out["ok"], out["stalled_in_window"]), (True, []))
        self.assertFalse(os.path.exists(self.e.cfg["blocked_file"]))
        self.assertIn("submit -l io.github.chronos.probe.", self.calls())
        log = open(self.e.fake_log).read()
        self.assertIn("CHRONOS_RUN=1", log)
        self.assertIn("--max-turns 1", log)
        self.assertIn("Reply with the word OK.", log)
        self.assertIn('"telegram@claude-plugins-official":false', log)                 # the same plugin-off settings as a real run
        self.assertIn("CWD=" + self.e.cfg["workspace"].replace("/private", ""), log)

    def test_with_the_hanging_fake_probe_re_trips_the_block_with_tcc_suspected(self):
        self.block()
        out, _ = api(self.e, ["probe"], {}, now=NOW, FAKE_MODE="hang", **self.kw)
        self.assertFalse(out["ok"])
        blk = json.load(open(self.e.cfg["blocked_file"]))
        self.assertEqual((blk["reason"], blk["tcc_suspected"], blk["source"]), ("launch_stalled", True, "probe"))
        self.assertIn("remove io.github.chronos.probe.", self.calls())                  # the hung one-off job is removed, by label

    def test_a_successful_probe_lists_stalled_days_still_inside_their_window(self):
        self.block()
        self.e.touch(self.e.cfg["state_dir"], "stalled-bg-" + DS)
        out, _ = api(self.e, ["probe"], {}, now=NOW, **self.kw)
        self.assertEqual(out["stalled_in_window"], [{"id": "bg", "ds": DS}])
        out, _ = api(self.e, ["probe"], {}, now="2026-10-05T12:00", **self.kw)             # window (07:30 + 180) is over
        self.assertEqual(out["stalled_in_window"], [])


if __name__ == "__main__":
    unittest.main()
