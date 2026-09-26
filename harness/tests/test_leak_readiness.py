#!/usr/bin/env python3
"""bench.py leak and readiness gates.

Run with: python3 -m unittest discover harness/tests
"""
import json
import os
import sys
import tempfile
import unittest

HARNESS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HARNESS_DIR)
import bench  # noqa: E402


def gate(fn, *args, **kw):
    out = []
    return fn(*args, out=out.append, **kw), out


class TestLeak(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, text):
        p = os.path.join(self.tmp, "p%d.txt" % len(os.listdir(self.tmp)))
        with open(p, "w") as f:
            f.write(text)
        return p

    def test_shipped_prompts_pass(self):
        files = [os.path.join(HARNESS_DIR, "system-prompt.txt")] + \
                [os.path.join(HARNESS_DIR, "prompts", f) for f in ("noskill.txt", "skill.txt")]
        rc, out = gate(bench.leak, files)
        self.assertEqual(rc, 0, out)

    def test_leak_word_fails_with_file_line_word(self):
        p = self.write("fine line\nthe Feature Flag is on\n")
        rc, out = gate(bench.leak, [p])
        self.assertEqual(rc, 1)
        self.assertIn("%s:2:feature flag" % p, out)

    def test_word_boundary(self):
        # "flags" and "scenarios" are not the words "flag" and "scenario".
        rc, out = gate(bench.leak, [self.write("many flags, two scenarios\n")])
        self.assertEqual(rc, 0, out)

    def test_missing_file_is_2(self):
        rc, _ = gate(bench.leak, [os.path.join(self.tmp, "nope.txt")])
        self.assertEqual(rc, 2)


class TestReadiness(unittest.TestCase):
    def test_shipped_scenarios_pass(self):
        for s in ("paymentFailure", "adFailure", "paymentUnreachable"):
            rc, out = gate(bench.readiness, s)
            self.assertEqual(rc, 0, out)
            self.assertEqual(out[-1], "readiness %s: PASS" % s)

    def test_missing_scenario_fails(self):
        rc, out = gate(bench.readiness, "noSuchScenario")
        self.assertEqual(rc, 1)
        self.assertIn("scenario file not found", out[0])

    def test_missing_evidence_fails(self):
        with open(os.path.join(HARNESS_DIR, "scenarios", "paymentFailure.json")) as f:
            d = json.load(f)
        d["evidence"]["baseline_absence"] = "../nowhere.json"
        with tempfile.TemporaryDirectory() as h:
            os.makedirs(os.path.join(h, "scenarios"))
            with open(os.path.join(h, "scenarios", "x.json"), "w") as f:
                json.dump(d, f)
            rc, out = gate(bench.readiness, "x", harness=h)
        self.assertEqual(rc, 1)
        self.assertIn("readiness x: baseline_absence file exists - FAIL", "\n".join(out))
        self.assertEqual(out[-1], "readiness x: FAIL")

    def test_version_must_be_a_positive_integer(self):
        with open(os.path.join(HARNESS_DIR, "scenarios", "paymentFailure.json")) as f:
            d = json.load(f)
        d["evidence"] = {k: os.path.join(HARNESS_DIR, "scenarios", v) for k, v in d["evidence"].items()}
        for bad in (None, 0, -1, "1", 1.5, True):
            with self.subTest(version=bad), tempfile.TemporaryDirectory() as h:
                os.makedirs(os.path.join(h, "scenarios"))
                doc = dict(d)
                if bad is None:
                    del doc["version"]
                else:
                    doc["version"] = bad
                with open(os.path.join(h, "scenarios", "x.json"), "w") as f:
                    json.dump(doc, f)
                rc, out = gate(bench.readiness, "x", harness=h)
                self.assertEqual(rc, 1, out)
                self.assertIn("readiness x: version is a positive integer - FAIL", "\n".join(out))

    def test_oracle_must_be_a_non_empty_list_of_tool_and_arguments(self):
        with open(os.path.join(HARNESS_DIR, "scenarios", "paymentFailure.json")) as f:
            d = json.load(f)
        d["evidence"] = {k: os.path.join(HARNESS_DIR, "scenarios", v) for k, v in d["evidence"].items()}
        cases = {"missing": None, "empty list": [], "item without tool": [{"arguments": {}}]}
        for label, bad in cases.items():
            with self.subTest(oracle=label), tempfile.TemporaryDirectory() as h:
                os.makedirs(os.path.join(h, "scenarios"))
                doc = dict(d)
                if bad is None:
                    del doc["oracle"]
                else:
                    doc["oracle"] = bad
                with open(os.path.join(h, "scenarios", "x.json"), "w") as f:
                    json.dump(doc, f)
                rc, out = gate(bench.readiness, "x", harness=h)
                self.assertEqual(rc, 1, out)
                self.assertIn("readiness x: oracle is a non-empty list of {tool, arguments} - FAIL", "\n".join(out))

if __name__ == "__main__":
    unittest.main()
