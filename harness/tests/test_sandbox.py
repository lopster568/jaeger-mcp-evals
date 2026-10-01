#!/usr/bin/env python3
"""score.py's sandbox check (sandbox_ok, sandbox_violations, verdict INVALID) and how band,
verify and judge count INVALID.

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
from unittest import mock

HARNESS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HARNESS_DIR)
sys.path.insert(0, os.path.join(HARNESS_DIR, "tests"))
import bench  # noqa: E402
import judge  # noqa: E402
import score  # noqa: E402
from fake_grader import graded  # noqa: E402
from test_band import PASS_FINAL, make_batch, run_band  # noqa: E402

J = ["mcp__jaeger__get_trace_errors", "mcp__jaeger__search_traces"]
SERVERS = [{"name": "jaeger", "status": "connected"}]


def init(tools=("StructuredOutput",) + tuple(J), servers=SERVERS):
    return {"type": "system", "subtype": "init", "model": "m", "tools": list(tools), "mcp_servers": servers}


def call(name, id_="t1", result="Payment request failed. Invalid token.", is_error=False):
    return [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": id_, "name": name, "input": {}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": id_, "is_error": is_error, "content": result}]}}]


REJECTED = call("mcp__jaeger__StructuredOutput", "t9", "<tool_use_error>Error: No such tool available: "
                "mcp__jaeger__StructuredOutput</tool_use_error>", True)


class TestSandboxCheck(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.batch = os.path.join(self._tmp.name, "batch-20260928T000000Z")
        os.makedirs(self.batch)

    def tearDown(self):
        self._tmp.cleanup()

    def score(self, events, meta=None, tools_json=None):
        """One cli trial in a batch dir; tools_json is the batch's tools/list (bare names)."""
        if tools_json is not None:
            with open(os.path.join(self.batch, "tools.json"), "w") as f:
                json.dump([{"name": n} for n in tools_json], f)
        d = os.path.join(self.batch, "0-noskill")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "stream.jsonl"), "w") as f:
            f.writelines(json.dumps(e) + "\n" for e in events)
        with open(os.path.join(d, "meta.json"), "w") as f:
            json.dump(dict({"scenario": "paymentFailure", "schema_version": 5, "client": "cli", "mcp_endpoint": ":1/mcp/"}, **(meta or {})), f)
        with graded():
            return score.score(d, call_grader=True)[0]

    def test_clean_run_passes_with_sandbox_ok(self):
        s = self.score([init()] + call(J[0]) + [PASS_FINAL], tools_json=["get_trace_errors", "search_traces"])
        self.assertEqual((s["sandbox_ok"], s["sandbox_violations"], s["verdict"]), (True, [], "PASS"))

    def test_extra_init_tool_is_invalid(self):
        s = self.score([init(("StructuredOutput", "Bash") + tuple(J))] + call(J[0]) + [PASS_FINAL])
        self.assertEqual((s["sandbox_ok"], s["verdict"]), (False, "INVALID"))
        self.assertEqual(s["sandbox_violations"], ["init_tool_extra:Bash"])

    def test_init_must_match_the_batch_tools_json_exactly(self):
        s = self.score([init()] + call(J[0]) + [PASS_FINAL], tools_json=["get_trace_errors", "get_services"])
        self.assertEqual(s["sandbox_violations"], ["init_tool_extra:mcp__jaeger__search_traces",
                                                   "init_tool_missing:mcp__jaeger__get_services"])
        self.assertEqual(s["verdict"], "INVALID")

    def test_cli_init_must_list_structured_output(self):
        s = self.score([init(J)] + call(J[0]) + [PASS_FINAL])
        self.assertEqual(s["sandbox_violations"], ["init_tool_missing:StructuredOutput"])

    def test_api_init_never_lists_structured_output(self):
        s = self.score([init()] + call(J[0]) + [PASS_FINAL], meta={"client": "api"})
        self.assertEqual(s["sandbox_violations"], ["init_tool_extra:StructuredOutput"])

    def test_mcp_servers_must_be_exactly_jaeger(self):
        for servers, want in (([], "mcp_servers:"), (SERVERS + [{"name": "fs"}], "mcp_servers:jaeger,fs"),
                              ([{"name": "other"}], "mcp_servers:other")):
            s = self.score([init(servers=servers)] + call(J[0]) + [PASS_FINAL])
            self.assertEqual((s["sandbox_violations"], s["verdict"]), ([want], "INVALID"))

    def test_executed_unknown_tool_is_invalid(self):
        s = self.score([init()] + call("Bash", "t2", "ok") + call(J[0]) + [PASS_FINAL])
        self.assertEqual((s["sandbox_violations"], s["verdict"]), (["executed_unknown_tool:Bash"], "INVALID"))

    def test_rejected_call_is_an_attempt_and_not_a_jaeger_call(self):
        s = self.score([init()] + call(J[0]) + REJECTED + [PASS_FINAL], tools_json=["get_trace_errors", "search_traces"])
        self.assertEqual(s["sandbox_violations"], ["attempted_unknown_tool:mcp__jaeger__StructuredOutput"])
        # refused by the client: recorded, but the sandbox held, so the run is still scored
        self.assertEqual((s["verdict"], s["sandbox_ok"]), ("PASS", True))
        self.assertEqual((s["tool_calls"], s["call_errors"], s["call_sequence"]), (1, 0, ["get_trace_errors"]))
        self.assertEqual(s["non_jaeger_tool_calls"], ["mcp__jaeger__StructuredOutput"])

    def test_hallucinated_name_is_not_a_jaeger_call_without_any_list(self):
        s = self.score([{"type": "system", "subtype": "init", "mcp_servers": SERVERS}] + call(J[0]) + REJECTED + [PASS_FINAL],
                       meta={"client": "codex"})
        self.assertEqual(s["tool_calls"], 1)
        self.assertEqual(s["sandbox_violations"], ["attempted_unknown_tool:mcp__jaeger__StructuredOutput"])

    def test_codex_error_item_is_not_a_tool_call(self):
        events = [{"type": "system", "subtype": "init", "mcp_servers": SERVERS}] + call(J[0]) + call("error", "e1", "x")
        s = self.score(events + [PASS_FINAL], meta={"client": "codex"}, tools_json=["get_trace_errors"])
        self.assertEqual((s["sandbox_ok"], s["verdict"]), (True, "PASS"))

    def test_tools_off_arm_needs_no_server_and_no_jaeger_tools(self):
        ok = self.score([init(("StructuredOutput",), [])] + [PASS_FINAL], meta={"mcp_endpoint": None}, tools_json=["x"])
        self.assertEqual(ok["sandbox_violations"], [])
        bad = self.score([init()] + [PASS_FINAL], meta={"mcp_endpoint": None})
        self.assertEqual(bad["sandbox_violations"], ["init_tool_extra:mcp__jaeger__get_trace_errors",
                                                     "init_tool_extra:mcp__jaeger__search_traces", "mcp_servers:jaeger"])

    def test_no_init_event_is_invalid(self):
        s = self.score(call(J[0]) + [PASS_FINAL])
        self.assertEqual((s["sandbox_violations"], s["verdict"]), (["no_init_event"], "INVALID"))

    def test_fixture_ok_false_is_invalid_without_touching_sandbox_ok(self):
        # meta.json round-trips through JSON, so before/after come back as lists, not tuples.
        changes = [{"container": "recommendation", "before": ["0", "false", "t0"], "after": ["1", "true", "t1"]}]
        s = self.score([init()] + call(J[0]) + [PASS_FINAL], meta={"fixture_ok": False, "fixture_changes": changes})
        self.assertEqual((s["verdict"], s["fixture_ok"], s["fixture_changes"]), ("INVALID", False, changes))
        self.assertEqual((s["sandbox_ok"], s["sandbox_violations"]), (True, []))  # a fixture change is not a sandbox breach

    def test_fixture_ok_true_scores_normally(self):
        s = self.score([init()] + call(J[0]) + [PASS_FINAL], meta={"fixture_ok": True, "fixture_changes": []})
        self.assertEqual((s["verdict"], s["fixture_ok"]), ("PASS", True))

    def test_fixture_ok_absent_defaults_true(self):
        s = self.score([init()] + call(J[0]) + [PASS_FINAL])
        self.assertEqual((s["verdict"], s["fixture_ok"], s["fixture_changes"]), ("PASS", True, []))


class TestInvalidCounted(unittest.TestCase):
    def test_band_counts_invalid_apart_and_never_as_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = [dict(init(), tools=["StructuredOutput", "Bash"])] + call(J[0]) + [PASS_FINAL]
            good = [init()] + call(J[0]) + [PASS_FINAL]
            rc, out = run_band(make_batch(tmp, [good, bad]))
        self.assertIn("n=2 passes=1", out)
        self.assertIn("ERROR=0 INVALID=1 LEAK=0 stops=", out)
        self.assertIn("noskill: 1/2 runs INVALID", out)

    def test_verify_fails_on_an_invalid_run(self):
        with tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as records:
            b = make_batch(runs, [[init(("StructuredOutput", "Bash"))] + call(J[0]) + [PASS_FINAL]])
            with open(os.path.join(b, "scores.jsonl"), "w") as f:
                f.write(json.dumps({"dir": "0-noskill", "arm": "noskill", "verdict": "INVALID"}) + "\n")
            with open(os.path.join(records, "INDEX.md"), "w") as f:
                f.write(bench.index_text(records))
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"RUNS_DIR": runs}), mock.patch.object(bench, "RECORDS", records), \
                    contextlib.redirect_stdout(out):
                rc = bench.main(["verify"])
        self.assertEqual(rc, 1)
        self.assertIn("init_tool_extra:Bash", out.getvalue())
        self.assertIn("verify: FAIL - 1 run(s) INVALID", out.getvalue())

    def test_judge_counts_invalid_and_never_as_pass(self):
        m = judge.arm_metrics([{"arm": "a", "verdict": "INVALID"}, {"arm": "a", "verdict": "PASS"}], "a", [])
        self.assertEqual((m["n"], m["pass_count"], m["invalid"]), (2, 1, 1))


class TestInvalidCause(unittest.TestCase):
    def test_a_fixture_only_invalid_row_is_not_blamed_on_the_sandbox(self):
        # the shape of the 2026-09-30 mainskill rows: sandbox held, fixture changed under the trial
        row = {"verdict": "INVALID", "sandbox_ok": True, "fixture_ok": False}
        self.assertEqual(judge.invalid_cause([row]), "fixture")
        self.assertEqual(judge.invalid_cause([row, dict(row, sandbox_ok=False)]), "sandbox and fixture")
        self.assertEqual(judge.invalid_cause([dict(row, sandbox_ok=False, fixture_ok=True)]), "sandbox")


if __name__ == "__main__":
    unittest.main()
