#!/usr/bin/env python3
"""WP-C1: validate/apply, the single writer under the jobs flock, conflicts, byte-exact prompts, disable, pause."""
import fcntl
import os
import sys
import threading
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_helpers import *  # noqa: E402,F401,F403


def jobs_bytes(e):
    return open(e.cfg["jobs_file"], "rb").read()


def prompt_path(e, jid):
    return os.path.join(e.cfg["jobs_dir"], jid, "prompt.md")


class Validate(unittest.TestCase):
    def setUp(self):
        self.e = Env([job("alpha")])

    def tearDown(self):
        self.e.cleanup()

    def test_bad_json_on_stdin_is_a_json_error_with_exit_1(self):
        _, r = api(self.e, ["validate"], stdin="{not json", expect=1)
        self.assertEqual(err(r)["code"], "bad_request")

    def test_validate_returns_normalized_preview_and_the_etag_the_write_will_produce(self):
        v = validated(self.e, create_change("fresh"))
        self.assertTrue(v["ok"])
        self.assertEqual(v["normalized"], create_change("fresh"))                  # already canonical: unchanged
        self.assertEqual(v["preview"]["when_text"], "Weekdays at 7:30 AM")
        self.assertEqual(v["preview"]["tz"], NY)
        self.assertEqual(len(v["preview"]["next_runs"]), 3)
        for t in v["preview"]["next_runs"]:
            self.assertRegex(t, RFC3339)
        self.assertRegex(v["expected_etag"], SHA)
        self.assertFalse(os.path.exists(os.path.join(self.e.cfg["jobs_dir"], "fresh")))   # dry run: nothing written

    def test_validate_normalizes_whitespace_and_day_lists(self):
        c = create_change("fresh", name="  Padded Name ", when={"type": "monthdays", "days": [15, 1, 15], "time": "07:30"})
        v = validated(self.e, c)
        self.assertEqual(v["normalized"]["draft"]["name"], "Padded Name")
        self.assertEqual(v["normalized"]["draft"]["when"], {"type": "monthdays", "days": [1, 15], "time": "07:30"})

    def test_validate_errors_carry_paths(self):
        bad = create_change("Fresh Job", catchup_min=5, notify="loud", runner="nowhere", model="gpt-9", when={"type": "daily", "time": "25:00"})
        _, r = api(self.e, ["validate"], bad, expect=1)
        e = err(r)
        self.assertEqual(e["code"], "schedule_invalid")
        paths = {f["path"] for f in e["fields"]}
        self.assertTrue({"draft.id", "draft.catchup_min", "draft.notify", "draft.runner", "draft.model", "draft.when"} <= paths, paths)

    def test_talos_only_and_unknown_fields_are_refused(self):
        c = create_change("fresh")
        c["draft"]["thread_id"] = "01ABC"
        _, r = api(self.e, ["validate"], c, expect=1)
        self.assertIn("draft.thread_id", [f["path"] for f in err(r)["fields"]])

    def test_prompt_rules(self):
        for bad in ("", "   \n", "bell\x07char", "x" * (16 * 1024 + 1)):
            _, r = api(self.e, ["validate"], create_change("fresh", prompt=bad), expect=1)
            self.assertIn("draft.prompt", [f["path"] for f in err(r)["fields"]], repr(bad[:10]))
        validated(self.e, create_change("fresh", prompt="tabs\tand\nnewlines are fine"))

    def test_restricted_cannot_run_in_session(self):
        _, r = api(self.e, ["validate"], create_change("fresh", permissions="restricted", runner="session"), expect=1)
        self.assertEqual(err(r)["code"], "schedule_invalid")
        v = validated(self.e, create_change("fresh", permissions="restricted", runner="headless", allowed_tools=["Read"]))
        self.assertTrue(v["ok"])

    def test_model_denylist_applies(self):
        e = Env([], model_denylist=["haiku"])
        try:
            _, r = api(e, ["validate"], create_change("fresh", model="haiku"), expect=1)
            self.assertIn("draft.model", [f["path"] for f in err(r)["fields"]])
        finally:
            e.cleanup()


class Apply(unittest.TestCase):
    def setUp(self):
        self.e = Env([job("alpha")])

    def tearDown(self):
        self.e.cleanup()

    def test_create_with_enabled_false_writes_false(self):
        out = apply_ok(self.e, create_change("fresh", enabled=False))
        self.assertTrue(out["ok"])
        rec = job_files(self.e, "fresh")[0]
        self.assertIs(rec["enabled"], False)
        self.assertFalse(sched(self.e, "fresh")["enabled"])
        self.assertEqual(sched(self.e, "fresh")["today"], "not_due")

    def test_create_maps_runner_and_permissions(self):
        apply_ok(self.e, create_change("sess", runner="session"))
        apply_ok(self.e, create_change("head", runner="headless", permissions="restricted", allowed_tools=["Read", "Grep"], model="sonnet"))
        s, h = job_files(self.e, "sess")[0], job_files(self.e, "head")[0]
        self.assertEqual((s["in_session"], s["headless"]), (True, False))
        self.assertEqual((h["in_session"], h["headless"], h["restricted"], h["model"], h["allowed_tools"]), (False, True, True, "sonnet", ["Read", "Grep"]))
        self.assertEqual(sched(self.e, "sess")["runner"], "session")
        self.assertEqual(sched(self.e, "head")["permissions"], "restricted")

    def test_once_job_stores_its_own_date_and_clock_time(self):
        apply_ok(self.e, create_change("remind", when={"type": "once", "at": "2026-10-09T15:30"}, runner="headless"))
        rec = job_files(self.e, "remind")[0]
        self.assertEqual((rec["once"], rec["time"], rec["days"]), ("2026-10-09T15:30", "15:30", "daily"))
        self.assertEqual(sched(self.e, "remind")["next_run"], "2026-10-09T15:30:00-04:00")

    def test_prompt_is_byte_identical_with_and_without_a_trailing_newline(self):
        for jid, text in (("with-nl", "Line one\nLine two\n"), ("no-nl", "Line one\nLine two"), ("multi-nl", "text\n\n\n"), ("unicode", "café — done\n")):
            apply_ok(self.e, create_change(jid, prompt=text))
            self.assertEqual(open(prompt_path(self.e, jid), "rb").read(), text.encode("utf-8"), jid)
        d, _ = api(self.e, ["detail", "no-nl"])
        self.assertEqual(d["prompt"], "Line one\nLine two")

    def test_apply_returns_the_etag_snapshot_shows(self):
        out = apply_ok(self.e, create_change("fresh"))
        self.assertEqual(out["etag"], sched(self.e, "fresh")["etag"])
        self.assertEqual(out["etag"], expected_etag(self.e, "fresh"))

    def test_apply_refuses_a_change_that_is_not_normalized(self):
        c = create_change("fresh", name=" padded ")
        _, r = api(self.e, ["apply"], {"change": c, "expected_etag": validated(self.e, c)["expected_etag"]}, expect=1)
        self.assertEqual(err(r)["code"], "schedule_invalid")
        self.assertFalse(os.path.exists(os.path.join(self.e.cfg["jobs_dir"], "fresh")))

    def test_apply_refuses_when_the_expected_etag_does_not_match(self):
        v = validated(self.e, create_change("fresh"))
        before = jobs_bytes(self.e)
        _, r = api(self.e, ["apply"], {"change": v["normalized"], "expected_etag": "sha256:" + "0" * 64}, expect=1)
        self.assertEqual(err(r)["code"], "schedule_conflict")
        self.assertEqual(jobs_bytes(self.e), before)
        self.assertFalse(os.path.exists(os.path.join(self.e.cfg["jobs_dir"], "fresh")))

    def test_stale_etag_is_a_conflict_and_leaves_the_file_unchanged(self):
        old = sched(self.e, "alpha")["etag"]
        open(prompt_path(self.e, "alpha"), "a").write("edited by hand\n")           # the job moves under the caller
        before = jobs_bytes(self.e)
        prompt_before = open(prompt_path(self.e, "alpha"), "rb").read()
        change = {"op": "update", "id": "alpha", "base_etag": old, "set": {"name": "Changed"}}
        _, r = api(self.e, ["validate"], change, expect=1)
        self.assertEqual(err(r)["code"], "schedule_conflict")
        _, r = api(self.e, ["apply"], {"change": change, "expected_etag": old}, expect=1)
        self.assertEqual(err(r)["code"], "schedule_conflict")
        self.assertEqual(jobs_bytes(self.e), before)
        self.assertEqual(open(prompt_path(self.e, "alpha"), "rb").read(), prompt_before)

    def test_create_on_an_existing_id_or_folder_is_a_conflict(self):
        _, r = api(self.e, ["validate"], create_change("alpha"), expect=1)
        self.assertEqual(err(r)["code"], "schedule_conflict")
        os.makedirs(os.path.join(self.e.cfg["jobs_dir"], "orphan-folder"))
        _, r = api(self.e, ["validate"], create_change("orphan-folder"), expect=1)
        self.assertEqual(err(r)["code"], "schedule_conflict")

    def test_update_changes_only_the_fields_in_set_and_keeps_history(self):
        base = sched(self.e, "alpha")
        change = {"op": "update", "id": "alpha", "base_etag": base["etag"], "set": {"name": "Renamed", "prompt": "New prompt\n", "catchup_min": 60}}
        out = apply_ok(self.e, change)
        s = sched(self.e, "alpha")
        self.assertEqual((s["name"], s["catchup_min"], s["when"]), ("Renamed", 60, base["when"]))
        self.assertEqual(open(prompt_path(self.e, "alpha")).read(), "New prompt\n")
        self.assertEqual(out["etag"], s["etag"])
        hist = os.listdir(os.path.join(self.e.cfg["history_dir"], "alpha"))
        self.assertEqual(len(hist), 1)
        self.assertTrue(hist[0].endswith("-prompt.md"))
        self.assertEqual(open(os.path.join(self.e.cfg["history_dir"], "alpha", hist[0])).read(), "Say hello. TASK-BODY-alpha\n")
        self.assertTrue(os.listdir(os.path.join(self.e.cfg["history_dir"], "jobs")))      # jobs.json snapshot too

    def test_update_of_a_both_job_must_choose_a_runner(self):
        self.e.set_jobs([job("dual", in_session=True)])
        base = sched(self.e, "dual")
        _, r = api(self.e, ["validate"], {"op": "update", "id": "dual", "base_etag": base["etag"], "set": {"name": "X"}}, expect=1)
        self.assertEqual(err(r)["code"], "schedule_invalid")
        self.assertEqual(err(r)["fields"][0]["path"], "runner")
        apply_ok(self.e, {"op": "update", "id": "dual", "base_etag": base["etag"], "set": {"runner": "session"}})
        self.assertEqual(sched(self.e, "dual")["runner"], "session")

    def test_a_job_with_no_way_to_run_cannot_be_saved(self):
        self.e.set_jobs([job("dead", in_session=False, headless=False)])
        base = sched(self.e, "dead")
        _, r = api(self.e, ["validate"], {"op": "update", "id": "dead", "base_etag": base["etag"], "set": {"name": "X"}}, expect=1)
        self.assertEqual(err(r)["code"], "schedule_invalid")
        apply_ok(self.e, {"op": "update", "id": "dead", "base_etag": base["etag"], "set": {"runner": "headless"}})    # choosing a runner repairs it

    def test_command_and_event_only_jobs_are_not_editable(self):
        self.e.set_jobs([job("cmd", kind="command", command="echo hi"), job("ev", clock=False, triggers=[{"type": "webhook"}])])
        for jid in ("cmd", "ev"):
            _, r = api(self.e, ["validate"], {"op": "update", "id": jid, "base_etag": sched(self.e, jid)["etag"], "set": {"name": "X"}}, expect=1)
            self.assertEqual(err(r)["code"], "schedule_invalid", jid)

    def test_delete_archives_the_folder_and_drops_the_record(self):
        base = sched(self.e, "alpha")
        v = validated(self.e, {"op": "delete", "id": "alpha", "base_etag": base["etag"]})
        self.assertIsNone(v["expected_etag"])
        out, _ = api(self.e, ["apply"], {"change": v["normalized"], "expected_etag": None})
        self.assertTrue(out["ok"])
        self.assertIsNone(out["etag"])
        self.assertEqual(snap(self.e)["schedules"], [])
        self.assertFalse(os.path.exists(os.path.join(self.e.cfg["jobs_dir"], "alpha")))
        arch = os.listdir(os.path.join(self.e.cfg["history_dir"], "deleted"))
        self.assertEqual(len(arch), 1)
        _, r = api(self.e, ["validate"], {"op": "delete", "id": "alpha", "base_etag": base["etag"]}, expect=1)
        self.assertEqual(err(r)["code"], "not_found")


class DisableAndPause(unittest.TestCase):
    def setUp(self):
        self.e = Env([job("alpha"), job("beta")])

    def tearDown(self):
        self.e.cleanup()

    def test_disable_switches_off_and_returns_the_new_etag(self):
        out, _ = api(self.e, ["disable", "alpha"])
        self.assertEqual(out["etag"], sched(self.e, "alpha")["etag"])
        self.assertFalse(sched(self.e, "alpha")["enabled"])
        self.assertTrue(sched(self.e, "beta")["enabled"])
        _, r = api(self.e, ["disable", "no-such"], expect=1)
        self.assertEqual(err(r)["code"], "not_found")

    def test_pause_one_and_all(self):
        out, _ = api(self.e, ["pause"], {"scope": {"id": "alpha"}, "paused": True})
        self.assertTrue(out["paused"])
        self.assertTrue(os.path.exists(os.path.join(self.e.cfg["paused_dir"], "alpha")))
        self.assertTrue(sched(self.e, "alpha")["paused"])
        self.assertFalse(sched(self.e, "beta")["paused"])
        api(self.e, ["pause"], {"scope": {"id": "alpha"}, "paused": False})
        self.assertFalse(sched(self.e, "alpha")["paused"])
        out, _ = api(self.e, ["pause"], {"scope": "all", "paused": True})
        self.assertTrue(out["paused_all"])
        self.assertTrue(os.path.exists(self.e.cfg["pause_all"]))
        api(self.e, ["pause"], {"scope": "all", "paused": False})
        self.assertFalse(os.path.exists(self.e.cfg["pause_all"]))
        _, r = api(self.e, ["pause"], {"scope": {"id": "no-such"}, "paused": True}, expect=1)
        self.assertEqual(err(r)["code"], "not_found")


class UiAndApiShareOneWriter(unittest.TestCase):
    """The web UI and `chronos api apply` both hold <jobs_file>.lock while they write, so they serialize."""

    def setUp(self):
        self.e = Env([job("alpha")])
        self.ui = UIServer(self.e)

    def tearDown(self):
        self.ui.stop()
        self.e.cleanup()

    def hold_lock(self):
        fh = open(self.e.cfg["jobs_file"] + ".lock", "a")
        fcntl.flock(fh, fcntl.LOCK_EX)
        return fh

    def test_a_ui_prompt_save_waits_for_the_lock_then_applies(self):
        fh = self.hold_lock()
        res = {}
        t = threading.Thread(target=lambda: res.update(r=self.ui.req("POST", "/api/jobs/alpha/prompt", {"content": "from the ui\n", "base_sha": None})))
        t.start()
        t.join(1.5)
        self.assertTrue(t.is_alive(), "the UI saved while another writer held the jobs lock")
        self.assertEqual(open(prompt_path(self.e, "alpha")).read(), "Say hello. TASK-BODY-alpha\n")
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
        t.join(10)
        self.assertEqual(res["r"][0], 200)
        self.assertEqual(open(prompt_path(self.e, "alpha")).read(), "from the ui\n")

    def test_apply_waits_for_the_lock_too(self):
        v = validated(self.e, create_change("fresh"))
        fh = self.hold_lock()
        res = {}
        t = threading.Thread(target=lambda: res.update(r=api(self.e, ["apply"], {"change": v["normalized"], "expected_etag": v["expected_etag"]})))
        t.start()
        t.join(1.5)
        self.assertTrue(t.is_alive(), "apply wrote while another writer held the jobs lock")
        self.assertFalse(os.path.exists(os.path.join(self.e.cfg["jobs_dir"], "fresh")))
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
        t.join(15)
        self.assertTrue(res["r"][0]["ok"])

    def test_ui_create_update_model_triggers_delete_still_work_through_the_shared_writer(self):
        st, r = self.ui.req("POST", "/api/jobs", {"name": "From UI", "id": "from-ui", "prompt": "Do UI things", "time": "10:00", "days": "daily", "notify": "failure"})
        self.assertEqual(st, 201, r)
        self.assertEqual(open(prompt_path(self.e, "from-ui")).read(), "Do UI things\n")
        self.assertTrue(job_files(self.e, "from-ui")[0]["enabled"])
        self.assertEqual(self.ui.req("POST", "/api/jobs/from-ui/schedule", {"time": "11:30"})[0], 200)
        self.assertEqual(self.ui.req("POST", "/api/jobs/from-ui/model", {"model": "sonnet"})[0], 200)
        self.assertEqual(self.ui.req("POST", "/api/jobs/from-ui/triggers", {"triggers": [{"type": "webhook"}], "clock": True})[0], 200)
        self.assertEqual(job_files(self.e, "from-ui")[0]["time"], "11:30")
        self.assertEqual(self.ui.req("DELETE", "/api/jobs/from-ui")[0], 200)
        self.assertEqual([s["id"] for s in snap(self.e)["schedules"]], ["alpha"])


if __name__ == "__main__":
    unittest.main(verbosity=2, warnings="ignore")
