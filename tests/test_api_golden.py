#!/usr/bin/env python3
"""WP-C1: the golden outputs of every `chronos api` verb (tests/golden/api/*.json) are committed, and the real CLI still
produces exactly them. Downstream consumers (the Talos bridge adapter) vendor these files as their contract fixtures, so a
change in the API's output has to show up here, in the same commit, as a visible diff.

Regenerate after an intended change:   tests/run.sh --golden
"""
import json
import os
import sys
import tempfile
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import golden  # noqa: E402

GOLDEN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden", "api")
VERBS = ("version", "snapshot", "detail", "validate", "apply", "pause", "disable", "config", "due", "session-beat", "approved", "migrate")


class Golden(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = tempfile.mkdtemp(prefix="chronos-golden-")
        cls.made = golden.generate(cls.out)

    def test_every_verb_has_a_golden(self):
        names = set(self.made)
        for v in VERBS:
            self.assertTrue(any(n == v or n.startswith(v + "-") for n in names), "no golden for the %r verb" % v)

    def test_the_committed_files_are_exactly_what_the_real_cli_produces(self):
        committed = sorted(n for n in os.listdir(GOLDEN_DIR) if n.endswith(".json")) if os.path.isdir(GOLDEN_DIR) else []
        self.assertEqual(committed, sorted(n + ".json" for n in self.made), "run tests/run.sh --golden and commit the result")
        for name in self.made:
            want = open(os.path.join(GOLDEN_DIR, name + ".json")).read()
            got = open(os.path.join(self.out, name + ".json")).read()
            self.assertEqual(got, want, "golden %s drifted" % name)

    def test_goldens_carry_no_machine_or_personal_data(self):
        for name in self.made:
            txt = open(os.path.join(self.out, name + ".json")).read()
            for bad in ("/Users/", "/var/folders", "/private/", "chronos-golden", "@", "Phoenix", "telegram"):
                self.assertNotIn(bad, txt, "%s contains %r" % (name, bad))

    def test_goldens_are_deterministic(self):
        again = tempfile.mkdtemp(prefix="chronos-golden-")
        golden.generate(again)
        for name in self.made:
            self.assertEqual(open(os.path.join(self.out, name + ".json")).read(), open(os.path.join(again, name + ".json")).read(), name)

    def test_instants_in_goldens_are_rfc3339_with_an_offset(self):
        import re
        rx = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
        n = 0
        for name in self.made:
            def visit(x, key=""):
                nonlocal n
                if isinstance(x, dict):
                    for k, v in x.items():
                        visit(v, k)
                elif isinstance(x, list):
                    for v in x:
                        visit(v, key)
                elif isinstance(x, str) and key in ("as_of", "next_run", "window_ends", "fire_at", "approved_at", "beat") or key == "next_runs":
                    if isinstance(x, str):
                        n += 1
                        self.assertRegex(x, rx, "%s: %s" % (name, key))
            visit(json.load(open(os.path.join(self.out, name + ".json"))))
        self.assertGreater(n, 10)


if __name__ == "__main__":
    unittest.main(verbosity=2, warnings="ignore")
