#!/usr/bin/env python3
"""WP-C1: `chronos api` version/snapshot/detail, RFC 3339 times with IANA zone, DST, hashes, day states."""
import os
import sys
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_helpers import *  # noqa: E402,F401,F403


class VersionAndShape(unittest.TestCase):
    def setUp(self):
        self.e = Env([job("alpha", time="09:00"), job("sat-job", days="sat", time="10:15", in_session=True, headless=False)])

    def tearDown(self):
        self.e.cleanup()

    def test_version_reports_api_runner_and_approval_default(self):
        out, _ = api(self.e, ["version"])
        self.assertEqual(out, {"api": 1, "chronos": "0.3.1", "session_runner": "hook", "approval_required": False})

    def test_unknown_verb_is_a_json_error_with_exit_1(self):
        _, r = api(self.e, ["nonsense"], expect=1)
        self.assertEqual(err(r)["code"], "unknown_verb")

    def test_snapshot_shape(self):
        out = snap(self.e)
        self.assertEqual(out["api"], 1)
        self.assertEqual(out["tz"], NY)
        for k in ("as_of", "session_runner", "approval_required", "paused_all", "session_beat_age_s", "tick_age_s", "schedules"):
            self.assertIn(k, out)
        self.assertEqual([s["id"] for s in out["schedules"]], ["alpha", "sat-job"])
        a = out["schedules"][0]
        want = {"id", "name", "description", "kind", "when", "when_text", "catchup_min", "enabled", "paused", "runner", "permissions",
                "allowed_tools", "model", "notify", "triggers_count", "next_run", "today", "window_ends", "last_run", "prompt_preview",
                "prompt_bytes", "editable", "read_only_reason", "changed_outside", "etag", "content_hash", "state_rev"}
        self.assertTrue(want <= set(a), want - set(a))
        self.assertEqual(a["when"], {"type": "weekdays", "time": "09:00"})
        self.assertEqual(a["when_text"], "Weekdays at 9:00 AM")
        self.assertEqual(a["kind"], "claude")
        self.assertEqual(a["permissions"], "full")
        self.assertEqual(a["prompt_bytes"], len("Say hello. TASK-BODY-alpha\n"))
        self.assertTrue(a["prompt_preview"].startswith("Say hello."))
        for k in ("etag", "content_hash", "state_rev"):
            self.assertRegex(a[k], SHA)

    def test_when_variants(self):
        self.e.set_jobs([job("dd", days="daily"), job("mm", days="dom:1,15"), job("xx", days="mon,wed", time="08:05"),
                         job("oo", once="2026-10-06T15:00", time="15:00", days="daily"), job("ev", clock=False, triggers=[{"type": "webhook"}])])
        w = {s["id"]: s["when"] for s in snap(self.e)["schedules"]}
        self.assertEqual(w["dd"], {"type": "daily", "time": "09:00"})
        self.assertEqual(w["mm"], {"type": "monthdays", "days": [1, 15], "time": "09:00"})
        self.assertEqual(w["xx"], {"type": "days", "days": ["mon", "wed"], "time": "08:05"})
        self.assertEqual(w["oo"], {"type": "once", "at": "2026-10-06T15:00"})
        self.assertEqual(w["ev"], {"type": "events"})

    def test_detail_has_prompt_guard_next_runs(self):
        os.makedirs(os.path.join(self.e.cfg["jobs_dir"], "alpha"), exist_ok=True)
        open(os.path.join(self.e.cfg["jobs_dir"], "alpha", "guard.md"), "w").write("Never delete.\n")
        out, _ = api(self.e, ["detail", "alpha"])
        self.assertEqual(out["schedule"]["id"], "alpha")
        self.assertEqual(out["prompt"], "Say hello. TASK-BODY-alpha\n")
        self.assertFalse(out["prompt_truncated"])
        self.assertEqual(out["guard"], "Never delete.\n")
        self.assertEqual(len(out["next_runs"]), 3)
        self.assertEqual(out["runs"], [])
        _, r = api(self.e, ["detail", "no-such"], expect=1)
        self.assertEqual(err(r)["code"], "not_found")

    def test_detail_truncates_a_long_prompt_and_marks_it_read_only(self):
        big = "x" * (70 * 1024)
        open(os.path.join(self.e.cfg["jobs_dir"], "alpha", "prompt.md"), "w").write(big)
        out, _ = api(self.e, ["detail", "alpha"])
        self.assertTrue(out["prompt_truncated"])
        self.assertLessEqual(len(out["prompt"].encode()), 64 * 1024)
        self.assertFalse(out["schedule"]["editable"])
        self.assertIn("longer than the app can edit", out["schedule"]["read_only_reason"])


class Times(unittest.TestCase):
    """Every instant is RFC 3339 with an offset; the zone is IANA. Naive local times are what 0.2.2 printed."""

    def instants(self, doc):
        out = []
        for p, v in walk(doc):
            if isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}T", v) and not p.endswith(".when.at"):
                out.append((p, v))
        return out

    def test_july_every_instant_has_the_summer_offset(self):
        e = Env([job("alpha", time="09:00"), job("late", time="23:50")])
        try:
            out = snap(e, now="2026-07-15T09:30", tz=NY)
            self.assertEqual(out["tz"], NY)
            inst = self.instants(out)
            self.assertTrue(inst)
            for p, v in inst:
                self.assertRegex(v, RFC3339, p)
                self.assertTrue(v.endswith("-04:00"), (p, v))
            a = [s for s in out["schedules"] if s["id"] == "alpha"][0]
            self.assertEqual(a["next_run"], "2026-07-16T09:00:00-04:00")
            self.assertEqual(out["as_of"], "2026-07-15T09:30:00-04:00")
            d, _ = api(e, ["detail", "alpha"], now="2026-07-15T09:30")
            for p, v in self.instants(d):
                self.assertRegex(v, RFC3339, p)
        finally:
            e.cleanup()

    def test_fall_back_day_offsets_and_a_0130_job_fires_once(self):
        e = Env([job("early", time="01:30", days="daily")])
        try:
            out = snap(e, now="2026-11-01T00:30", tz=NY)
            for p, v in self.instants(out):
                self.assertRegex(v, RFC3339, p)
            s = out["schedules"][0]
            self.assertEqual(s["next_run"], "2026-11-01T01:30:00-04:00")        # first occurrence of the repeated hour
            self.assertEqual(out["tz"], NY)
            after = snap(e, now="2026-11-01T03:00", tz=NY)["schedules"][0]
            self.assertEqual(after["next_run"], "2026-11-02T01:30:00-05:00")    # then standard time
            # due once: the repeated 01:30-01:59 hour lists it, a done-marker stops the second pass
            r, lines = e.due("2026-11-01T01:45", TZ=NY)
            self.assertEqual(lines, ["early|2026-11-01"])
            e.touch(e.cfg["state_dir"], "ran-early-2026-11-01")
            r, lines = e.due("2026-11-01T01:45", TZ=NY)
            self.assertEqual(lines, [])
        finally:
            e.cleanup()

    def test_spring_forward_gap_fires_at_the_next_valid_minute(self):
        e = Env([job("gap", time="02:30", days="daily")])
        try:
            s = snap(e, now="2026-03-08T00:10", tz=NY)["schedules"][0]
            self.assertEqual(s["next_run"], "2026-03-08T03:00:00-04:00")
        finally:
            e.cleanup()

    def test_zone_comes_from_TZ_then_from_localtime(self):
        self.assertEqual(C.zone_name({"TZ": "America/Chicago"}), "America/Chicago")
        self.assertEqual(C.zone_name({"TZ": ":America/Denver"}), "America/Denver")
        z = C.zone_name({"TZ": ""}, localtime="/var/db/timezone/zoneinfo/Europe/Paris")
        self.assertEqual(z, "Europe/Paris")
        self.assertEqual(C.zone_name({}, localtime="/usr/share/zoneinfo/Asia/Tokyo"), "Asia/Tokyo")
        self.assertEqual(C.zone_name({}, localtime="/usr/share/zoneinfo.default/America/Los_Angeles"), "America/Los_Angeles")   # what macOS resolves to
        self.assertEqual(C.zone_name({"TZ": "EST5EDT"}, localtime="/usr/share/zoneinfo/Asia/Tokyo"), "EST5EDT")
        self.assertEqual(C.zone_name({}, localtime=None), "UTC")

    def test_no_tz_env_still_gives_an_iana_zone_and_offsets(self):
        e = Env([job("alpha")])
        try:
            out = snap(e, now="2026-07-15T09:30", tz=None)
            self.assertTrue(out["tz"] == "UTC" or "/" in out["tz"], out["tz"])
            self.assertRegex(out["as_of"], RFC3339)
        finally:
            e.cleanup()


class Hashes(unittest.TestCase):
    def setUp(self):
        self.e = Env([job("alpha"), job("beta", model="sonnet", allowed_tools=["Read"], restricted=True)])
        open(os.path.join(self.e.cfg["jobs_dir"], "beta", "guard.md"), "w").write("Guard rules\n")

    def tearDown(self):
        self.e.cleanup()

    def test_etag_and_content_hash_follow_the_spec_construction(self):
        for jid in ("alpha", "beta"):
            s = sched(self.e, jid)
            self.assertEqual(s["etag"], expected_etag(self.e, jid))
            self.assertEqual(s["content_hash"], expected_content_hash(self.e, jid))

    def test_disable_once_changes_etag_but_not_content_hash(self):
        self.e.set_jobs([job("remind", once="2026-10-05T09:00", time="09:00", days="daily")])
        before = sched(self.e, "remind")
        r = self.e.run([sys.executable, CLI, "disable-once", "remind"])
        self.assertEqual(r.returncode, 0, r.stderr)
        after = sched(self.e, "remind")
        self.assertFalse(after["enabled"])
        self.assertNotEqual(before["etag"], after["etag"])
        self.assertEqual(before["content_hash"], after["content_hash"])

    def test_time_notify_name_and_updated_edits_leave_content_hash_alone(self):
        before = sched(self.e, "alpha")
        jobs = json.load(open(self.e.cfg["jobs_file"]))
        jobs[0].update({"time": "11:11", "name": "Renamed", "notify": "always", "updated": "2026-10-04T00:00:00", "enabled": False})
        json.dump(jobs, open(self.e.cfg["jobs_file"], "w"))
        after = sched(self.e, "alpha")
        self.assertEqual(before["content_hash"], after["content_hash"])
        self.assertNotEqual(before["etag"], after["etag"])
        # but a prompt edit moves both
        open(os.path.join(self.e.cfg["jobs_dir"], "alpha", "prompt.md"), "a").write("more\n")
        third = sched(self.e, "alpha")
        self.assertNotEqual(after["content_hash"], third["content_hash"])

    def test_updated_and_created_do_not_move_the_etag(self):
        before = sched(self.e, "alpha")["etag"]
        jobs = json.load(open(self.e.cfg["jobs_file"]))
        jobs[0]["updated"] = "2030-01-01T00:00:00"
        jobs[0]["created"] = "2020-01-01T00:00:00"
        json.dump(jobs, open(self.e.cfg["jobs_file"], "w"))
        self.assertEqual(sched(self.e, "alpha")["etag"], before)


class DayStates(unittest.TestCase):
    """S7.5. Monday 2026-10-05; alpha fires 09:00 with a 180 minute window."""

    def setUp(self):
        self.e = Env([job("alpha", time="09:00", catchup_min=180)])

    def tearDown(self):
        self.e.cleanup()

    def today(self, now):
        return sched(self.e, "alpha", now=now)["today"]

    def test_pending_waiting_missed_use_the_catchup_window(self):
        self.assertEqual(self.today("2026-10-05T08:59"), "pending")
        self.assertEqual(self.today("2026-10-05T09:00"), "waiting")
        self.assertEqual(self.today("2026-10-05T11:59"), "waiting")
        self.assertEqual(self.today("2026-10-05T12:00"), "missed")               # only at fire + catchup_min, never before

    def test_not_due_on_a_day_off_and_when_disabled(self):
        self.assertEqual(self.today("2026-10-03T10:00"), "not_due")              # Saturday
        jobs = json.load(open(self.e.cfg["jobs_file"]))
        jobs[0]["enabled"] = False
        json.dump(jobs, open(self.e.cfg["jobs_file"], "w"))
        self.assertEqual(self.today("2026-10-05T10:00"), "not_due")

    def test_markers_and_claim_decide_ran_failed_stalled_running(self):
        st = self.e.cfg["state_dir"]
        self.e.touch(st, "failed-alpha-2026-10-05")
        self.assertEqual(self.today("2026-10-05T10:00"), "failed")
        os.remove(os.path.join(st, "failed-alpha-2026-10-05"))
        self.e.touch(st, "stalled-alpha-2026-10-05")
        self.assertEqual(self.today("2026-10-05T10:00"), "stalled")
        os.remove(os.path.join(st, "stalled-alpha-2026-10-05"))
        os.makedirs(os.path.join(st, "claim-alpha-2026-10-05"))
        self.assertEqual(self.today("2026-10-05T10:00"), "running")
        os.rmdir(os.path.join(st, "claim-alpha-2026-10-05"))
        self.e.touch(st, "ran-alpha-2026-10-05")
        self.assertEqual(self.today("2026-10-05T10:00"), "ran")

    def test_paused_flag_and_pause_all(self):
        self.assertFalse(sched(self.e, "alpha")["paused"])
        self.e.touch(self.e.cfg["paused_dir"], "alpha")
        self.assertTrue(sched(self.e, "alpha")["paused"])
        self.e.touch(self.e.cfg["pause_all"])
        self.assertTrue(snap(self.e)["paused_all"])


class RunnerMapping(unittest.TestCase):
    """The S2.1 table. headless absent means true."""

    def test_the_four_combinations(self):
        e = Env([job("sess", in_session=True, headless=False), job("head", in_session=False, headless=True),
                 job("head-absent", in_session=False), job("both-job", in_session=True), job("both-explicit", in_session=True, headless=True),
                 job("dead", in_session=False, headless=False)])
        try:
            s = {x["id"]: x for x in snap(e)["schedules"]}
            self.assertEqual(s["sess"]["runner"], "session")
            self.assertEqual(s["head"]["runner"], "headless")
            self.assertEqual(s["head-absent"]["runner"], "headless")
            self.assertEqual(s["both-job"]["runner"], "both")
            self.assertEqual(s["both-explicit"]["runner"], "both")
            self.assertFalse(s["both-job"]["editable"])
            self.assertIn("Session, with a background backup", s["both-job"]["read_only_reason"])
            self.assertFalse(s["dead"]["editable"])
            self.assertEqual(s["dead"]["read_only_reason"], "Has no way to run")
            self.assertTrue(s["sess"]["editable"])
        finally:
            e.cleanup()

    def test_command_restricted_and_event_only_jobs(self):
        e = Env([job("cmd", kind="command", command="echo hi"), job("rest", restricted=True, allowed_tools=["Read"]),
                 job("ev", clock=False, triggers=[{"type": "webhook"}])])
        try:
            s = {x["id"]: x for x in snap(e)["schedules"]}
            self.assertEqual((s["cmd"]["kind"], s["cmd"]["permissions"], s["cmd"]["editable"]), ("command", "command", False))
            self.assertEqual((s["rest"]["permissions"], s["rest"]["runner"]), ("restricted", "headless"))
            self.assertEqual(s["rest"]["allowed_tools"], ["Read"])
            self.assertEqual((s["ev"]["when"], s["ev"]["triggers_count"], s["ev"]["editable"]), ({"type": "events"}, 1, False))
        finally:
            e.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2, warnings="ignore")
