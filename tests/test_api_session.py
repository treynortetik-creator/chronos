#!/usr/bin/env python3
"""WP-C1: `due --for session` (no grace, yesterday's fire, midnight), the session heartbeat, one-shots under talos, claims keyed on ds."""
import os
import sys
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_helpers import *  # noqa: E402,F401,F403


def due_session(e, now, **kw):
    out, _ = api(e, ["due", "--for", "session"], now=now, **kw)
    return out


class DueForSession(unittest.TestCase):
    def setUp(self):
        self.e = Env([job("morning", time="07:30", in_session=True, headless=False),
                      job("night", time="23:50", days="daily", in_session=True, headless=False, catchup_min=180),
                      job("bg", time="07:30", in_session=False)])

    def tearDown(self):
        self.e.cleanup()

    def ids(self, now, **kw):
        return [(d["id"], d["ds"]) for d in due_session(self.e, now, **kw)["due"]]

    def test_offered_at_fire_time_with_no_grace_and_never_a_headless_job(self):
        self.assertEqual(self.ids("2026-10-05T07:29"), [])
        self.assertEqual(self.ids("2026-10-05T07:30"), [("morning", "2026-10-05")])      # the headless tick would wait grace_min; the session does not
        d = due_session(self.e, "2026-10-05T07:30")["due"][0]
        self.assertEqual(d["fire_at"], "2026-10-05T07:30:00-04:00")
        self.assertEqual(d["window_ends"], "2026-10-05T10:30:00-04:00")                  # fire + catchup_min
        self.assertEqual(self.ids("2026-10-05T10:29"), [("morning", "2026-10-05")])
        self.assertEqual(self.ids("2026-10-05T10:30"), [])                               # the window is over
        self.assertNotIn("bg", [i for i, _ in self.ids("2026-10-05T07:45")])

    def test_midnight_a_2350_job_is_offered_at_0005_with_yesterdays_ds(self):
        self.assertEqual(self.ids("2026-10-06T00:05"), [("night", "2026-10-05")])
        d = due_session(self.e, "2026-10-06T00:05")["due"][0]
        self.assertEqual(d["fire_at"], "2026-10-05T23:50:00-04:00")
        self.assertEqual(d["window_ends"], "2026-10-06T02:50:00-04:00")
        self.assertEqual(self.ids("2026-10-06T02:49"), [("night", "2026-10-05")])
        self.assertEqual(self.ids("2026-10-06T02:50"), [])

    def test_midnight_yesterdays_claim_and_marker_leave_todays_fire_untouched(self):
        st = self.e.cfg["state_dir"]
        self.e.touch(st, "ran-night-2026-10-05")
        self.assertEqual(self.ids("2026-10-06T00:05"), [])                                # yesterday's fire is done
        self.assertEqual(self.ids("2026-10-06T23:55"), [("night", "2026-10-06")])         # today's 23:50 is a separate day
        os.makedirs(os.path.join(st, "claim-night-2026-10-06"))
        self.assertEqual(self.ids("2026-10-06T23:55"), [])

    def test_ran_failed_stalled_markers_and_claims_remove_a_job_from_the_list(self):
        st = self.e.cfg["state_dir"]
        for marker in ("ran-morning-2026-10-05", "failed-morning-2026-10-05", "stalled-morning-2026-10-05"):
            p = self.e.touch(st, marker)
            self.assertEqual(self.ids("2026-10-05T08:00"), [], marker)
            os.remove(p)
        self.assertEqual(self.ids("2026-10-05T08:00"), [("morning", "2026-10-05")])

    def test_paused_job_pause_all_and_disabled_job_are_not_offered(self):
        self.e.touch(self.e.cfg["paused_dir"], "morning")
        self.assertEqual(self.ids("2026-10-05T08:00"), [])
        os.remove(os.path.join(self.e.cfg["paused_dir"], "morning"))
        self.e.touch(self.e.cfg["pause_all"])
        self.assertEqual(self.ids("2026-10-05T08:00"), [])
        os.remove(self.e.cfg["pause_all"])
        jobs = json.load(open(self.e.cfg["jobs_file"]))
        jobs[0]["enabled"] = False
        json.dump(jobs, open(self.e.cfg["jobs_file"], "w"))
        self.assertEqual(self.ids("2026-10-05T08:00"), [])

    def test_restricted_command_and_event_only_jobs_are_never_session_jobs(self):
        self.e.set_jobs([job("rest", time="07:30", restricted=True, allowed_tools=["Read"], in_session=False),
                         job("cmd", time="07:30", kind="command", command="true"),
                         job("ev", time="07:30", clock=False, in_session=True, triggers=[{"type": "webhook"}])])
        self.assertEqual(self.ids("2026-10-05T08:00"), [])

    def test_a_both_job_is_session_eligible(self):
        self.e.set_jobs([job("dual", time="07:30", in_session=True)])
        self.assertEqual(self.ids("2026-10-05T08:00"), [("dual", "2026-10-05")])

    def test_day_off_is_not_offered(self):
        self.e.set_jobs([job("weekday-only", time="07:30", days="weekdays", in_session=True, headless=False)])
        self.assertEqual(self.ids("2026-10-03T08:00"), [])                                # Saturday

    def test_dst_fall_back_day_offers_a_0130_job_once(self):
        self.e.set_jobs([job("early", time="01:30", days="daily", in_session=True, headless=False)])
        self.assertEqual(self.ids("2026-11-01T01:45"), [("early", "2026-11-01")])
        d = due_session(self.e, "2026-11-01T01:45")["due"][0]
        self.assertEqual(d["fire_at"], "2026-11-01T01:30:00-04:00")
        self.e.touch(self.e.cfg["state_dir"], "ran-early-2026-11-01")
        self.assertEqual(self.ids("2026-11-01T01:45"), [])


class OneShotsAndTheBeat(unittest.TestCase):
    def test_one_shots_are_session_jobs_only_under_talos(self):
        jobs = [job("remind", once="2026-10-05T09:00", time="09:00", days="daily", in_session=True, headless=False)]
        e = Env(jobs)
        try:
            self.assertEqual(due_session(e, "2026-10-05T09:00")["due"], [])               # session_runner hook: the 0.2 rule stands
        finally:
            e.cleanup()
        e = Env(jobs, session_runner="talos")
        try:
            d = due_session(e, "2026-10-05T09:00")["due"]
            self.assertEqual([(x["id"], x["ds"]) for x in d], [("remind", "2026-10-05")])
            self.assertEqual(d[0]["window_ends"], "2026-10-05T12:00:00-04:00")
            # a one-shot whose window crosses midnight keeps its own date
            d = due_session(e, "2026-10-05T11:59")["due"]
            self.assertEqual(d[0]["ds"], "2026-10-05")
        finally:
            e.cleanup()

    def test_ui_keeps_clearing_in_session_on_one_shots_under_hook_but_not_under_talos(self):
        for runner, expect in (("hook", False), ("talos", True)):
            e = Env([job("remind", once="2030-01-01T15:00", time="15:00", days="daily", in_session=True, headless=False)], session_runner=runner)
            ui = UIServer(e)
            try:
                st, _ = ui.req("POST", "/api/jobs/remind/schedule", {"catchup_min": 90})
                self.assertEqual(st, 200)
                self.assertIs(job_files(e, "remind")[0]["in_session"], expect, runner)
            finally:
                ui.stop()
                e.cleanup()

    def test_the_beat_is_written_by_due_and_by_session_beat_and_ages(self):
        e = Env([job("morning", time="07:30", in_session=True, headless=False)])
        try:
            self.assertIsNone(snap(e)["session_beat_age_s"])
            out = due_session(e, "2026-10-05T07:00")
            self.assertRegex(out["beat"], RFC3339)                                        # due refreshes the heartbeat (S7.4)
            self.assertEqual(snap(e, now="2026-10-05T07:00")["session_beat_age_s"], 0)
            self.assertEqual(snap(e, now="2026-10-05T07:02:30")["session_beat_age_s"], 150)
            out, _ = api(e, ["session-beat"], now="2026-10-05T07:10")
            self.assertTrue(out["ok"])
            self.assertEqual(snap(e, now="2026-10-05T07:10")["session_beat_age_s"], 0)
        finally:
            e.cleanup()

    def test_state_rev_moves_when_the_session_goes_stale_and_when_a_claim_appears(self):
        e = Env([job("morning", time="07:30", in_session=True, headless=False)])
        try:
            api(e, ["session-beat"], now="2026-10-05T07:31")
            fresh = sched(e, "morning", now="2026-10-05T07:32")["state_rev"]
            self.assertEqual(sched(e, "morning", now="2026-10-05T07:33")["state_rev"], fresh)    # under 3 minutes either way: same bucket
            stale = sched(e, "morning", now="2026-10-05T07:40")["state_rev"]
            self.assertNotEqual(fresh, stale)
            os.makedirs(os.path.join(e.cfg["state_dir"], "claim-morning-2026-10-05"))
            self.assertNotEqual(sched(e, "morning", now="2026-10-05T07:40")["state_rev"], stale)
        finally:
            e.cleanup()


class MidnightSnapshot(unittest.TestCase):
    def test_a_2350_job_is_waiting_on_yesterdays_date_at_0005(self):
        e = Env([job("night", time="23:50", days="daily", in_session=True, headless=False)])
        try:
            s = sched(e, "night", now="2026-10-06T00:05")
            self.assertEqual(s["today"], "waiting")
            self.assertEqual(s["window_ends"], "2026-10-06T02:50:00-04:00")
            self.assertEqual(sched(e, "night", now="2026-10-06T02:50")["today"], "pending")   # yesterday's window is over; today's 23:50 hasn't come
        finally:
            e.cleanup()


class DstBothDirections(unittest.TestCase):
    def test_spring_forward_gap_job_is_offered_at_the_first_valid_instant_inside_its_window(self):
        e = Env([job("gap", time="02:30", days="daily", catchup_min=30, in_session=True, headless=False)])
        try:
            self.assertEqual(due_session(e, "2026-03-08T01:59")["due"], [])
            d = due_session(e, "2026-03-08T03:00")["due"]
            self.assertEqual([(x["fire_at"], x["window_ends"]) for x in d], [("2026-03-08T03:00:00-04:00", "2026-03-08T03:30:00-04:00")])
            self.assertEqual(due_session(e, "2026-03-08T03:30")["due"], [])
        finally:
            e.cleanup()

    def test_fall_back_second_hour_has_the_standard_offset_and_the_job_is_still_offered_once(self):
        e = Env([job("early", time="01:30", days="daily", catchup_min=120, in_session=True, headless=False)])
        try:
            s = snap(e, now="2026-11-01T01:45-05:00")
            self.assertEqual(s["as_of"], "2026-11-01T01:45:00-05:00")
            d = due_session(e, "2026-11-01T01:45-05:00")["due"]
            self.assertEqual([(x["id"], x["fire_at"]) for x in d], [("early", "2026-11-01T01:30:00-04:00")])
            self.e = e
        finally:
            e.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2, warnings="ignore")
