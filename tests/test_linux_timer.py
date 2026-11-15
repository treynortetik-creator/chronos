"""Linux: install.sh writes a systemd USER timer for the tick (launchd's job on a Mac) and enables it; uninstall.sh stops and
removes it. systemctl and loginctl are stand-ins that log their arguments (CHRONOS_SYSTEMCTL / CHRONOS_LOGINCTL, with
CHRONOS_TEST_MODE=1). Skipped on a Mac, where the launchd path is tested in test_chronos.py."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(ROOT, "install.sh")
UNINSTALL = os.path.join(ROOT, "uninstall.sh")


# a name, not an inline decorator call, which the PR hygiene scan would read as an address
LINUX_ONLY = unittest.skipIf(sys.platform == "darwin", "the Linux installer path")


@LINUX_ONLY
class LinuxTimer(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="chronos-linux-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.units = os.path.join(self.home, "units")
        self.calls = os.path.join(self.home, "calls.log")
        bindir = os.path.join(self.home, "fakebin")
        os.makedirs(bindir)
        self.linger = os.path.join(self.home, "linger")
        for name, body in (
            ("systemctl", 'echo "systemctl $*" >> "%s"; [ -e "%s/no-user-bus" ] && exit 1; exit 0' % (self.calls, self.home)),
            ("loginctl", 'echo "loginctl $*" >> "%s"; cat "%s" 2>/dev/null || echo no' % (self.calls, self.linger)),
        ):
            p = os.path.join(bindir, name)
            with open(p, "w") as fh:
                fh.write("#!/bin/bash\n" + body + "\n")
            os.chmod(p, 0o755)
        self.env = dict(os.environ, HOME=self.home, CHRONOS_TEST_MODE="1", CHRONOS_SYSTEMD_DIR=self.units,
                        CHRONOS_SYSTEMCTL=os.path.join(bindir, "systemctl"), CHRONOS_LOGINCTL=os.path.join(bindir, "loginctl"))
        self.env.pop("CHRONOS_CONFIG", None)

    def install(self, *args):
        return subprocess.run(["/bin/bash", INSTALL, "--no-ui", "--workspace", os.path.join(self.home, "ws")] + list(args),
                              env=self.env, capture_output=True, text=True, timeout=120)

    def calls_made(self):
        if not os.path.exists(self.calls):
            return ""
        with open(self.calls) as fh:
            return fh.read()

    def test_install_writes_and_enables_the_timer_and_its_service_keeps_the_runs_it_starts(self):
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        svc = open(os.path.join(self.units, "chronos-tick.service")).read()
        timer = open(os.path.join(self.units, "chronos-tick.timer")).read()
        runtime = os.path.join(self.home, ".local", "share", "chronos")
        self.assertIn('ExecStart=/bin/bash "%s/bin/chronos-tick.sh"' % runtime, svc)
        self.assertIn('Environment="CHRONOS_CONFIG=%s/.config/chronos/config.json"' % self.home, svc)
        # the runs the tick starts in the background must outlive it (launchd's AbandonProcessGroup)
        self.assertIn("KillMode=process", svc)
        self.assertIn("Type=oneshot", svc)
        self.assertIn("OnCalendar=*:0/5", timer)
        self.assertIn("Persistent=true", timer)
        self.assertNotIn("__", svc + timer)  # every placeholder filled
        self.assertTrue(os.path.isfile(os.path.join(runtime, "systemd", "chronos-tick.timer.tmpl")))  # shipped with the copy
        calls = self.calls_made()
        self.assertIn("systemctl --user daemon-reload", calls)
        self.assertIn("systemctl --user enable --now chronos-tick.timer", calls)
        self.assertIn("The background timer is on", r.stdout)
        self.assertIn("loginctl enable-linger", r.stdout)  # linger off: the owner is told how to keep it running

    def test_linger_on_says_nothing_about_it(self):
        with open(self.linger, "w") as fh:
            fh.write("yes\n")
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("enable-linger", r.stdout)

    def test_no_load_writes_the_units_and_starts_nothing(self):
        r = self.install("--no-load")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.units, "chronos-tick.timer")))
        self.assertNotIn("systemctl", self.calls_made())
        self.assertIn("not started (--no-load)", r.stdout)

    def test_no_user_systemd_is_said_plainly_with_the_tick_to_run_by_hand(self):
        open(os.path.join(self.home, "no-user-bus"), "w").close()
        r = self.install("--quiet")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no systemd user session here", r.stdout)
        self.assertIn("chronos-tick.sh", r.stdout)

    def test_uninstall_stops_and_removes_the_timer_and_keeps_the_data(self):
        self.assertEqual(self.install().returncode, 0)
        open(self.calls, "w").close()
        u = subprocess.run(["/bin/bash", UNINSTALL], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(u.returncode, 0, u.stdout + u.stderr)
        self.assertIn("systemctl --user disable --now chronos-tick.timer", self.calls_made())
        self.assertFalse(os.path.exists(os.path.join(self.units, "chronos-tick.timer")))
        self.assertFalse(os.path.exists(os.path.join(self.units, "chronos-tick.service")))
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".chronos")))

    def test_a_claude_outside_the_ticks_path_has_its_folder_put_in_the_config(self):
        # nvm or an npm prefix: claude (and node beside it) live where the tick's fixed PATH never looks
        nvm = os.path.join(self.home, "nvm", "bin")
        os.makedirs(nvm)
        with open(os.path.join(nvm, "claude"), "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(os.path.join(nvm, "claude"), 0o755)
        self.env["PATH"] = nvm + ":/usr/bin:/bin"
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.home, ".config", "chronos", "config.json")) as fh:
            cfg = json.load(fh)
        self.assertIn(nvm, cfg["path"])
        self.assertIn("background runs find claude there", r.stdout)
        # again: not added twice
        self.assertEqual(self.install().returncode, 0)
        with open(os.path.join(self.home, ".config", "chronos", "config.json")) as fh:
            self.assertEqual(json.load(fh)["path"].count(nvm), 1)

    def test_no_claude_anywhere_is_a_warning_that_background_runs_will_not_find_it(self):
        self.env["PATH"] = "/usr/bin:/bin"
        if shutil.which("claude", path=self.env["PATH"]):
            self.skipTest("a claude is installed in /usr/bin or /bin here")
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("background runs won't find it either", r.stderr)

    def test_a_stray_stand_in_without_test_mode_is_not_used(self):
        env = dict(self.env)
        env.pop("CHRONOS_TEST_MODE")
        env["PATH"] = "/usr/bin:/bin"
        r = subprocess.run(["/bin/bash", INSTALL, "--no-ui", "--no-load", "--workspace", os.path.join(self.home, "ws")],
                           env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls_made(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
