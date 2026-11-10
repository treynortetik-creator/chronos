#!/usr/bin/env python3
"""Regression tests for the 2026-11-10 security and correctness audit (docs/audits/2026-11-10-security.md).
Each class names the finding it pins. Everything runs against a throwaway home with a fake `claude`."""
import http.client
import json
import os
import plistlib
import socket
import stat
import subprocess
import sys
import time
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_helpers import *  # noqa: E402,F401,F403
from test_chronos import free_port  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "ui"))
import chronos_runlog as RL  # noqa: E402
import chronos_triggers as TR  # noqa: E402
import chronos_write as W  # noqa: E402
import control_room as CR  # noqa: E402
import agent_files as AF  # noqa: E402

MON = C.datetime.datetime(2026, 10, 5, 9, 30)      # a Monday, inside alpha's window (09:00 + 180)


def old(path, age=120):
    t = time.time() - age
    os.utime(path, (t, t))


class TriggerHitsSurviveADeadline(unittest.TestCase):
    """A-01: the tick threw away EVERY hit of the trigger pass when the pass was killed at its deadline, although the hits printed
    before the kill had already been marked as seen (state saved, webhook queue file deleted). Those events never ran."""

    def setUp(self):
        self.e = Env([], trigger_deadline_s=3)
        t = self.e.tmp
        self.watch = os.path.join(t, "watch")
        os.makedirs(self.watch)
        self.flag = os.path.join(t, "slow.flag")
        self.adapter = os.path.join(t, "gmail.sh")
        with open(self.adapter, "w") as fh:
            fh.write('#!/bin/bash\n[ -e "%s" ] && sleep 30\necho \'{"emails": []}\'\n' % self.flag)
        os.chmod(self.adapter, 0o755)
        self.e.set_jobs([job("fwatch", clock=False, triggers=[{"type": "file", "path": self.watch, "glob": "*.txt"}]),
                         job("slowmail", clock=False, triggers=[{"type": "gmail", "query": "from:x"}])])

    def tearDown(self):
        subprocess.run(["pkill", "-f", self.adapter], capture_output=True)
        self.e.cleanup()

    def test_hits_printed_before_the_deadline_still_run(self):
        r = self.e.run(["/bin/bash", TICK], CHRONOS_GMAIL_COMMAND=self.adapter)          # tick 1: baselines only
        self.assertEqual(r.returncode, 0, r.stderr)
        sp = os.path.join(self.e.cfg["trigger_state_dir"], "slowmail.json")
        st = json.load(open(sp))
        for ts in st["triggers"].values():
            ts["last_poll"] = 0                                                       # the mail poll is due again
        json.dump(st, open(sp, "w"))
        f = os.path.join(self.watch, "new.txt")
        open(f, "w").write("x")
        old(f)
        open(self.flag, "w").close()                                                  # the mail adapter now hangs past the 3 s deadline
        r = self.e.run(["/bin/bash", TICK], CHRONOS_GMAIL_COMMAND=self.adapter)
        self.assertEqual(r.returncode, 0, r.stderr)
        log = open(os.path.join(self.e.cfg["logs_dir"], "chronos.log")).read()
        self.assertIn("DEADLINE triggers", log)                                       # the pass really was killed
        self.assertIn("CLAIMED fwatch TRIGGER file", log)                             # and the file event it had already announced still ran
        self.assertTrue(self.e.wait_for(lambda: bool(C.event_ran(self.e.cfg, "fwatch", C.day_str(C.now().date())))))


class RunNowKeepsMarkersWhenItCannotClaim(unittest.TestCase):
    """A-02: `chronos run --again` (and the UI's Run now) deleted the done / failed markers BEFORE taking the claim. Refused because a
    run still held the claim, it left the day with no marker, so the tick ran the job a second time."""

    def setUp(self):
        self.e = Env([job("alpha")])
        self.ds = C.day_str(C.now().date())
        self.st = self.e.cfg["state_dir"]
        os.mkdir(os.path.join(self.st, "claim-alpha-" + self.ds))                     # a run that is finishing still holds the claim

    def tearDown(self):
        self.e.cleanup()

    def test_a_refused_run_again_keeps_the_done_marker(self):
        ran = self.e.touch(self.st, "ran-alpha-" + self.ds)
        with self.assertRaises(C.RunError):
            C.start_run(self.e.cfg, "alpha", again=True)
        self.assertTrue(os.path.exists(ran))

    def test_a_refused_run_keeps_the_failed_marker(self):
        failed = self.e.touch(self.st, "failed-alpha-" + self.ds)
        with self.assertRaises(C.RunError):
            C.start_run(self.e.cfg, "alpha")
        self.assertTrue(os.path.exists(failed))


class UnattendedClaimHonoursPause(unittest.TestCase):
    """A-03: an in-session timer starts with `chronos claim <id> --unattended`, which never looked at the pause switches, so a paused
    job (or Pause all) still ran in a live session."""

    def setUp(self):
        self.e = Env([job("alpha", in_session=True)])

    def tearDown(self):
        self.e.cleanup()

    def test_a_paused_job_is_not_due_for_a_timer(self):
        self.e.touch(self.e.cfg["paused_dir"], "alpha")
        self.assertEqual(C.claim(self.e.cfg, "alpha", unattended=True, at=MON), "NOT_DUE")
        self.assertFalse(os.path.exists(C.claim_path(self.e.cfg, "alpha", "2026-10-05")))

    def test_pause_all_stops_a_timer_too(self):
        self.e.touch(self.e.cfg["pause_all"])
        self.assertEqual(C.claim(self.e.cfg, "alpha", unattended=True, at=MON), "NOT_DUE")

    def test_a_person_may_still_claim_a_paused_job_by_hand(self):
        self.e.touch(self.e.cfg["paused_dir"], "alpha")
        self.assertEqual(C.claim(self.e.cfg, "alpha", unattended=False, at=MON), "CLAIMED")


class FileTriggerOverTheCap(unittest.TestCase):
    """A-04: the file trigger remembers 5,000 files. In a folder with more, every file past the cap looked new on every check, so
    old files were replayed as events (6 an hour, for ever)."""

    def setUp(self):
        self.e = Env([])
        self.watch = os.path.join(self.e.tmp, "big")
        os.makedirs(self.watch)
        t = time.time() - 600
        for i in range(5003):
            p = os.path.join(self.watch, "f%05d.txt" % i)
            open(p, "w").close()
            os.utime(p, (t, t))

    def tearDown(self):
        self.e.cleanup()

    def test_files_past_the_cap_never_fire_as_new(self):
        t = TR.validate_trigger({"type": "file", "path": self.watch, "glob": "*.txt"})
        now = time.time()
        try:
            events, ts, _info = TR.eval_file(self.e.cfg, t, {}, now)                  # the baseline
            events, ts, _info = TR.eval_file(self.e.cfg, t, ts, now + 300)            # nothing changed since
        except TR.TriggerError:
            events = []                                                               # refusing the folder is fine; replaying it is not
        self.assertEqual([e["summary"] for e in events], [])


class BrokenConfigFailsClosed(unittest.TestCase):
    """A-05 / A-06: a config.json with a syntax error silently became the defaults (--dangerously-skip-permissions, approval_required off,
    no notify), and `chronos api config set` then replaced the whole file with only the keys it was given."""

    def setUp(self):
        self.e = Env([job("alpha")], approval_required=True, claude_args=["--permission-mode", "acceptEdits"])
        self.good = open(self.e.cfg_path).read()
        self.bad = self.good.rstrip()[:-1] + ',\n  "notify": \n}'                    # a half-finished hand edit
        open(self.e.cfg_path, "w").write(self.bad)

    def tearDown(self):
        self.e.cleanup()

    def test_load_config_refuses_a_file_it_cannot_parse(self):
        with self.assertRaises(RuntimeError):
            C.load_config()

    def test_the_tick_starts_nothing_on_a_broken_config(self):
        r = self.e.run([sys.executable, CLI, "due"], CHRONOS_NOW="2026-10-05T09:30")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")
        r = self.e.run([sys.executable, CLI, "env"])
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("dangerously-skip-permissions", r.stdout)

    def test_config_set_does_not_replace_a_file_it_cannot_parse(self):
        with self.assertRaises(W.WriteError):
            W.set_config(self.e.cfg, {"session_wait_notify_min": 5})
        self.assertEqual(open(self.e.cfg_path).read(), self.bad)


class TickFilesArePrivate(unittest.TestCase):
    """A-07: the tick had no umask, so on a hand-made install (no install.sh) the state and log folders and chronos.log were world-readable."""

    def setUp(self):
        self.e = Env([job("alpha")])
        fresh = os.path.join(self.e.tmp, "fresh")
        c = json.load(open(self.e.cfg_path))
        c.update(state_dir=os.path.join(fresh, "state"), logs_dir=os.path.join(fresh, "logs"), home_dir=fresh)
        json.dump(c, open(self.e.cfg_path, "w"))
        self.fresh = fresh

    def tearDown(self):
        self.e.cleanup()

    def test_folders_and_the_log_have_no_group_or_other_bits(self):
        r = self.e.run(["/bin/bash", "-c", 'umask 022; exec /bin/bash "$0"', TICK], CHRONOS_NOW="2026-10-05T08:00")
        self.assertEqual(r.returncode, 0, r.stderr)
        for p in (self.fresh, os.path.join(self.fresh, "state"), os.path.join(self.fresh, "logs"), os.path.join(self.fresh, "logs", "chronos.log"),
                  os.path.join(self.fresh, "chronos.beat")):
            self.assertTrue(os.path.exists(p), p)
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode) & 0o077, 0, "%s is %o" % (p, stat.S_IMODE(os.stat(p).st_mode)))


class KeysEndWhereTheyEnd(unittest.TestCase):
    """A-08: `$` in a Python regex also matches before a trailing newline, so "2026-10-05\\n" passed as a run key and became part of a file name."""

    def setUp(self):
        self.e = Env([job("morning", time="07:30", in_session=True, headless=False)])

    def tearDown(self):
        self.e.cleanup()

    def test_patterns_refuse_a_trailing_newline(self):
        self.assertIsNone(RL.DS_RE.match("2026-10-05\n"))
        self.assertIsNone(RL.RUN_ID_RE.match("r_" + "0" * 26 + "\n"))
        self.assertIsNone(C.MODEL_RE.match("opus\n"))
        self.assertIsNone(C.TOOL_RE.match("Read\n"))
        self.assertIsNone(TR.REPO_RE.match("owner/name\n"))

    def test_api_claim_refuses_a_key_with_a_newline(self):
        req = {"run_id": "r_" + "0" * 25 + "1", "ds": "2026-10-05\n", "manual": False, "again": False, "runner": "session", "bridge_instance": "i"}
        out, _r = api(self.e, ["claim", "morning"], req, now="2026-10-05T07:45", expect=1)
        self.assertEqual(out["error"]["code"], "bad_request")
        self.assertEqual([n for n in os.listdir(self.e.cfg["state_dir"]) if "\n" in n], [])

    def test_cli_claim_never_builds_a_marker_name_with_a_newline(self):
        C.claim(self.e.cfg, "morning", at=C.datetime.datetime(2026, 10, 5, 7, 45), ds="2026-10-05\n")
        self.assertEqual([n for n in os.listdir(self.e.cfg["state_dir"]) if "\n" in n], [])


class ControlRoomShowsNoCredentials(unittest.TestCase):
    """A-09: GET /api/control needs no token. An MCP server URL with credentials in it (https://token@host/) was shown as its "target"."""

    def setUp(self):
        self.e = Env([])

    def tearDown(self):
        self.e.cleanup()

    def test_userinfo_in_an_mcp_url_is_never_returned(self):
        ws = self.e.cfg["workspace"]
        at = "@"                    # (built in two parts: the repo's hygiene test reads a credential in a URL as an email address)
        json.dump({"mcpServers": {"a": {"url": "https://s3cr3t-token" + at + "mcp.example.test/sse"},
                                  "b": {"url": "https://user:s3cr3t-pass" + at + "mcp2.example.test:8443/x"}}}, open(os.path.join(ws, ".mcp.json"), "w"))
        _plugins, mcp = CR.plugins_and_mcp(ws, self.e.cfg["claude_home"])
        self.assertEqual(sorted(m["target"] for m in mcp), ["mcp.example.test", "mcp2.example.test"])
        self.assertNotIn("s3cr3t", json.dumps(CR.collect(self.e.cfg)))


class ExcludeGlobsIgnoreCase(unittest.TestCase):
    """A-10: macOS folders are case-insensitive, the exclude globs were not: `"agent_files_exclude": ["private"]` did not hide Private/."""

    def setUp(self):
        self.e = Env([], agent_files_exclude=["private", "notes/secret-*.md"],
                     agent_files_extra=[{"glob": "Private/*.md", "category": "Notes"}, {"glob": "notes/*.md", "category": "Notes"}])
        ws = self.e.cfg["workspace"]
        for rel in ("Private/diary.md", "notes/Secret-plan.md", "notes/todo.md"):
            os.makedirs(os.path.dirname(os.path.join(ws, rel)), exist_ok=True)
            open(os.path.join(ws, rel), "w").write("# x\n")

    def tearDown(self):
        self.e.cleanup()

    def test_an_excluded_folder_is_hidden_whatever_its_case(self):
        labels = [f["label"] for f in AF.enumerate_files(AF.Cfg.from_config(self.e.cfg))]
        self.assertIn("notes/todo.md", labels)
        self.assertNotIn("Private/diary.md", labels)
        self.assertNotIn("notes/Secret-plan.md", labels)


class NegativeContentLength(unittest.TestCase):
    """A-11: Content-Length: -1 passed the size check and became rfile.read(-1): the request thread read until the client hung up,
    with no limit on what it buffered."""

    @classmethod
    def setUpClass(cls):
        cls.e = Env([job("alpha")])
        cls.port = free_port()
        cls.proc = subprocess.Popen([sys.executable, SERVER], env=cls.e.env(CHRONOS_UI_PORT=str(cls.port)), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        end = time.time() + 10
        while time.time() < end:
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.3).close()
                break
            except OSError:
                time.sleep(0.1)
        cls.token = open(cls.e.cfg["token_file"]).read().strip()

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=5)
        cls.e.cleanup()

    def test_a_negative_length_is_refused_at_once(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        c.putrequest("POST", "/api/pause-all", skip_host=True)
        c.putheader("Host", "127.0.0.1:%d" % self.port)
        c.putheader("X-Chronos-Token", self.token)
        c.putheader("Content-Length", "-1")
        c.endheaders()
        try:
            status = c.getresponse().status
        except (socket.timeout, TimeoutError):
            status = None                                                             # the server is still reading: the bug
        finally:
            c.close()
        self.assertEqual(status, 400)
        self.assertFalse(os.path.exists(self.e.cfg["pause_all"]))


class UiPlistSurvivesASpaceInThePath(unittest.TestCase):
    """A-12: the UI agent ran `bash -c "exec <python> <dir>/ui/server.py"` with both paths spliced into the shell text, so a checkout in
    a folder with a space never started the UI, and a folder name with shell syntax in it ran as a command at every login."""

    def test_the_program_arguments_run_the_server_from_a_folder_with_a_space(self):
        import tempfile
        import shutil
        tmp = tempfile.mkdtemp(prefix="chronos test ")
        try:
            inst = os.path.join(tmp, "my checkout; touch INJECTED")
            os.makedirs(os.path.join(inst, "ui"))
            marker = os.path.join(tmp, "started")
            open(os.path.join(inst, "ui", "server.py"), "w").write("open(%r, 'w').write('ok')\n" % marker)
            tmpl = open(os.path.join(ROOT, "launchd", "io.github.chronos.ui.plist.tmpl")).read()
            esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            text = (tmpl.replace("__INSTALL_DIR__", esc(inst)).replace("__HOME_DIR__", esc(tmp)).replace("__CONFIG__", esc(os.path.join(tmp, "c.json")))
                    .replace("__PYTHON__", esc(sys.executable)))
            args = plistlib.loads(text.encode("utf-8"))["ProgramArguments"]
            r = subprocess.run(args, cwd=tmp, capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(os.path.exists(marker))
            self.assertFalse(os.path.exists(os.path.join(tmp, "INJECTED")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
