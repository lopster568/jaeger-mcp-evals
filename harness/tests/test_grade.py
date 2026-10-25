#!/usr/bin/env python3
"""grade.py against a fake claude CLI: the canned label is parsed, cached, and never fetched twice.

Run with: python3 -m unittest discover harness/tests
Python 3 stdlib only, no network, no LLM.
"""
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

HARNESS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HARNESS_DIR)
import grade  # noqa: E402

SHIM = """#!/usr/bin/env python3
import json, os, sys
open(os.environ["SHIM_LOG"], "a").write("call\\n")
print(json.dumps(%s))
"""


class TestGrade(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log = os.path.join(self._tmp.name, "calls")
        self.addCleanup(setattr, grade, "BILLING_FAILED", False)

    def shim(self, out):
        p = os.path.join(self._tmp.name, "claude")
        with open(p, "w") as f:
            f.write(SHIM % repr(out))
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        return mock.patch.object(grade, "CLAUDE", p), mock.patch.dict(os.environ, {"SHIM_LOG": self.log})

    def calls(self):
        return open(self.log).read().count("call") if os.path.isfile(self.log) else 0

    def test_canned_label_is_cached_and_reused(self):
        a, b = self.shim({"type": "result", "structured_output": {"label": "incorrect", "reason": "different cause"}})
        with a, b:
            first = grade.grade(self._tmp.name, "truth", "answer")
            second = grade.grade(self._tmp.name, "truth", "answer")
        self.assertEqual((first["label"], first["reason"], first["cached"]), ("incorrect", "different cause", False))
        self.assertEqual((second["label"], second["cached"], second["model"]), ("incorrect", True, "claude-fable-5-1"))
        self.assertEqual(self.calls(), 1)
        self.assertIsNone(grade.grade(self._tmp.name, "truth", "another answer", call=False))

    def test_no_valid_label_raises_and_caches_nothing(self):
        a, b = self.shim({"type": "result", "structured_output": {"label": "maybe", "reason": "x"}})
        with a, b, self.assertRaises(RuntimeError):
            grade.grade(self._tmp.name, "truth", "answer")
        self.assertFalse(os.path.exists(os.path.join(self._tmp.name, "grades.jsonl")))

    def test_bill_api_key_passes_the_key_in_env_only_and_checks_init(self):
        good = [{"type": "system", "subtype": "init", "apiKeySource": "ANTHROPIC_API_KEY"},
                {"type": "result", "structured_output": {"label": "correct", "reason": "same"}}]
        seen = os.path.join(self._tmp.name, "seen")
        a, b = self.shim(good)
        with open(os.path.join(self._tmp.name, "claude"), "a") as f:
            f.write("open(%r, 'w').write(json.dumps([os.environ.get('ANTHROPIC_API_KEY'), sys.argv]))\n" % seen)
        with a, b, mock.patch.object(grade, "API_KEY", "sk-test"), mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "dropped"}):
            self.assertEqual(grade.grade(self._tmp.name, "t", "x")["label"], "correct")
        key, argv = json.load(open(seen))
        self.assertEqual(key, "sk-test")
        self.assertNotIn("sk-test", json.dumps(argv) + open(os.path.join(self._tmp.name, "grades.jsonl")).read())
        self.assertIn("--verbose", argv)

    def test_bill_api_key_fails_when_init_reports_another_source(self):
        for out in ([{"type": "system", "subtype": "init", "apiKeySource": "none"},
                     {"type": "result", "structured_output": {"label": "correct", "reason": "same"}}],
                    {"type": "result", "structured_output": {"label": "correct", "reason": "same"}}):
            a, b = self.shim(out)
            with a, b, mock.patch.object(grade, "API_KEY", "sk-test"), self.assertRaisesRegex(RuntimeError, "billing"):
                grade.grade(self._tmp.name, "t", "x")

    def test_after_a_billing_error_no_further_call_reaches_the_cli(self):
        a, b = self.shim([{"type": "system", "subtype": "init", "apiKeySource": "none"},
                          {"type": "result", "structured_output": {"label": "correct", "reason": "same"}}])
        with a, b, mock.patch.object(grade, "API_KEY", "sk-test"):
            for text in ("x", "y", "z"):
                with self.assertRaisesRegex(RuntimeError, "billing"):
                    grade.grade(self._tmp.name, "t", text)
        self.assertEqual(self.calls(), 1)

    def test_verbose_array_uses_the_last_result_event_not_the_last_element(self):
        a, b = self.shim([{"type": "system", "subtype": "init", "apiKeySource": "ANTHROPIC_API_KEY"},
                          {"type": "result", "structured_output": {"label": "correct", "reason": "same"}},
                          {"type": "system", "subtype": "trailer"}])
        with a, b, mock.patch.object(grade, "API_KEY", "sk-test"):
            self.assertEqual(grade.grade(self._tmp.name, "t", "x")["label"], "correct")

    def test_default_drops_the_key_and_adds_no_flag(self):
        self.assertIsNone(grade.API_KEY)
        self.assertNotIn("--verbose", grade.argv("t", "x", "m.json"))


if __name__ == "__main__":
    unittest.main()
