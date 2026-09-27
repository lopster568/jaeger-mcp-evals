#!/usr/bin/env python3
"""score.py: effort surfaced from meta.json, and compaction_events counted.

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
import score as score_mod  # noqa: E402

# The init event of a real CLI 2.1.x stream lists "compact" and "autocompact"
# among its slash_commands; a detector must not count that.
INIT = {"type": "system", "subtype": "init", "model": "claude-test",
        "tools": ["mcp__jaeger__get_trace_errors", "mcp__jaeger__search_traces", "mcp__jaeger__read_skill"],
        "mcp_servers": [{"name": "jaeger", "status": "connected"}],
        "slash_commands": ["autocompact", "clear", "compact", "config"]}
CALL = {"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "t1", "name": "mcp__jaeger__get_trace_errors", "input": {}}]}}
RESULT = {"type": "user", "message": {"content": [
    {"type": "tool_result", "tool_use_id": "t1", "is_error": False, "content": "some trace evidence"}]}}
THINK = {"type": "system", "subtype": "thinking_tokens", "tokens": 12}
FINAL = {"type": "result", "subtype": "success", "num_turns": 2, "total_cost_usd": 0.01,
         "duration_ms": 1000, "usage": {"input_tokens": 10, "output_tokens": 20}, "result": "x"}
# Shape of the SDK stream message, from the zod schema bundled in CLI 2.1.282
# (type system, subtype compact_boundary, compact_metadata.{trigger,pre_tokens}).
# Never yet seen in a captured run.
COMPACT = {"type": "system", "subtype": "compact_boundary", "session_id": "s", "uuid": "u",
           "compact_metadata": {"trigger": "auto", "pre_tokens": 170000}}


def make_run(root, events, meta):
    out = os.path.join(root, "noskill-20260928T000000Z")
    os.makedirs(out)
    with open(os.path.join(out, "stream.jsonl"), "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    if meta is not None:
        with open(os.path.join(out, "meta.json"), "w") as f:
            json.dump(dict(meta, schema_version=5), f)
    return out


def quiet_score(d):
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        s, _ = score_mod.score(d)
    return s, err.getvalue()


class TestEffort(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_effort_surfaced(self):
        d = make_run(self.tmp, [INIT, CALL, RESULT, FINAL],
                     {"scenario": "paymentFailure", "effort": "xhigh", "system_under_test": {"a": 1}})
        s, err = quiet_score(d)
        self.assertEqual(s["effort"], "xhigh")
        self.assertNotIn("effort", err)

    def test_effort_missing_is_none(self):
        d = make_run(self.tmp, [INIT, CALL, RESULT, FINAL], {"scenario": "paymentFailure"})
        s, _ = quiet_score(d)
        self.assertIsNone(s["effort"])

    def test_no_meta_is_an_error(self):
        d = make_run(self.tmp, [INIT, CALL, RESULT, FINAL], None)
        with self.assertRaises(FileNotFoundError):
            score_mod.score(d)


class TestCompaction(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_synthetic_compaction_counts_one(self):
        d = make_run(self.tmp, [INIT, CALL, RESULT, THINK, COMPACT, FINAL], {"scenario": "paymentFailure"})
        s, _ = quiet_score(d)
        self.assertEqual(s["compaction_events"], 1)

    def test_other_compact_subtype_counts(self):
        ev = {"type": "system", "subtype": "auto_compact_started"}
        d = make_run(self.tmp, [INIT, ev, FINAL], {"scenario": "paymentFailure"})
        s, _ = quiet_score(d)
        self.assertEqual(s["compaction_events"], 1)

    def test_compact_boundary_text_elsewhere_counts(self):
        ev = {"type": "stream_event", "event": {"kind": "compact_boundary"}}
        d = make_run(self.tmp, [INIT, ev, FINAL], {"scenario": "paymentFailure"})
        s, _ = quiet_score(d)
        self.assertEqual(s["compaction_events"], 1)

    def test_real_shape_scores_zero(self):
        # init lists "compact" as a slash command; thinking_tokens is a system
        # subtype without "compact" in it. Neither is a compaction.
        d = make_run(self.tmp, [INIT, THINK, CALL, RESULT, FINAL], {"scenario": "paymentFailure"})
        s, _ = quiet_score(d)
        self.assertEqual(s["compaction_events"], 0)


if __name__ == "__main__":
    unittest.main()
