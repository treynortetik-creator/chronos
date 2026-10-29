#!/usr/bin/env python3
"""Adversarial review of WP-C1 (0.3.0-rc1). Every test here FAILS on feat/0.3-api at 0dcb40c and passes once its
finding is fixed. Each one drives the real CLI, tick, hook or web UI through the shared test helpers; nothing is mocked.

    python3 tests/test_review_c1.py
"""
import datetime
import json
import os
import subprocess
import sys
import time
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_helpers import *  # noqa: E402,F401,F403

NOW = "2026-10-05T09:00"          # a Monday


def make(e, jid, **kw):
    kw.setdefault("when", {"type": "weekdays", "time": "08:30"})
    kw.setdefault("runner", "headless")
    return apply_ok(e, create_change(jid, **kw))


def edit_prompt(e, jid, extra="\nAlso do something new.\n"):
    open(os.path.join(e.cfg["jobs_dir"], jid, "prompt.md"), "a").write(extra)


def hand_edit_record(e, jid, **fields):
    jobs = json.load(open(e.cfg["jobs_file"]))
    for j in jobs:
        if j["id"] == jid:
            j.update(fields)
    json.dump(jobs, open(e.cfg["jobs_file"], "w"))


# --------------------------------------------------------------------------- R1 (HIGH): the tick spawns session-only jobs
class R1TickHonoursHeadlessFalse(unittest.TestCase):
    """S2.1: runner `session` = {in_session:true, headless:false}: "Offered to the live session only. The tick never spawns it."
    `chronos api apply` creates exactly that record (runner defaults to session), but due_jobs never reads `headless`, so the
    launchd tick spawns `claude -p` for it after grace_min: the automatic headless fallback owner decision (a) rejected."""

    def test_due_never_lists_a_session_only_job(self):
        e = Env([job("sess", in_session=True, headless=False, time="08:30"), job("hl", time="08:30")])
        try:
            r, lines = e.due(NOW)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual([l.split("|")[0] for l in lines], ["hl"])
        finally:
            e.cleanup()

    def test_the_tick_does_not_run_an_api_created_session_job(self):
        e = Env([])
        try:
            apply_ok(e, create_change("sess", when={"type": "weekdays", "time": "08:30"}))      # runner: session (the default)
            r = e.run(["/bin/bash", TICK], CHRONOS_NOW=NOW)
            self.assertEqual(r.returncode, 0, r.stderr)
            time.sleep(3)
            st = os.listdir(e.cfg["state_dir"])
            self.assertFalse([n for n in st if n.startswith(("claim-sess", "ran-sess", "failed-sess"))], st)
        finally:
            e.cleanup()


# --------------------------------------------------------------------------- R2 (HIGH): an API update un-holds an outside change
class R2UpdateOnAHeldJobDoesNotApproveTheOutsideChange(unittest.TestCase):
    """With approval_required on, a hand-edited prompt holds the job. Any `apply` update (a low-risk rename, S8.2: "can only narrow
    or move behaviour the owner already approved") then writes a fresh approved record over the TAMPERED prompt, so the hold is
    gone and the never-reviewed text runs. The UI already refuses this (approve_after_edit/was_clean); the API path does not.
    Expected: the update is refused (held / schedule_conflict, pointing at adopt) or leaves the job held."""

    def test_rename_of_a_held_job_leaves_it_held(self):
        e = Env([], approval_required=True)
        try:
            make(e, "alpha")
            edit_prompt(e, "alpha")
            s = sched(e, "alpha", now=NOW)
            self.assertEqual(s["today"], "held")
            change = {"op": "update", "id": "alpha", "base_etag": s["etag"], "set": {"name": "Renamed"}}
            env = e.env(CHRONOS_NOW=NOW, TZ=NY)
            v = subprocess.run([sys.executable, CLI, "api", "validate"], env=env, input=json.dumps(change), capture_output=True, text=True)
            if v.returncode == 0:
                vv = json.loads(v.stdout)
                subprocess.run([sys.executable, CLI, "api", "apply"], env=env, capture_output=True, text=True,
                               input=json.dumps({"change": vv["normalized"], "expected_etag": vv["expected_etag"]}))
            self.assertEqual(sched(e, "alpha", now=NOW)["today"], "held", "a rename approved a prompt nobody reviewed")
            self.assertEqual(due_ids(e), [])
        finally:
            e.cleanup()


def due_ids(e, now=NOW):
    r, lines = e.due(now)
    return [l.split("|")[0] for l in lines]


# --------------------------------------------------------------------------- R3 (MEDIUM): spring-forward gap eats the window
class R3SpringForwardFiresAtTheNextValidMinute(unittest.TestCase):
    """S7.5: "A local time that doesn't exist (spring forward) fires at the next valid minute." next_run says 03:00-04:00, but
    day_state/due_jobs do naive wall-clock arithmetic from 02:30, so a window of 60 min or less ends before 03:00 ever exists:
    the job is `missed` and never fires on the spring-forward day."""

    def test_session_due_offers_a_0230_job_at_0310(self):
        e = Env([job("gap", time="02:30", days="daily", catchup_min=30, in_session=True, headless=False)])
        try:
            out, _ = api(e, ["due", "--for", "session"], now="2026-03-08T03:10", tz=NY)
            self.assertEqual([(d["id"], d["fire_at"]) for d in out["due"]], [("gap", "2026-03-08T03:00:00-04:00")])
        finally:
            e.cleanup()

    def test_snapshot_is_waiting_not_missed(self):
        e = Env([job("gap", time="02:30", days="daily", catchup_min=30)])
        try:
            self.assertEqual(sched(e, "gap", now="2026-03-08T03:10", tz=NY)["today"], "waiting")
        finally:
            e.cleanup()

    def test_the_tick_runs_it(self):
        e = Env([job("gap", time="02:30", days="daily", catchup_min=30)], grace_min=10)
        try:
            r, lines = e.due("2026-03-08T03:15", TZ=NY)
            self.assertEqual(lines, ["gap|2026-03-08"])
        finally:
            e.cleanup()


# --------------------------------------------------------------------------- R4 (MEDIUM): the repeated fall-back hour
class R4RepeatedHourInstants(unittest.TestCase):
    """now() is naive and localize() forces fold=0, so every instant in the second 01:00-01:59 (EST) is printed with the EDT
    offset, one hour in the past, and ages computed across the change are off by an hour."""

    def test_as_of_in_the_second_occurrence_has_the_standard_offset(self):
        e = Env([job("alpha")])
        try:
            self.assertEqual(snap(e, now="2026-11-01T01:45-05:00", tz=NY)["as_of"], "2026-11-01T01:45:00-05:00")
        finally:
            e.cleanup()

    def test_session_beat_age_across_the_fall_back(self):
        e = Env([job("alpha")])
        try:
            api(e, ["session-beat"], now="2026-11-01T01:50-04:00", tz=NY)
            age = snap(e, now="2026-11-01T01:10-05:00", tz=NY)["session_beat_age_s"]       # 20 real minutes later
            self.assertEqual(age, 20 * 60)
        finally:
            e.cleanup()


# --------------------------------------------------------------------------- R5 (MEDIUM): the web UI can still mint `both` jobs under talos
class R5NoBothJobsUnderTalos(unittest.TestCase):
    """N9/S3.7: session_runner:talos is refused while any `both` job exists, because a `both` job runs in the session AND headless
    after grace. But after the switch, the web UI's create (and its schedule save) still writes in_session:true with no
    `headless` key, which is `both`, so the invariant the config switch enforced is broken from the side."""

    def test_ui_create_with_in_session_under_talos_is_not_both(self):
        e = Env([], session_runner="talos")
        ui = UIServer(e)
        try:
            st, r = ui.req("POST", "/api/jobs", {"name": "UI Sess", "id": "ui-sess", "prompt": "Hello", "time": "08:30",
                                                 "days": "weekdays", "in_session": True})
            if st == 201:
                self.assertNotEqual(sched(e, "ui-sess")["runner"], "both")
        finally:
            ui.stop()
            e.cleanup()


# --------------------------------------------------------------------------- R6 (MEDIUM): UI approvals not bound to what was shown
class R6UIApprovalsAreBound(unittest.TestCase):
    """The hold exists because something with file access (the session's own agent has Bash) may rewrite prompt.md or jobs.json.
    (a) "Approve as it is on disk" posts {} and approves whatever is on disk at click time, not the content the owner looked at
    (the API's adopt is bound to a content_hash, N7; the UI's is not). (b) A UI prompt save approves the WHOLE job, including
    settings that were hand-edited and never shown, while the other UI saves (schedule/model/triggers) correctly do not."""

    def setUp(self):
        self.e = Env([], approval_required=True)
        make(self.e, "alpha")
        self.ui = UIServer(self.e)

    def tearDown(self):
        self.ui.stop()
        self.e.cleanup()

    def test_approve_with_a_stale_content_hash_is_refused(self):
        edit_prompt(self.e, "alpha")
        seen = sched(self.e, "alpha")["content_hash"]                  # what the owner's screen showed
        edit_prompt(self.e, "alpha", "\nAnd one more thing nobody saw.\n")
        st, r = self.ui.req("POST", "/api/jobs/alpha/approve", {"content_hash": seen})
        self.assertEqual(st, 409, r)
        self.assertEqual(sched(self.e, "alpha")["today"], "held")

    def test_a_ui_prompt_save_does_not_approve_hand_edited_settings(self):
        hand_edit_record(self.e, "alpha", model="opus", allowed_tools=["Bash(curl:*)"])
        self.assertEqual(sched(self.e, "alpha")["today"], "held")
        st, r = self.ui.req("POST", "/api/jobs/alpha/prompt", {"content": "A new prompt.\n", "base_sha": None})
        self.assertEqual(st, 200, r)
        self.assertEqual(sched(self.e, "alpha")["today"], "held", "a prompt save approved a model + tool change it never showed")


# --------------------------------------------------------------------------- R7 (LOW-MED): ids with a trailing newline
class R7IdsAreFullMatched(unittest.TestCase):
    """ID_RE uses `$`, which matches before a trailing newline. `apply` accepts id "alpha\\n" and creates jobs/alpha\\n/. The
    tick reads `chronos due` line by line, so a due "alpha\\n|<ds>" becomes the line "alpha": the tick claims and runs a
    DIFFERENT job, with an empty ds."""

    def test_validate_refuses_an_id_with_a_trailing_newline(self):
        e = Env([])
        try:
            out, r = api(e, ["validate"], create_change("alpha\n"), expect=1)
            self.assertEqual(out["error"]["code"], "schedule_invalid")
        finally:
            e.cleanup()


# --------------------------------------------------------------------------- R8 (LOW): an invalid TZ mislabels every instant
class R8InvalidTZ(unittest.TestCase):
    """zone_name() ignores an invalid $TZ and labels times with /etc/localtime's zone, but libc (datetime.now()) treats the same
    invalid TZ as UTC. The printed instant is then wrong by the zone's offset."""

    def test_as_of_is_the_real_instant_under_an_invalid_tz(self):
        e = Env([job("alpha")])
        try:
            env = e.env()
            env.pop("CHRONOS_NOW", None)
            env["TZ"] = "Not/AZone"
            r = subprocess.run([sys.executable, CLI, "api", "snapshot"], env=env, capture_output=True, text=True)
            as_of = datetime.datetime.fromisoformat(json.loads(r.stdout)["as_of"])
            self.assertLess(abs(as_of.timestamp() - time.time()), 30, as_of.isoformat())
        finally:
            e.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2, warnings="ignore")
