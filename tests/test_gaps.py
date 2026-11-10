#!/usr/bin/env python3
"""Gap tests (2026-11 audit): paths the older suites did not reach. Throwaway homes, fake clock, fake `claude`, no launchctl.

    python3 tests/test_gaps.py        (also run by tests/run.sh)
"""
import json
import os
import socket
import subprocess
import sys
import time
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_chronos import Env, job, event_job, C, CLI, TICK, RUN, FAKE, ROOT  # noqa: E402,F401
from api_helpers import api, UIServer, NY  # noqa: E402
import chronos_triggers as TR  # noqa: E402

D = "2026-10-05"          # a Monday


def state(e, name):
    return os.path.join(e.cfg["state_dir"], name)


def settled(e, jid, ds=D):
    return lambda: ((os.path.exists(state(e, "ran-%s-%s" % (jid, ds))) or os.path.exists(state(e, "failed-%s-%s" % (jid, ds))))
                    and not os.path.isdir(state(e, "claim-%s-%s" % (jid, ds))))


class RunWrapperWatchdog(unittest.TestCase):
    def test_a_timed_out_command_that_ignores_sigterm_is_killed_and_releases_its_claim(self):
        # timeout_min 0.03 is about 2 s. The command ignores TERM and would sleep 90 s, holding the claim all that time.
        e = Env([job("cmd", kind="command", command="trap '' TERM; sleep 90")], timeout_min=0.03)
        self.addCleanup(e.cleanup)
        e.run(["/bin/bash", TICK], CHRONOS_NOW=D + "T09:30")
        t0 = time.time()
        self.assertTrue(e.wait_for(settled(e, "cmd"), 30), "the claim was still held 30 s after the timeout")
        self.assertLess(time.time() - t0, 30)
        self.assertTrue(os.path.exists(state(e, "failed-cmd-" + D)))
        st = json.load(open(state(e, "run-cmd-%s.json" % D)))
        self.assertEqual(st["status"], "timed_out")


def raw_http(port, head, timeout=5):
    """Send raw request bytes, keep the socket open, return the status line (or None if no answer came within `timeout`)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(head)
        try:
            data = s.recv(200)
        except socket.timeout:
            return None
        return data.split(b"\r\n", 1)[0].decode()
    finally:
        s.close()


class UIBodyLength(unittest.TestCase):
    def setUp(self):
        self.e = Env([event_job("hooked", [{"type": "webhook"}])])
        self.addCleanup(self.e.cleanup)
        self.ui = UIServer(self.e)
        self.addCleanup(self.ui.stop)

    def test_webhook_with_a_negative_content_length_is_refused_not_left_hanging(self):
        secret = TR.ensure_secret(self.e.cfg, "hooked")
        head = ("POST /hook/hooked HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-Chronos-Secret: %s\r\nContent-Length: -1\r\n\r\n"
                % (self.ui.port, secret)).encode()
        self.assertEqual(raw_http(self.ui.port, head), "HTTP/1.0 400 Bad Request")

    def test_ui_post_with_a_negative_content_length_is_refused_not_left_hanging(self):
        head = ("POST /api/pause-all HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-Chronos-Token: %s\r\nContent-Length: -1\r\n\r\n"
                % (self.ui.port, self.ui.token)).encode()
        self.assertEqual(raw_http(self.ui.port, head), "HTTP/1.0 400 Bad Request")
        self.assertFalse(os.path.exists(self.e.cfg["pause_all"]))


class TrailingNewlines(unittest.TestCase):
    """`$` in a Python regex also matches before a final newline. Ids already use \\Z (R7); run ids, dates and models must too."""

    def setUp(self):
        self.e = Env([job("morning", time="07:30", in_session=True, headless=False)])
        self.addCleanup(self.e.cleanup)

    def claim(self, **kw):
        r = {"run_id": "r_" + "0" * 25 + "1", "ds": D, "fire_at": D + "T07:30:00-04:00", "manual": False, "again": False,
             "runner": "session", "bridge_instance": "inst-a"}
        r.update(kw)
        return api(self.e, ["claim", "morning"], r, now=D + "T07:45", expect=1)[0]

    def test_api_claim_refuses_a_run_id_with_a_trailing_newline(self):
        out = self.claim(run_id="r_" + "0" * 25 + "1\n")
        self.assertEqual(out["error"]["code"], "bad_request")
        self.assertEqual([n for n in os.listdir(self.e.cfg["state_dir"]) if n.startswith("claim-")], [])

    def test_a_model_with_a_trailing_newline_is_invalid_and_never_reaches_claude(self):
        j = job("ab", model="opus\n")
        with self.assertRaises(ValueError):
            C.validate_job_record(j)
        self.assertEqual(C.job_model(self.e.cfg, j), "")

    def test_cli_claim_with_a_date_and_a_trailing_newline_is_not_a_new_key(self):
        self.e.set_jobs([job("plain", time="07:30")])
        r = self.e.run([sys.executable, CLI, "claim", "plain", D + "\n"], CHRONOS_NOW=D + "T07:45")
        names = [n for n in os.listdir(self.e.cfg["state_dir"]) if n.startswith("claim-plain")]
        self.assertEqual(names, ["claim-plain-" + D], (r.stdout, names))


if __name__ == "__main__":
    unittest.main(verbosity=1)
