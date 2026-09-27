#!/usr/bin/env python3
"""Self-check: the full scoring path on seeded trajectories, with the grader faked by rule.

Each fixture is a trajectory whose right verdict is known in advance; if one of these moves,
the scorer is wrong, not the agent.

Run with: python3 -m unittest discover harness/tests
Python 3 stdlib only, no network, no LLM.
"""
import json
import os
import sys
import tempfile
import unittest

HARNESS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [HARNESS_DIR, os.path.join(HARNESS_DIR, "tests")]
import score  # noqa: E402
from fake_grader import graded  # noqa: E402

TRUTH = score.load_scenario("paymentFailure")["mechanism_truth"]
INIT = {"type": "system", "subtype": "init", "model": "m", "mcp_servers": [{"name": "jaeger", "status": "connected"}],
        "tools": ["StructuredOutput", "mcp__jaeger__get_trace_errors"]}


def call(name, text="Payment request failed. Invalid token."):
    return [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": name, "name": name, "input": {}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": name, "content": text}]}}]


def answer(mechanism, **kw):
    v = dict({"root_cause_service": "payment", "root_cause_operation": "charge", "mechanism": mechanism,
              "cascading": [{"service": "checkout", "operation": "PlaceOrder"}], "confidence": "high",
              "evidence_span_ids": ["a1"], "abstain": False}, **kw)
    return {"type": "result", "subtype": "success", "result": "", "usage": {}, "structured_output": v}


def say(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


class TestSelfCheck(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.n = 0

    def score(self, events):
        self.n += 1
        d = os.path.join(self._tmp.name, "%d-noskill" % self.n)
        os.makedirs(d)
        with open(os.path.join(d, "stream.jsonl"), "w") as f:
            f.writelines(json.dumps(e) + "\n" for e in events)
        with open(os.path.join(d, "meta.json"), "w") as f:
            json.dump({"scenario": "paymentFailure", "client": "cli", "schema_version": score.SCHEMA_VERSION}, f)
        with graded():
            return score.score(d, call_grader=True)[0]

    def test_gold_answer_passes(self):
        s = self.score([INIT] + call("mcp__jaeger__get_trace_errors") + [answer(TRUTH)])
        self.assertEqual((s["mechanism_grade"], s["verdict"]), ("correct", "PASS"))

    def test_confident_wrong_mechanism_fails(self):
        s = self.score([INIT] + call("mcp__jaeger__get_trace_errors") +
                       [answer("Certain: the payment process ran out of heap and crashed on every charge.")])
        self.assertEqual((s["mechanism_grade"], s["mechanism"], s["verdict"]), ("incorrect", "FAIL", "PARTIAL"))

    def test_aborted_or_empty_trajectory_never_passes(self):
        aborted = self.score([INIT] + call("mcp__jaeger__get_trace_errors"))
        empty = self.score([])
        self.assertEqual((aborted["verdict_source"], aborted["verdict"]), (None, "FAIL"))
        self.assertEqual((empty["verdict_source"], empty["verdict"]), (None, "INVALID"))

    def test_executed_tool_outside_the_sandbox_is_invalid(self):
        s = self.score([INIT] + call("Bash", "ok") + [answer(TRUTH)])
        self.assertEqual((s["verdict"], s["sandbox_violations"]), ("INVALID", ["executed_unknown_tool:Bash"]))

    def test_flag_name_in_answer_or_text_is_leak(self):
        s = self.score([INIT, say("flagd is not in the traces")] + call("mcp__jaeger__get_trace_errors") +
                       [answer(TRUTH + " The paymentFailure path fires.")])
        self.assertEqual((s["verdict"], s["leak_hits"]), ("LEAK", ["flagd", "paymentFailure"]))


if __name__ == "__main__":
    unittest.main()
