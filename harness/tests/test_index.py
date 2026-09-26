#!/usr/bin/env python3
"""records/INDEX.md: generated from records/, stable, and checked by verify.

Run with: python3 -m unittest discover -s harness/tests
"""
import contextlib
import io
import json
import os
import pathlib
import tempfile
import sys
import unittest
from unittest import mock

HARNESS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HARNESS_DIR)
import bench  # noqa: E402


class TestIndex(unittest.TestCase):
    def test_committed_index_matches_and_is_stable(self):
        text = bench.index_text(bench.RECORDS)
        self.assertEqual(text, bench.index_text(bench.RECORDS))
        self.assertEqual(text, pathlib.Path(bench.RECORDS, "INDEX.md").read_text(encoding="utf-8"))

    def test_scenarios_table(self):
        with tempfile.TemporaryDirectory() as records:
            lines = bench.index_text(records).splitlines()
        self.assertIn("| adFailure | no | PASS | 0 | 0 |", lines)
        self.assertIn("| paymentFailure | yes | PASS | 0 | 0 |", lines)
        self.assertIn("| paymentUnreachable | yes | PASS | 0 | 0 |", lines)
        self.assertLess(lines.index("## Scenarios"), lines.index("## Batches"))


class TestVerifyCrossCheck(unittest.TestCase):
    """verify re-scores each stream and fails when a stored scores.jsonl verdict disagrees."""

    def make(self, runs, records, stored_verdict):
        b = os.path.join(runs, "paymentFailure", "batch-20260928T000000Z")
        t = os.path.join(b, "0-noskill")
        os.makedirs(t)
        final = {"type": "result", "subtype": "success", "result": "x", "usage": {}, "structured_output": {
            "root_cause_service": "payment", "root_cause_operation": "charge", "mechanism": "invalid_token",
            "cascading": [{"service": "checkout"}], "abstain": False}}
        pathlib.Path(t, "stream.jsonl").write_text(json.dumps(final) + "\n")
        pathlib.Path(t, "meta.json").write_text(json.dumps({"scenario": "paymentFailure", "arm": "noskill"}))
        pathlib.Path(b, "cells.jsonl").write_text(json.dumps({"arm": "noskill", "out_dir": "0-noskill"}) + "\n")
        pathlib.Path(b, "scores.jsonl").write_text(json.dumps({"dir": "0-noskill", "arm": "noskill", "verdict": stored_verdict}) + "\n")
        pathlib.Path(records, "INDEX.md").write_text(bench.index_text(records))

    def verify(self, runs, records, stale=False):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"RUNS_DIR": runs}), mock.patch.object(bench, "RECORDS", records), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            if stale:
                pathlib.Path(records, "INDEX.md").write_text("stale\n")
            return bench.main(["verify"]), out.getvalue()

    def test_matching_verdict_passes(self):
        with tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as records:
            self.make(runs, records, "PASS")
            self.assertEqual(self.verify(runs, records)[0], 0)

    def test_stored_verdict_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as records:
            self.make(runs, records, "FAIL")
            rc, out = self.verify(runs, records)
        self.assertEqual(rc, 1)
        self.assertIn("stored verdict FAIL, re-scored PASS", out)

    def test_stale_index_fails(self):
        with tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as records:
            self.make(runs, records, "PASS")
            rc, out = self.verify(runs, records, stale=True)
        self.assertEqual(rc, 1)
        self.assertIn("verify: FAIL", out)


if __name__ == "__main__":
    unittest.main()
