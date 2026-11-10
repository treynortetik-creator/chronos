#!/usr/bin/env python3
"""Gap tests (2026-11 audit): paths the older suites did not reach. Throwaway homes, fake clock, fake `claude`, no launchctl.

    python3 tests/test_gaps.py        (also run by tests/run.sh)
"""
import json
import shutil
import tempfile
import os
import socket
import subprocess
import sys
import time
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_chronos import Env, job, event_job, macos_shims, C, CLI, TICK, RUN, FAKE, ROOT, INSTALL  # noqa: E402,F401
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


class CliErrors(unittest.TestCase):
    def test_prompt_for_an_unknown_job_is_one_line_on_stderr_not_a_traceback(self):
        e = Env([job("alpha")])
        self.addCleanup(e.cleanup)
        r = e.run([sys.executable, CLI, "prompt", "no-such-job"])
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(r.stdout, "")


class WebhookQueueFull(unittest.TestCase):
    def test_a_full_queue_says_the_queue_is_full_not_the_hourly_rate(self):
        e = Env([event_job("hooked", [{"type": "webhook"}])])
        self.addCleanup(e.cleanup)
        ui = UIServer(e)
        self.addCleanup(ui.stop)
        secret = TR.ensure_secret(e.cfg, "hooked")
        for _ in range(TR.QUEUE_CAP):
            TR.enqueue_webhook(e.cfg, "hooked", "{}")
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", ui.port, timeout=10)
        c.request("POST", "/hook/hooked", body=b"{}", headers={"Host": "127.0.0.1:%d" % ui.port, "X-Chronos-Secret": secret})
        r = c.getresponse()
        body = json.loads(r.read())
        c.close()
        self.assertEqual(r.status, 429)
        self.assertIn("queue", body["error"])
        self.assertNotIn("per hour", body["error"])


class NotifyQuoting(unittest.TestCase):
    def test_a_message_with_shell_syntax_reaches_the_notify_command_as_one_literal_argument(self):
        e = Env([job("alpha")])
        self.addCleanup(e.cleanup)
        pwned = os.path.join(e.tmp, "pwned")
        msg = 'a "q" $(touch %s) `touch %s` ; touch %s' % (pwned, pwned, pwned)
        r = e.run(["/bin/bash", os.path.join(ROOT, "bin", "chronos-notify"), msg])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(e.wait_for(lambda: os.path.exists(e.notify_file), 10))
        self.assertEqual(open(e.notify_file).read(), msg + "\n")
        self.assertFalse(os.path.exists(pwned))

    def test_no_notify_command_is_a_silent_no_op(self):
        e = Env([job("alpha")], notify=False)
        self.addCleanup(e.cleanup)
        r = e.run(["/bin/bash", os.path.join(ROOT, "bin", "chronos-notify"), "hello"])
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))


class RunWrapperInputs(unittest.TestCase):
    """chronos-run.sh builds file names from its arguments; anything outside the allowed shapes must stop it before it touches state."""

    def test_bad_ids_dates_and_hashes_are_refused(self):
        e = Env([job("alpha")])
        self.addCleanup(e.cleanup)
        before = sorted(os.listdir(e.cfg["state_dir"]))
        for args, want in ((["../x", D], "bad job id"), (["alpha", "2026-10-05/../.."], "bad date"), (["Alpha", D], "bad job id"),
                           (["alpha", D, "/etc/hosts", "XYZ", "file"], "bad event hash"), (["alpha", D, "/etc/hosts", "abc123", "File"], "bad event type")):
            r = e.run(["/bin/bash", RUN] + args)
            self.assertEqual(r.returncode, 1, args)
            self.assertIn(want, r.stdout, args)
        self.assertEqual(sorted(os.listdir(e.cfg["state_dir"])), before)
        self.assertFalse(os.path.exists(e.fake_log))

    def test_a_relaunch_after_success_never_reruns_and_releases_the_claim(self):
        e = Env([job("alpha")])
        self.addCleanup(e.cleanup)
        e.touch(state(e, "ran-alpha-" + D))
        os.mkdir(state(e, "claim-alpha-" + D))
        r = e.run(["/bin/bash", RUN, "alpha", D])
        self.assertIn("SKIP alpha already ran", r.stdout)
        self.assertFalse(os.path.isdir(state(e, "claim-alpha-" + D)))
        self.assertFalse(os.path.exists(e.fake_log))


class TickGates(unittest.TestCase):
    def test_dry_run_prints_the_due_job_and_claims_spawns_and_beats_nothing(self):
        e = Env([job("alpha")])
        self.addCleanup(e.cleanup)
        r = e.run(["/bin/bash", TICK], CHRONOS_NOW=D + "T09:30", CHRONOS_DRYRUN="1")
        self.assertIn("DRYRUN would claim alpha (%s)" % D, r.stdout)
        self.assertEqual([n for n in os.listdir(e.cfg["state_dir"]) if n.startswith("claim-")], [])
        self.assertFalse(os.path.exists(os.path.join(e.cfg["home_dir"], "chronos.beat")))
        time.sleep(1)
        self.assertFalse(os.path.exists(e.fake_log))

    def test_an_unparseable_jobs_file_logs_a_tick_error_and_exits_0(self):
        e = Env([job("alpha")])
        self.addCleanup(e.cleanup)
        open(e.cfg["jobs_file"], "w").write("[{not json")
        r = e.run(["/bin/bash", TICK], CHRONOS_NOW=D + "T09:30")
        self.assertEqual(r.returncode, 0)
        self.assertIn("TICK-ERROR jobs file unreadable", open(os.path.join(e.cfg["logs_dir"], "chronos.log")).read())
        self.assertEqual([n for n in os.listdir(e.cfg["state_dir"]) if n.startswith("claim-")], [])

    def test_one_invalid_record_does_not_stop_the_others(self):
        e = Env([job("alpha"), job("bad", notify="sometimes"), job("beta")])
        self.addCleanup(e.cleanup)
        r, lines = e.due(D + "T09:30")
        self.assertEqual(sorted(lines), ["alpha|" + D, "beta|" + D])
        self.assertIn("skip job 'bad'", r.stderr)

    def test_a_catchup_window_shorter_than_grace_is_refused_by_validation(self):
        clean, errors = C.validate_schedule({"time": "09:00", "days": "daily", "catchup_min": 5})
        self.assertTrue(errors)
        clean, errors = C.validate_schedule({"time": "09:00", "days": "daily", "catchup_min": 1441})
        self.assertTrue(errors)


class InstallerRerun(unittest.TestCase):
    def test_a_second_install_keeps_config_jobs_and_token_and_force_config_rewrites_only_the_config(self):
        home = tempfile.mkdtemp(prefix="chronos-home-")
        self.addCleanup(shutil.rmtree, home, True)
        env = dict(os.environ, HOME=home)
        for k in ("CHRONOS_CONFIG", "CHRONOS_LAUNCHAGENTS_DIR"):
            env.pop(k, None)
        env = macos_shims(env, home)

        def install(*extra):
            r = subprocess.run(["/bin/bash", INSTALL, "--no-load", "--workspace", os.path.join(home, "ws")] + list(extra),
                               env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            return r.stdout
        install()
        cfgp = os.path.join(home, ".config", "chronos", "config.json")
        jobsp = os.path.join(home, ".config", "chronos", "jobs.json")
        tokp = os.path.join(home, ".chronos", "ui-token")
        c = json.load(open(cfgp))
        c["grace_min"] = 3
        json.dump(c, open(cfgp, "w"))
        open(jobsp, "w").write('[{"id": "mine"}]')
        tok = open(tokp).read()
        out = install("--port", "4800")
        self.assertIn("kept existing", out)
        self.assertEqual(json.load(open(cfgp))["grace_min"], 3)          # a re-run never overwrites your config...
        self.assertEqual(json.load(open(cfgp))["ui_port"], 4747)         # ...not even the port you pass
        self.assertEqual(open(jobsp).read(), '[{"id": "mine"}]')
        self.assertEqual(open(tokp).read(), tok)
        install("--force-config", "--port", "4800")
        self.assertEqual(json.load(open(cfgp))["ui_port"], 4800)
        self.assertEqual(open(jobsp).read(), '[{"id": "mine"}]')         # --force-config is the config only
        self.assertEqual(open(tokp).read(), tok)

    def test_a_bad_option_or_port_stops_before_writing_anything(self):
        home = tempfile.mkdtemp(prefix="chronos-home-")
        self.addCleanup(shutil.rmtree, home, True)
        env = macos_shims(dict(os.environ, HOME=home), home)
        env.pop("CHRONOS_CONFIG", None)
        for args in (["--port", "47x"], ["--nope"]):
            r = subprocess.run(["/bin/bash", INSTALL, "--no-load"] + args, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 2, args)
        self.assertFalse(os.path.exists(os.path.join(home, ".chronos")))
        self.assertFalse(os.path.exists(os.path.join(home, ".config")))


if __name__ == "__main__":
    unittest.main(verbosity=1)
