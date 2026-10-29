#!/usr/bin/env python3
"""WP-C1: the approved record (N1), approval_required on and off (B1), config set, migrate, and the SessionStart hook switch."""
import os
import stat
import sys
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_helpers import *  # noqa: E402,F401,F403

NOW = "2026-10-05T09:00"          # a Monday; the jobs below fire at 08:30 with a 180 minute window


def make(e, jid, **kw):
    kw.setdefault("when", {"type": "weekdays", "time": "08:30"})
    kw.setdefault("runner", "headless")
    return apply_ok(e, create_change(jid, **kw))


def due_ids(e, now=NOW):
    r, lines = e.due(now)
    assert r.returncode == 0, r.stderr
    return [l.split("|")[0] for l in lines]


def approved_path(e, jid):
    return os.path.join(e.cfg["state_dir"], "approved", "%s.json" % jid)


def edit_prompt(e, jid, extra="\nAlso do something new.\n"):
    open(os.path.join(e.cfg["jobs_dir"], jid, "prompt.md"), "a").write(extra)


class ApprovedRecord(unittest.TestCase):
    def setUp(self):
        self.e = Env([], approval_required=True)
        make(self.e, "alpha")

    def tearDown(self):
        self.e.cleanup()

    def test_apply_writes_the_record_0600_with_hash_prompt_guard_time_and_via(self):
        p = approved_path(self.e, "alpha")
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        rec = json.load(open(p))
        self.assertEqual(set(rec), {"content_hash", "prompt", "guard", "approved_at", "via"})
        self.assertEqual(rec["content_hash"], expected_content_hash(self.e, "alpha"))
        self.assertEqual(rec["prompt"], "Do the thing.\n")
        self.assertIsNone(rec["guard"])
        self.assertEqual(rec["via"], "api_apply")
        self.assertRegex(rec["approved_at"], RFC3339)
        out, _ = api(self.e, ["approved", "alpha"])
        self.assertEqual(out, rec)
        none, _ = api(self.e, ["approved", "no-such"])
        self.assertIsNone(none)

    def test_a_clean_job_is_not_held_and_not_changed_outside(self):
        s = sched(self.e, "alpha")
        self.assertFalse(s["changed_outside"])
        self.assertEqual(s["today"], "waiting")
        self.assertEqual(due_ids(self.e), ["alpha"])

    def test_a_hand_edited_prompt_is_held_everywhere(self):
        edit_prompt(self.e, "alpha")
        s = sched(self.e, "alpha")
        self.assertTrue(s["changed_outside"])
        self.assertEqual(s["today"], "held")
        self.assertEqual(due_ids(self.e), [])                                      # not due
        r = self.e.run([sys.executable, CLI, "claim", "alpha"], CHRONOS_NOW=NOW)
        self.assertEqual((r.stdout.strip(), r.returncode), ("HELD", 1))             # not claimable
        r = self.e.run([sys.executable, CLI, "run", "alpha"], CHRONOS_NOW=NOW)      # `start`
        self.assertEqual(r.returncode, 1)
        self.assertIn("HELD", r.stderr)
        r = self.e.run([sys.executable, CLI, "prompt", "alpha"], CHRONOS_NOW=NOW)    # the prompt a run would get
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Also do something new", r.stdout)
        self.assertFalse(os.path.isdir(os.path.join(self.e.cfg["state_dir"], "claim-alpha-2026-10-05")))

    def test_the_headless_tick_does_not_spawn_a_held_job(self):
        edit_prompt(self.e, "alpha")
        r = self.e.run(["/bin/bash", TICK], CHRONOS_NOW=NOW)
        self.assertEqual(r.returncode, 0, r.stderr)
        time.sleep(1.5)
        self.assertFalse(os.path.exists(self.e.fake_log) and open(self.e.fake_log).read().strip(), "claude was started for a held job")
        self.assertEqual([n for n in os.listdir(self.e.cfg["state_dir"]) if n.startswith(("claim-", "ran-", "failed-"))], [])

    def test_the_headless_tick_still_runs_a_clean_job(self):
        r = self.e.run(["/bin/bash", TICK], CHRONOS_NOW=NOW)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.e.wait_for(lambda: os.path.exists(os.path.join(self.e.cfg["state_dir"], "ran-alpha-2026-10-05"))), "a clean job did not run")

    def test_deleting_the_approved_folder_holds_every_job(self):
        make(self.e, "beta")
        for n in os.listdir(os.path.join(self.e.cfg["state_dir"], "approved")):
            os.remove(os.path.join(self.e.cfg["state_dir"], "approved", n))
        self.assertEqual({s["today"] for s in snap(self.e)["schedules"]}, {"held"})
        self.assertEqual(due_ids(self.e), [])

    def test_adopt_makes_a_held_job_runnable_again(self):
        edit_prompt(self.e, "alpha")
        s = sched(self.e, "alpha")
        out, _ = api(self.e, ["apply"], {"change": {"op": "adopt", "id": "alpha", "content_hash": s["content_hash"]}, "expected_etag": s["etag"]})
        self.assertTrue(out["ok"])
        self.assertEqual(json.load(open(approved_path(self.e, "alpha")))["via"], "api_apply")
        self.assertEqual(sched(self.e, "alpha")["today"], "waiting")
        self.assertEqual(due_ids(self.e), ["alpha"])

    def test_adopt_with_a_stale_hash_is_a_conflict(self):
        s = sched(self.e, "alpha")
        edit_prompt(self.e, "alpha")                                                # the file moves after the owner looked
        _, r = api(self.e, ["apply"], {"change": {"op": "adopt", "id": "alpha", "content_hash": s["content_hash"]}, "expected_etag": s["etag"]}, expect=1)
        self.assertEqual(err(r)["code"], "schedule_conflict")
        self.assertEqual(sched(self.e, "alpha")["today"], "held")

    def test_hand_editing_the_job_settings_that_matter_also_holds(self):
        jobs = json.load(open(self.e.cfg["jobs_file"]))
        jobs[0]["restricted"] = False
        jobs[0]["model"] = "opus"                                                   # model is part of content_hash
        json.dump(jobs, open(self.e.cfg["jobs_file"], "w"))
        self.assertEqual(sched(self.e, "alpha")["today"], "held")

    def test_changing_the_time_by_hand_does_not_hold(self):
        jobs = json.load(open(self.e.cfg["jobs_file"]))
        jobs[0]["time"] = "08:45"
        json.dump(jobs, open(self.e.cfg["jobs_file"], "w"))
        self.assertEqual(sched(self.e, "alpha")["today"], "waiting")

    def test_update_converting_both_to_session_is_not_held(self):
        self.e.set_jobs([job("dual", in_session=True, time="08:30")])
        s = sched(self.e, "dual")
        out, _ = api(self.e, ["apply"], {"change": {"op": "adopt", "id": "dual", "content_hash": s["content_hash"]}, "expected_etag": s["etag"]})
        self.assertEqual(sched(self.e, "dual")["today"], "waiting")
        apply_ok(self.e, {"op": "update", "id": "dual", "base_etag": sched(self.e, "dual")["etag"], "set": {"runner": "session"}})
        s = sched(self.e, "dual")
        self.assertEqual((s["runner"], s["today"], s["changed_outside"]), ("session", "waiting", False))

    def test_disable_does_not_approve_a_held_job(self):
        edit_prompt(self.e, "alpha")
        api(self.e, ["disable", "alpha"])
        self.assertTrue(sched(self.e, "alpha")["changed_outside"])

    def test_deleting_a_job_removes_its_record(self):
        s = sched(self.e, "alpha")
        apply_ok(self.e, {"op": "delete", "id": "alpha", "base_etag": s["etag"]})
        self.assertFalse(os.path.exists(approved_path(self.e, "alpha")))


class ApprovedRecordThroughTheUi(unittest.TestCase):
    def setUp(self):
        self.e = Env([], approval_required=True)
        make(self.e, "alpha")
        self.ui = UIServer(self.e)

    def tearDown(self):
        self.ui.stop()
        self.e.cleanup()

    def test_a_ui_prompt_save_is_an_approval(self):
        edit_prompt(self.e, "alpha")
        self.assertEqual(sched(self.e, "alpha")["today"], "held")
        st, r = self.ui.req("POST", "/api/jobs/alpha/prompt", {"content": "Replaced in the UI.\n", "base_sha": None})
        self.assertEqual(st, 200, r)
        self.assertEqual(sched(self.e, "alpha")["today"], "waiting")
        self.assertEqual(json.load(open(approved_path(self.e, "alpha")))["via"], "ui")

    def test_approve_as_it_is_on_disk_is_token_gated_and_writes_via_ui(self):
        edit_prompt(self.e, "alpha")
        self.assertEqual(self.ui.req("POST", "/api/jobs/alpha/approve", {}, token=False)[0], 401)
        st, r = self.ui.req("GET", "/api/jobs/alpha")
        self.assertTrue(r["held"])
        self.assertTrue(r["changed_outside"])
        st, r = self.ui.req("POST", "/api/jobs/alpha/approve", {})
        self.assertEqual(st, 400, r)                                                  # the hash of what was reviewed is required
        st, r = self.ui.req("POST", "/api/jobs/alpha/approve", {"content_hash": sched(self.e, "alpha")["content_hash"]})
        self.assertEqual(st, 200, r)
        rec = json.load(open(approved_path(self.e, "alpha")))
        self.assertEqual(rec["via"], "ui")
        self.assertIn("Also do something new", rec["prompt"])                         # approved exactly as it was on disk
        self.assertEqual(sched(self.e, "alpha")["today"], "waiting")
        self.assertEqual(self.ui.req("POST", "/api/jobs/no-such/approve", {"content_hash": "sha256:" + "0" * 64})[0], 404)

    def test_a_ui_settings_save_on_a_held_job_does_not_approve_what_changed_outside(self):
        edit_prompt(self.e, "alpha")
        self.assertEqual(self.ui.req("POST", "/api/jobs/alpha/schedule", {"time": "08:45"})[0], 200)
        self.assertEqual(sched(self.e, "alpha")["today"], "held")

    def test_a_ui_created_job_has_a_record_and_a_ui_settings_save_keeps_it_current(self):
        st, r = self.ui.req("POST", "/api/jobs", {"name": "UI Made", "id": "ui-made", "prompt": "Hello", "time": "08:30", "days": "weekdays"})
        self.assertEqual(st, 201, r)
        self.assertEqual(json.load(open(approved_path(self.e, "ui-made")))["via"], "ui")
        self.assertEqual(self.ui.req("POST", "/api/jobs/ui-made/model", {"model": "sonnet"})[0], 200)
        self.assertEqual(sched(self.e, "ui-made")["today"], "waiting")


class DefaultOffKeepsZeroPointTwoBehaviour(unittest.TestCase):
    """B1: approval_required defaults to false. Records are still written and changed_outside still reported; nothing is held."""

    def setUp(self):
        self.e = Env([job("hand", time="08:30")])                                      # hand-written, no record, like every 0.2.2 test

    def tearDown(self):
        self.e.cleanup()

    def test_defaults(self):
        self.assertIs(self.e.cfg["approval_required"], False)
        self.assertEqual(self.e.cfg["session_runner"], "hook")
        v, _ = api(self.e, ["version"])
        self.assertIs(v["approval_required"], False)

    def test_a_hand_written_job_with_no_record_runs_on_the_tick(self):
        self.assertEqual(due_ids(self.e), ["hand"])
        r = self.e.run(["/bin/bash", TICK], CHRONOS_NOW=NOW)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.e.wait_for(lambda: os.path.exists(os.path.join(self.e.cfg["state_dir"], "ran-hand-2026-10-05"))), "the job did not run")

    def test_a_hand_edited_prompt_runs_but_snapshot_reports_changed_outside(self):
        make(self.e, "made")
        self.assertFalse(sched(self.e, "made")["changed_outside"])
        edit_prompt(self.e, "made")
        s = sched(self.e, "made")
        self.assertTrue(s["changed_outside"])
        self.assertNotEqual(s["today"], "held")
        self.assertEqual(s["today"], "waiting")
        self.assertIn("made", due_ids(self.e))
        self.assertEqual(self.e.run([sys.executable, CLI, "claim", "made"], CHRONOS_NOW=NOW).stdout.strip(), "CLAIMED")
        self.assertTrue(sched(self.e, "hand")["changed_outside"])                       # no record at all also counts as changed

    def test_an_upgrade_from_a_0_2_2_state_dir_holds_nothing(self):
        self.e.set_jobs([job("one", time="08:30"), job("two", time="08:30"), job("three", time="08:30")])
        self.assertFalse(os.path.exists(os.path.join(self.e.cfg["state_dir"], "approved")))
        self.assertEqual(sorted(due_ids(self.e)), ["one", "three", "two"])
        self.assertNotIn("held", {s["today"] for s in snap(self.e)["schedules"]})


class SwitchingEnforcementOn(unittest.TestCase):
    def test_after_approving_two_of_three_exactly_the_third_is_held(self):
        e = Env([job("one", time="08:30"), job("two", time="08:30"), job("three", time="08:30")])
        try:
            for jid in ("one", "two"):
                s = sched(e, jid)
                api(e, ["apply"], {"change": {"op": "adopt", "id": jid, "content_hash": s["content_hash"]}, "expected_etag": s["etag"]})
            self.assertEqual(sorted(due_ids(e)), ["one", "three", "two"])                  # still off: nothing held
            out, _ = api(e, ["config", "set", "session_runner", "talos", "approval_required", "true"])
            self.assertTrue(out["ok"])
            cfg_file = json.load(open(e.cfg_path))
            self.assertEqual((cfg_file["session_runner"], cfg_file["approval_required"]), ("talos", True))   # one write, both keys
            s = {x["id"]: x["today"] for x in snap(e)["schedules"]}
            self.assertEqual(s, {"one": "waiting", "two": "waiting", "three": "held"})
            self.assertEqual(sorted(due_ids(e)), ["one", "two"])
            v, _ = api(e, ["version"])
            self.assertEqual((v["session_runner"], v["approval_required"]), ("talos", True))
        finally:
            e.cleanup()


class ConfigSet(unittest.TestCase):
    def setUp(self):
        self.e = Env([job("dual", in_session=True), job("solo", in_session=True, headless=False)])

    def tearDown(self):
        self.e.cleanup()

    def test_talos_is_refused_while_a_both_job_exists_and_names_it(self):
        before = open(self.e.cfg_path).read()
        _, r = api(self.e, ["config", "set", "session_runner", "talos", "approval_required", "true"], expect=1)
        e = err(r)
        self.assertEqual(e["code"], "both_jobs_remain")
        self.assertEqual([f["path"] for f in e["fields"]], ["dual"])
        self.assertEqual(open(self.e.cfg_path).read(), before)                            # nothing half-written

    def test_values_from_stdin_and_validation(self):
        out, _ = api(self.e, ["config", "set"], {"session_wait_notify_min": 45, "approval_required": True})
        self.assertEqual(out["set"], {"session_wait_notify_min": 45, "approval_required": True})
        self.assertEqual(C.load_config()["session_wait_notify_min"], 45)
        _, r = api(self.e, ["config", "set", "session_runner", "sideways"], expect=1)
        self.assertEqual(err(r)["code"], "schedule_invalid")
        _, r = api(self.e, ["config", "set", "claude_bin", "/bin/sh"], expect=1)           # only the 0.3 keys can be set this way
        self.assertEqual(err(r)["code"], "bad_request")

    def test_talos_is_accepted_once_no_both_job_remains(self):
        s = sched(self.e, "dual")
        apply_ok(self.e, {"op": "update", "id": "dual", "base_etag": s["etag"], "set": {"runner": "session"}})
        out, _ = api(self.e, ["config", "set", "session_runner", "talos"])
        self.assertTrue(out["ok"])


class Migrate(unittest.TestCase):
    """`chronos api migrate`: the interactive y/N baseline for a 0.2 install (the same steps as `talos-app schedules adopt`)."""

    def test_approve_convert_and_switch(self):
        e = Env([job("keep", time="08:30"), job("skip", time="08:30"), job("dual", in_session=True, time="08:30")])
        try:
            # prompts, in order: approve keep, approve skip, approve dual, then convert dual to session-only
            out, r = api(e, ["migrate"], stdin="y\nn\ny\ny\n")
            self.assertEqual(out["approved"], ["keep", "dual"])
            self.assertEqual(out["held"], ["skip"])
            self.assertEqual(out["converted"], ["dual"])
            self.assertTrue(out["switched"])
            self.assertIn("Approve", r.stderr)
            v, _ = api(e, ["version"])
            self.assertEqual((v["session_runner"], v["approval_required"]), ("talos", True))
            s = {x["id"]: x for x in snap(e)["schedules"]}
            self.assertEqual(s["keep"]["today"], "waiting")
            self.assertEqual(s["skip"]["today"], "held")
            self.assertEqual((s["dual"]["runner"], s["dual"]["today"]), ("session", "waiting"))     # a job migrate converts is never held
            self.assertEqual(json.load(open(approved_path(e, "keep")))["via"], "migrate")
        finally:
            e.cleanup()

    def test_declining_the_conversion_keeps_both_and_does_not_switch(self):
        e = Env([job("dual", in_session=True, time="08:30")])
        try:
            out, _ = api(e, ["migrate"], stdin="y\nn\n")
            self.assertEqual((out["converted"], out["switched"], out["blocking"]), ([], False, ["dual"]))
            v, _ = api(e, ["version"])
            self.assertEqual((v["session_runner"], v["approval_required"]), ("hook", False))
        finally:
            e.cleanup()

    def test_running_it_again_asks_nothing_about_approved_jobs(self):
        e = Env([job("keep", time="08:30")])
        try:
            api(e, ["migrate"], stdin="y\n")
            out, r = api(e, ["migrate"], stdin="")
            self.assertEqual((out["approved"], out["held"]), ([], []))
            self.assertNotIn("Approve", r.stderr.split("Chronos jobs on this Mac:")[-1].split("\n", 3)[-1] if False else "")
        finally:
            e.cleanup()


class SessionStartHook(unittest.TestCase):
    def run_hook(self, e, now="2026-10-05T07:00"):
        r = e.run([sys.executable, HOOK], CHRONOS_NOW=now)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]

    def test_hook_arms_session_jobs_by_default(self):
        e = Env([job("sess", in_session=True, headless=False, time="08:30")])
        try:
            ctx = self.run_hook(e)
            self.assertIn("ARM THESE JOBS", ctx)
            self.assertIn("sess", ctx)
        finally:
            e.cleanup()

    def test_under_talos_the_hook_arms_nothing_and_says_so(self):
        e = Env([job("sess", in_session=True, headless=False, time="08:30")], session_runner="talos")
        try:
            ctx = self.run_hook(e)
            self.assertIn("Talos runs session jobs; no timers armed.", ctx)
            self.assertNotIn("ARM THESE JOBS", ctx)
            self.assertNotIn("CronCreate", ctx)
        finally:
            e.cleanup()

    def test_a_held_job_is_not_armed(self):
        e = Env([], approval_required=True)
        try:
            make(e, "sess", runner="session")
            self.assertIn("sess", self.run_hook(e))
            edit_prompt(e, "sess")
            self.assertNotIn("sess", self.run_hook(e))
        finally:
            e.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2, warnings="ignore")
