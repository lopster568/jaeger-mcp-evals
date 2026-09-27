#!/usr/bin/env python3
"""Unit tests for harness/score.py's mechanism-aware verdict.

Run with: python3 -m unittest discover harness/tests
Python 3 stdlib only, no network, no LLM, no ssh, no docker.
"""
import json
import os
import sys
import tempfile
import unittest

HARNESS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, HARNESS_DIR)
sys.path.insert(0, os.path.join(HARNESS_DIR, "tests"))
import score as score_mod  # noqa: E402
from fake_grader import graded  # noqa: E402


def graded_score(d):
    with graded():
        return score_mod.score(d, call_grader=True)


def build_run_dir_structured(root, verdict, scenario="paymentFailure", name="fake"):
    """Write a fake run dir whose result event carries a structured verdict
    in a separate structured_output field (result stays a plain string)."""
    out_dir = os.path.join(root, f"{name}-20260101T000000Z")
    os.makedirs(out_dir, exist_ok=True)

    result_event = {
        "type": "result",
        "subtype": "success",
        "num_turns": 3,
        "total_cost_usd": 0.01,
        "duration_ms": 1000,
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }
    result_event["structured_output"] = verdict
    result_event["result"] = "see structured_output"

    events = [
        {
            "type": "system",
            "subtype": "init",
            "model": "claude-test",
            "mcp_servers": [{"name": "jaeger"}],
            "tools": ["mcp__jaeger__get_trace_errors"],
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "mcp__jaeger__get_trace_errors", "input": {}},
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "is_error": False, "content": "some trace evidence"},
                ]
            },
        },
        result_event,
    ]
    with open(os.path.join(out_dir, "stream.jsonl"), "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"scenario": scenario, "schema_version": 5}, f)

    with open(os.path.join(out_dir, "prompt.txt"), "w", encoding="utf-8") as f:
        f.write("investigate the failing checkout trace")

    return out_dir


def build_run_dir_with_read_skill(root, read_skill_calls, name, scenario="paymentFailure"):
    """Write a fake run dir whose trajectory calls mcp__jaeger__read_skill
    zero or more times before answering. read_skill_calls is a list of
    (is_error, text) tuples, one per read_skill call/result pair. The final
    answer is always a passing structured verdict for the given scenario so
    verdict computation itself is untouched by these tests - only the
    read_skill_* fields are under test."""
    out_dir = os.path.join(root, f"{name}-20260101T000000Z")
    os.makedirs(out_dir, exist_ok=True)

    events = [
        {
            "type": "system",
            "subtype": "init",
            "model": "claude-test",
            "mcp_servers": [{"name": "jaeger"}],
            "tools": ["mcp__jaeger__read_skill", "mcp__jaeger__get_trace_errors"],
        },
    ]
    for i, (is_error, text) in enumerate(read_skill_calls):
        call_id = f"read-skill-{i}"
        events.append({
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "id": call_id, "name": "mcp__jaeger__read_skill", "input": {"skill": "error-root-cause"}},
            ]},
        })
        events.append({
            "type": "user",
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": call_id, "is_error": is_error, "content": text},
            ]},
        })
    events.append({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "id": "investigate-1", "name": "mcp__jaeger__get_trace_errors", "input": {}},
        ]},
    })
    events.append({
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": "investigate-1", "is_error": False, "content": "some trace evidence"},
        ]},
    })
    events.append({
        "type": "result",
        "subtype": "success",
        "num_turns": 3,
        "total_cost_usd": 0.01,
        "duration_ms": 1000,
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "result": "see structured_output",
        "structured_output": {"root_cause_service": "payment", "root_cause_operation": "charge",
                              "mechanism": "invalid_token", "cascading": [{"service": "checkout"}], "abstain": False},
    })

    with open(os.path.join(out_dir, "stream.jsonl"), "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"scenario": scenario, "schema_version": 5}, f)
    with open(os.path.join(out_dir, "prompt.txt"), "w", encoding="utf-8") as f:
        f.write("investigate the failing checkout trace, call read_skill first")

    return out_dir


class TestScore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_structured_payment_invalid_token_pass(self):
        verdict = {
            "root_cause_service": "payment",
            "root_cause_operation": "charge",
            "mechanism": "payment rejects every charge over an invalid token",
            "cascading": [{"service": "checkout", "operation": "PlaceOrder"}],
            "confidence": "high",
            "evidence_span_ids": ["span-1"],
            "abstain": False,
        }
        d = build_run_dir_structured(self.tmp.name, verdict, scenario="paymentFailure")
        summary, _ = graded_score(d)
        self.assertEqual(summary["verdict_source"], "structured")
        self.assertEqual(summary["locus"], "PASS")
        self.assertEqual(summary["mechanism"], "PASS")
        self.assertEqual(summary["cascade"], "PASS")
        self.assertFalse(summary["abstained"])
        self.assertEqual(summary["verdict"], "PASS")
        self.assertEqual(summary["mechanism_value"], verdict["mechanism"])
        self.assertEqual((summary["mechanism_grade"], summary["grader_model"], summary["grader_cached"]),
                         ("correct", "claude-fable-5-1", False))
        self.assertEqual(len(summary["grader_prompt_sha256"]), 64)

    def test_structured_payment_timeout_partial(self):
        verdict = {
            "root_cause_service": "payment",
            "root_cause_operation": "charge",
            "mechanism": "the charge call timed out waiting for the card processor",
            "cascading": [{"service": "checkout", "operation": "PlaceOrder"}],
            "confidence": "medium",
            "evidence_span_ids": ["span-1"],
            "abstain": False,
        }
        d = build_run_dir_structured(self.tmp.name, verdict, scenario="paymentFailure")
        summary, _ = graded_score(d)
        self.assertEqual(summary["verdict_source"], "structured")
        self.assertEqual(summary["locus"], "PASS")
        self.assertEqual(summary["mechanism"], "FAIL")
        self.assertEqual(summary["verdict"], "PARTIAL")

    def test_structured_abstain(self):
        verdict = {
            "root_cause_service": "unknown",
            "root_cause_operation": "unknown",
            "mechanism": "the spans do not show one",
            "cascading": [],
            "confidence": "low",
            "evidence_span_ids": [],
            "abstain": True,
        }
        d = build_run_dir_structured(self.tmp.name, verdict, scenario="paymentFailure")
        summary, _ = score_mod.score(d)
        self.assertEqual(summary["verdict_source"], "structured")
        self.assertTrue(summary["abstained"])
        self.assertEqual(summary["verdict"], "ABSTAIN")

    def test_unclear_grade_is_partial(self):
        verdict = {"root_cause_service": "payment", "root_cause_operation": "charge",
                   "mechanism": "unsure: something in the charge path", "cascading": [{"service": "checkout"}],
                   "confidence": "low", "evidence_span_ids": [], "abstain": False}
        summary, _ = graded_score(build_run_dir_structured(self.tmp.name, verdict))
        self.assertEqual((summary["mechanism_grade"], summary["mechanism"], summary["verdict"]),
                         ("unclear", "UNCLEAR", "PARTIAL"))

    def test_cache_miss_without_grader_is_ungraded(self):
        verdict = {"root_cause_service": "payment", "root_cause_operation": "charge", "mechanism": "invalid token",
                   "cascading": [{"service": "checkout"}], "confidence": "high", "evidence_span_ids": [], "abstain": False}
        summary, _ = score_mod.score(build_run_dir_structured(self.tmp.name, verdict))
        self.assertEqual((summary["mechanism"], summary["verdict"], summary["mechanism_grade"]), ("UNGRADED", "UNGRADED", None))

    def test_legacy_schema_is_not_rescored(self):
        d = build_run_dir_structured(self.tmp.name, {"mechanism": "invalid_token"})
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            json.dump({"scenario": "paymentFailure", "schema_version": 4}, f)
        with self.assertRaises(score_mod.Legacy):
            score_mod.score(d)

    def test_non_jaeger_tool_calls_excluded_from_metrics(self):
        # The CLI delivers the structured verdict through an internal tool
        # (e.g. "StructuredOutput") that is not a Jaeger MCP tool. It must
        # not be counted in tool_calls/call_sequence, and its result (if
        # any) must not be counted in call_errors/tool_output_chars either.
        out_dir = os.path.join(self.tmp.name, "nonjaeger-20260101T000000Z")
        os.makedirs(out_dir, exist_ok=True)
        verdict = {
            "root_cause_service": "payment",
            "root_cause_operation": "charge",
            "mechanism": "invalid_token",
            "cascading": [{"service": "checkout", "operation": "PlaceOrder"}],
            "confidence": "high",
            "evidence_span_ids": ["span-1"],
            "abstain": False,
        }
        events = [
            {
                "type": "system",
                "subtype": "init",
                "model": "claude-test",
                "mcp_servers": [{"name": "jaeger"}],
                "tools": ["mcp__jaeger__get_trace_errors", "mcp__jaeger__search_traces"],
            },
            {
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "id": "call-1", "name": "mcp__jaeger__search_traces", "input": {}},
                ]},
            },
            {
                "type": "user",
                "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "call-1", "is_error": False, "content": "trace list"},
                ]},
            },
            {
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "id": "call-2", "name": "mcp__jaeger__get_trace_errors", "input": {}},
                ]},
            },
            {
                "type": "user",
                "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "call-2", "is_error": False, "content": "Invalid token"},
                ]},
            },
            {
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "id": "call-3", "name": "StructuredOutput", "input": verdict},
                ]},
            },
            {
                "type": "result",
                "subtype": "success",
                "num_turns": 5,
                "total_cost_usd": 0.02,
                "duration_ms": 2000,
                "usage": {"input_tokens": 10, "output_tokens": 20},
                "result": "see structured_output",
                "structured_output": verdict,
            },
        ]
        with open(os.path.join(out_dir, "stream.jsonl"), "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
        with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump({"scenario": "paymentFailure", "schema_version": 5}, f)
        with open(os.path.join(out_dir, "prompt.txt"), "w", encoding="utf-8") as f:
            f.write("investigate the failing checkout trace")

        summary, _ = score_mod.score(out_dir)
        self.assertEqual(summary["tool_calls"], 2)
        self.assertEqual(summary["non_jaeger_tool_calls"], ["StructuredOutput"])
        self.assertEqual(summary["call_sequence"], ["search_traces", "get_trace_errors"])

    def test_structured_missing_keys_scores_missing_and_fail(self):
        # Only one of the required structured keys is present, so
        # extract_structured declines it: no verdict is a FAIL, not a crash.
        partial = {"root_cause_service": "payment"}
        d = build_run_dir_structured(self.tmp.name, partial, scenario="paymentFailure")
        summary, _ = score_mod.score(d)
        self.assertIsNone(summary["verdict_source"])
        self.assertEqual((summary["locus"], summary["mechanism"], summary["cascade"]), ("MISSING",) * 3)
        self.assertEqual(summary["verdict"], "FAIL")

    def test_structured_service_hallucinated_gateway_locus_fails(self):
        # Exact match, not substring: "payment-gateway" must never PASS
        # against pass_rule.service_exact == ["payment"], the way the old
        # unanchored \bpayment\b regex would have.
        verdict = {
            "root_cause_service": "payment-gateway",
            "root_cause_operation": "charge",
            "mechanism": "invalid_token",
            "cascading": [{"service": "checkout", "operation": "PlaceOrder"}],
            "confidence": "high",
            "evidence_span_ids": ["span-1"],
            "abstain": False,
        }
        d = build_run_dir_structured(self.tmp.name, verdict, scenario="paymentFailure")
        summary, _ = score_mod.score(d)
        self.assertEqual(summary["verdict_source"], "structured")
        self.assertEqual(summary["locus"], "FAIL")

    def test_structured_service_case_insensitive_locus_passes(self):
        # Exact match is case-insensitive (compares stripped, lowercased
        # strings): "Payment" must still PASS against ["payment"].
        verdict = {
            "root_cause_service": "Payment",
            "root_cause_operation": "charge",
            "mechanism": "invalid_token",
            "cascading": [{"service": "checkout", "operation": "PlaceOrder"}],
            "confidence": "high",
            "evidence_span_ids": ["span-1"],
            "abstain": False,
        }
        d = build_run_dir_structured(self.tmp.name, verdict, scenario="paymentFailure")
        summary, _ = score_mod.score(d)
        self.assertEqual(summary["verdict_source"], "structured")
        self.assertEqual(summary["locus"], "PASS")

    def test_structured_operation_recharge_not_pass(self):
        # Exact match, not substring: "recharge" must never PASS against
        # pass_rule.operation_exact == ["charge", ...], the way the old
        # unanchored \bcharge\b regex would not have caught it either, but
        # this pins the exact-match behavior explicitly.
        verdict = {
            "root_cause_service": "payment",
            "root_cause_operation": "recharge",
            "mechanism": "invalid_token",
            "cascading": [{"service": "checkout", "operation": "PlaceOrder"}],
            "confidence": "high",
            "evidence_span_ids": ["span-1"],
            "abstain": False,
        }
        d = build_run_dir_structured(self.tmp.name, verdict, scenario="paymentFailure")
        summary, _ = score_mod.score(d)
        self.assertEqual(summary["verdict_source"], "structured")
        self.assertNotEqual(summary["locus"], "PASS")

    def test_read_skill_never_attempted(self):
        # noskill arm (or a skill arm that never calls it): attempted/
        # succeeded both false, no errors.
        d = build_run_dir_with_read_skill(self.tmp.name, [], "noskill-fake")
        summary, _ = score_mod.score(d)
        self.assertFalse(summary["read_skill_attempted"])
        self.assertFalse(summary["read_skill_succeeded"])
        self.assertEqual(summary["read_skill_errors"], 0)

    def test_read_skill_attempted_and_succeeded(self):
        # A read_skill call that comes back non-error with a real skill body
        # (well over 200 chars) counts as both attempted and succeeded.
        long_text = "x" * 250
        d = build_run_dir_with_read_skill(self.tmp.name, [(False, long_text)], "skill-ok-fake")
        summary, _ = score_mod.score(d)
        self.assertTrue(summary["read_skill_attempted"])
        self.assertTrue(summary["read_skill_succeeded"])
        self.assertEqual(summary["read_skill_errors"], 0)

    def test_read_skill_attempted_but_failed(self):
        # The directory-path failure this fix exists for: read_skill is
        # called (attempted=True) but errors out, so succeeded must be False
        # and the error must be counted - this is exactly the case that a
        # bare "called" boolean hid as a false pass.
        d = build_run_dir_with_read_skill(self.tmp.name, [(True, "Error: is a directory")], "skill-fail-fake")
        summary, _ = score_mod.score(d)
        self.assertTrue(summary["read_skill_attempted"])
        self.assertFalse(summary["read_skill_succeeded"])
        self.assertEqual(summary["read_skill_errors"], 1)


if __name__ == "__main__":
    unittest.main()
