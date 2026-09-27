#!/usr/bin/env python3
"""bench.py band: exit codes 0/2/3 and the compaction WARNING.

Run with: python3 -m unittest discover harness/tests
Python 3 stdlib only, no network, no LLM, no ssh, no docker.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

HARNESS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HARNESS_DIR)
sys.path.insert(0, os.path.join(HARNESS_DIR, "tests"))
import bench  # noqa: E402
from test_score_effort_compaction import CALL, COMPACT, FINAL, INIT, RESULT  # noqa: E402

VERDICT = {"root_cause_service": "payment", "root_cause_operation": "charge", "mechanism": "invalid_token",
           "mechanism_detail": "x", "cascading": [{"service": "checkout"}], "confidence": "high",
           "evidence_span_ids": [], "abstain": False}
PASS_FINAL = dict(FINAL, structured_output=VERDICT)
FAIL_FINAL = dict(FINAL, structured_output=dict(VERDICT, root_cause_service="checkout"))

CALL_SEARCH = {"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "t2", "name": "mcp__jaeger__search_traces", "input": {}}]}}
RESULT_SEARCH = {"type": "user", "message": {"content": [
    {"type": "tool_result", "tool_use_id": "t2", "is_error": False, "content": "x"}]}}
CALL_SKILL = {"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "t3", "name": "mcp__jaeger__read_skill", "input": {}}]}}
RESULT_SKILL = {"type": "user", "message": {"content": [
    {"type": "tool_result", "tool_use_id": "t3", "is_error": False, "content": "x" * 300}]}}


def make_batch(root, runs):
    """runs: list of event lists, one trial each, arm noskill."""
    scen = os.path.join(root, "runs", "paymentFailure")
    batch = os.path.join(scen, "batch-20260928T000000Z")
    os.makedirs(batch)
    with open(os.path.join(batch, "manifest.json"), "w") as f:
        json.dump({"scenario": "paymentFailure"}, f)
    with open(os.path.join(batch, "cells.jsonl"), "w") as cells:
        for i, events in enumerate(runs):
            name = "%d-noskill" % i
            d = os.path.join(batch, name)
            os.makedirs(d)
            with open(os.path.join(d, "stream.jsonl"), "w") as f:
                f.writelines(json.dumps(e) + "\n" for e in events)
            with open(os.path.join(d, "meta.json"), "w") as f:
                json.dump({"scenario": "paymentFailure"}, f)
            cells.write(json.dumps({"arm": "noskill", "out_dir": name}) + "\n")
    return batch


def run_band(batch, arm="noskill"):
    """The screen table and the key=value detail lines, colour stripped."""
    out = []
    with contextlib.redirect_stderr(io.StringIO()):
        rc = bench.band(batch, arm, out=out.append, detail=out.append)
    return rc, bench.ANSI.sub("", "\n".join(out))


class TestBand(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_rank_only_below_ten(self):
        rc, out = run_band(make_batch(self.tmp, [[INIT, CALL, RESULT, PASS_FINAL]] * 3))
        self.assertEqual(rc, 2)
        self.assertIn("band paymentFailure/noskill: n=3 passes=3 pass_rate=1.00", out)
        self.assertIn("(n=3 per arm; below certification threshold (10); rank only)", out)

    def test_all_pass_is_3(self):
        rc, out = run_band(make_batch(self.tmp, [[INIT, CALL, RESULT, PASS_FINAL]] * 10))
        self.assertEqual(rc, 3)
        self.assertIn("read the logs", out)

    def test_mixed_is_certified_0(self):
        rc, out = run_band(make_batch(self.tmp, [[INIT, CALL, RESULT, PASS_FINAL]] * 6 + [[INIT, CALL, RESULT, FAIL_FINAL]] * 4))
        self.assertEqual(rc, 0, out)
        self.assertIn("n=10 passes=6 pass_rate=0.60", out)

    def test_all_arms_when_no_arm_given(self):
        batch = make_batch(self.tmp, [[INIT, CALL, RESULT, PASS_FINAL]] * 2)
        out = []
        self.assertEqual(bench.band(batch, out=out.append, detail=out.append), 2)
        self.assertEqual(sum(l.startswith("band ") for l in out), 1)

    def test_wilson_interval_on_band_line(self):
        for passes, want in ((10, "ci95=[0.72,1.00]"), (9, "ci95=[0.60,0.98]"), (0, "ci95=[0.00,0.28]")):
            with self.subTest(passes=passes), tempfile.TemporaryDirectory() as t:
                runs = [[INIT, CALL, RESULT, PASS_FINAL]] * passes + [[INIT, CALL, RESULT, FAIL_FINAL]] * (10 - passes)
                _, out = run_band(make_batch(t, runs))
                self.assertIn("pass_rate=%.2f %s" % (passes / 10, want), out)

    def test_wilson_zero_n_prints_no_interval(self):
        out = []
        bench.results("s", [bench.arm_row("a", [], 0)], out.append, out.append)
        self.assertNotIn("ci95", out[1])
        self.assertIn("0/0", out[-2])

    def test_table_row_is_aligned_under_its_header(self):
        rc, out = run_band(make_batch(self.tmp, [[INIT, CALL, RESULT, PASS_FINAL]] * 6 + [[INIT, CALL, RESULT, FAIL_FINAL]] * 4))
        lines = out.splitlines()
        head = next(l for l in lines if l.lstrip().startswith("arm "))
        row = lines[lines.index(head) + 1]
        self.assertEqual(row.split(), ["noskill", "6/10", "0", "4", "0", "0", "0", "0.60", "[0.31,", "0.83]", "1", "19"])
        self.assertEqual(len(row), len(head))

    def test_errors_and_stops_counted_apart(self):
        runs = [[INIT, CALL, RESULT, PASS_FINAL], ["not an event"], [INIT, CALL, RESULT, dict(FAIL_FINAL, subtype="error_max_turns")],
                [INIT, CALL, RESULT]]
        _, out = run_band(make_batch(self.tmp, runs))
        self.assertIn("n=4 passes=1", out)
        self.assertIn("ERROR=1 INVALID=0 stops=error_max_turns:1,no_result:1", out)

    def test_tools_field_counts_runs_per_tool_excluding_read_skill(self):
        runs = [[INIT, CALL, RESULT, CALL_SEARCH, RESULT_SEARCH, PASS_FINAL],
                [INIT, CALL, RESULT, PASS_FINAL],
                [INIT, CALL_SKILL, RESULT_SKILL, PASS_FINAL]]
        _, out = run_band(make_batch(self.tmp, runs))
        self.assertIn("tools=get_trace_errors:2,search_traces:1", out)

    def test_no_errors_no_stops(self):
        _, out = run_band(make_batch(self.tmp, [[INIT, CALL, RESULT, PASS_FINAL]] * 2))
        self.assertIn("ERROR=0 INVALID=0 stops=-", out)

    def test_missing_cells_is_2(self):
        out = []
        self.assertEqual(bench.band(self.tmp, "noskill", out=out.append), 2)
        self.assertIn("no such file", out[0])


class TestBandCompactionWarning(unittest.TestCase):
    """A compacted run is a WARNING line and does not change band's exit code."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_band_warns_on_compacted_run(self):
        batch = make_batch(self._tmp.name, [[INIT, CALL, RESULT, FINAL], [INIT, CALL, RESULT, COMPACT, FINAL]])
        rc, out = run_band(batch)
        self.assertIn("n=2", out)
        self.assertIn("WARNING: 1/2 counted runs contain a compaction event", out)
        self.assertEqual(rc, 2, out)  # n<10 rank-only, unchanged by the warning


if __name__ == "__main__":
    unittest.main()
