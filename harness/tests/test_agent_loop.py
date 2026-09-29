#!/usr/bin/env python3
"""Tests for the owned agent loop. No network beyond 127.0.0.1, no LLM call.

Run with: python3 -m unittest discover -s harness/tests
"""
import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
HARNESS_DIR = LOOP_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, LOOP_DIR)
sys.path.insert(0, TESTS_DIR)

import agent_loop  # noqa: E402
import fake_mcp_server  # noqa: E402
from fake_grader import graded  # noqa: E402
from fake_provider import FakeProvider  # noqa: E402

spec = importlib.util.spec_from_file_location("score", os.path.join(HARNESS_DIR, "score.py"))
score_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(score_mod)

VERDICT = {
    "root_cause_service": "payment",
    "root_cause_operation": "charge",
    "mechanism": "The payment charge span carries an Invalid token status.",
    "cascading": [{"service": "checkout", "operation": "oteldemo.PaymentService/Charge"}],
    "confidence": "high",
    "evidence_span_ids": ["12a540814b076b06"],
    "abstain": False,
}

USAGE1 = {"input_tokens": 1200, "output_tokens": 80, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
USAGE2 = {"input_tokens": 300, "output_tokens": 400, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1500}


def two_turn_script():
    """Turn 1: thinking + two parallel tool_use. Turn 2: thinking + verdict text + end_turn."""
    return {"turns": [
        {
            "content": [
                {"type": "thinking", "thinking": "List services, then look at a failing trace.", "signature": "sig1"},
                {"type": "tool_use", "id": "toolu_fake_1", "name": "mcp__jaeger__get_services", "input": {}},
                {"type": "tool_use", "id": "toolu_fake_2", "name": "mcp__jaeger__get_trace_errors",
                 "input": {"trace_id": "abc123"}},
            ],
            "stop_reason": "tool_use",
            "usage": USAGE1,
            "request_id": "req_fake_1",
        },
        {
            "content": [
                {"type": "thinking", "thinking": "The payment charge span has the error.", "signature": "sig2"},
                {"type": "text", "text": json.dumps(VERDICT)},
            ],
            "stop_reason": "end_turn",
            "usage": USAGE2,
            "request_id": "req_fake_2",
        },
    ]}


class LoopTestBase(unittest.TestCase):
    def setUp(self):
        self.srv, self.url = fake_mcp_server.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.mcp_config = os.path.join(self.root, "mcp.json")
        with open(self.mcp_config, "w") as f:
            json.dump({"mcpServers": {"jaeger": {"type": "http", "url": self.url}}}, f)
        self.out_dir = os.path.join(self.root, "runs", "paymentFailure", "noskill-20260101T000000Z")

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def write_script(self, script):
        p = os.path.join(self.root, "script.json")
        with open(p, "w") as f:
            json.dump(script, f)
        return p

    def base_args(self, script_path, extra=None):
        args = [
            "--scenario", "paymentFailure",
            "--arm", "noskill",
            "--model", "claude-sonnet-5",
            "--prompt-file", os.path.join(HARNESS_DIR, "prompts", "noskill.txt"),
            "--system-prompt-file", os.path.join(HARNESS_DIR, "system-prompt.txt"),
            "--schema-file", os.path.join(HARNESS_DIR, "verdict-schema.json"),
            "--scenario-file", os.path.join(HARNESS_DIR, "scenarios", "paymentFailure.json"),
            "--mcp-config", self.mcp_config,
            "--out-dir", self.out_dir,
        ]
        return args + (extra or [])

    def main(self, script, extra=None, provider_factory=None, **kw):
        """Run the loop on a scripted FakeProvider (or the given factory)."""
        path = self.write_script(script)
        factory = provider_factory or (lambda args, schema: FakeProvider(path, model=args.model))
        return agent_loop.main(self.base_args(path, extra), provider_factory=factory, **kw)

    def read_jsonl(self, name):
        with open(os.path.join(self.out_dir, name)) as f:
            return [json.loads(l) for l in f if l.strip()]

    def read_json(self, name):
        with open(os.path.join(self.out_dir, name)) as f:
            return json.load(f)


class TestFullRun(LoopTestBase):
    def run_two_turns(self):
        holder = {}

        def factory(args, output_schema):
            holder["p"] = FakeProvider(os.path.join(self.root, "script.json"), model=args.model)
            return holder["p"]

        rc = self.main(two_turn_script(), provider_factory=factory)
        return rc, holder["p"]

    def test_run_dir_shape_and_score(self):
        rc, _ = self.run_two_turns()
        self.assertEqual(rc, 0)
        for name in ["agent_loop.json", "prompt.txt", "system-prompt.txt", "stream.jsonl",
                     "reasoning.jsonl", "tools.json", "exit.txt"]:
            self.assertTrue(os.path.isfile(os.path.join(self.out_dir, name)), name)
        with open(os.path.join(self.out_dir, "exit.txt")) as f:
            self.assertEqual(f.read().strip(), "exit=0")
        with open(os.path.join(self.out_dir, "prompt.txt"), "rb") as a, \
                open(os.path.join(HARNESS_DIR, "prompts", "noskill.txt"), "rb") as b:
            self.assertEqual(a.read(), b.read())

        with open(os.path.join(self.out_dir, "meta.json"), "w") as f:  # bench.py writes it around the loop
            json.dump({"scenario": "paymentFailure", "schema_version": 5}, f)
        with graded():
            summary, _ = score_mod.score(self.out_dir, call_grader=True)
        self.assertEqual(summary["verdict_source"], "structured")
        self.assertEqual(summary["verdict"], "PASS")
        self.assertEqual(summary["tool_calls"], 2)
        self.assertEqual(summary["call_errors"], 1)
        self.assertEqual(summary["call_sequence"], ["get_services", "get_trace_errors"])
        self.assertEqual(summary["non_jaeger_tool_calls"], [])
        self.assertEqual(summary["model_asserted"], "claude-sonnet-5")
        self.assertEqual(summary["mcp_tools_visible"], 3)
        self.assertEqual(summary["num_turns"], 2)
        self.assertEqual(summary["stop"], "success")
        self.assertEqual(summary["input_tokens_total"], 1200 + 300 + 1500)
        self.assertEqual(summary["output_tokens"], 480)
        # $2/$10 per MTok, cache read 0.1x input: (1200+300)*2e-6 + 1500*0.2e-6 + 480*10e-6
        self.assertAlmostEqual(summary["cost_usd"], 0.003 + 0.0003 + 0.0048, places=9)

    def test_meta_agent_loop_block(self):
        self.run_two_turns()
        meta = self.read_json("agent_loop.json")
        self.assertEqual(meta["scenario"], "paymentFailure")
        self.assertEqual(meta["arm"], "noskill")
        self.assertEqual(meta["model_requested"], "claude-sonnet-5")
        for k in ["prompt_sha256", "system_prompt_sha256", "scenario_sha256", "verdict_schema_sha256", "started_utc"]:
            self.assertIn(k, meta)
        al = meta["agent_loop"]
        self.assertEqual(al["loop"], "owned")
        self.assertEqual(al["model"], "claude-sonnet-5")
        self.assertEqual(al["temperature"], "not_sent")
        self.assertEqual(al["thinking"], {"type": "adaptive", "display": "summarized"})
        self.assertEqual(al["effort"], "high")
        self.assertEqual(al["max_turns"], 30)
        self.assertEqual(al["tool_choice"], "auto")
        self.assertEqual(al["request_ids"], ["req_fake_1", "req_fake_2"])
        self.assertIn("claude-sonnet-5", al["price_table_usd_per_mtok"]["models"])
        for k in ["loop_version", "api_version", "max_tokens"]:
            self.assertIn(k, al)

    def test_stream_events_shape(self):
        self.run_two_turns()
        ev = self.read_jsonl("stream.jsonl")
        init = ev[0]
        self.assertEqual((init["type"], init["subtype"]), ("system", "init"))
        self.assertEqual(init["tools"], ["mcp__jaeger__get_services", "mcp__jaeger__search_traces",
                                         "mcp__jaeger__get_trace_errors"])
        self.assertEqual(init["mcp_servers"][0]["name"], "jaeger")
        assistants = [e for e in ev if e["type"] == "assistant"]
        self.assertEqual(len(assistants), 2)
        self.assertEqual(assistants[0]["message"]["content"][0]["type"], "thinking")
        self.assertEqual(assistants[0]["request_id"], "req_fake_1")
        users = [e for e in ev if e["type"] == "user"]
        self.assertEqual(len(users), 1)
        blocks = users[0]["message"]["content"]
        self.assertEqual([b["tool_use_id"] for b in blocks], ["toolu_fake_1", "toolu_fake_2"])
        self.assertEqual([b["is_error"] for b in blocks], [False, True])
        self.assertIn("checkout", blocks[0]["content"])
        final = ev[-1]
        self.assertEqual(final["type"], "result")
        self.assertEqual(final["structured_output"], VERDICT)
        self.assertEqual(json.loads(final["result"]), VERDICT)
        self.assertFalse(final["is_error"])
        self.assertEqual((final["num_turns"], final["num_tool_calls"]), (2, 2))

    def test_parallel_results_in_one_user_message_and_content_appended_verbatim(self):
        _, provider = self.run_two_turns()
        self.assertEqual(len(provider.calls), 2)
        msgs = provider.calls[1]["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
        # assistant content goes back verbatim, thinking block and signature included
        self.assertEqual(msgs[1]["content"], two_turn_script()["turns"][0]["content"])
        results = msgs[2]["content"]
        self.assertEqual([r["type"] for r in results], ["tool_result", "tool_result"])
        self.assertTrue(results[1]["is_error"])
        # tools sent to the model carry the prefixed names and the server's schemas
        tools = provider.calls[0]["tools"]
        self.assertEqual(tools[1]["name"], "mcp__jaeger__search_traces")
        self.assertEqual(tools[1]["input_schema"], fake_mcp_server.TOOLS[1]["inputSchema"])
        self.assertEqual(tools[1]["description"], fake_mcp_server.TOOLS[1]["description"])

    def test_tools_json_verbatim(self):
        self.run_two_turns()
        tj = self.read_json("tools.json")
        self.assertEqual(tj["tools"], fake_mcp_server.TOOLS)
        self.assertEqual(tj["server_url"], self.url)

    def test_reasoning_jsonl(self):
        self.run_two_turns()
        lines = self.read_jsonl("reasoning.jsonl")
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["turn"], 1)
        self.assertEqual(lines[0]["thinking"], "List services, then look at a failing trace.")
        self.assertEqual(lines[0]["text"], "")
        self.assertEqual(lines[0]["tool_calls"], [
            {"name": "mcp__jaeger__get_services", "input": {}},
            {"name": "mcp__jaeger__get_trace_errors", "input": {"trace_id": "abc123"}},
        ])
        self.assertEqual(lines[0]["stop_reason"], "tool_use")
        self.assertEqual(lines[0]["usage"], USAGE1)
        self.assertEqual(lines[1]["stop_reason"], "end_turn")
        self.assertEqual(json.loads(lines[1]["text"]), VERDICT)

    def test_mcp_session_header_and_raw_log(self):
        self.run_two_turns()
        calls = [r for r in self.srv.requests if r["body"].get("method") == "tools/call"]
        self.assertEqual(len(calls), 2)
        for c in calls:
            self.assertEqual(c["headers"].get("Mcp-Session-Id"), fake_mcp_server.SESSION_ID)
        raw = self.read_jsonl("mcp.jsonl")
        self.assertTrue(any(r.get("content_type", "").startswith("text/event-stream") for r in raw))


class TestEdgeCases(LoopTestBase):
    run_script = LoopTestBase.main

    def test_empty_thinking_still_yields_reasoning_line(self):
        rc = self.run_script({"turns": [{
            "content": [
                {"type": "thinking", "thinking": "", "signature": "sigx"},
                {"type": "text", "text": json.dumps(VERDICT)},
            ],
            "stop_reason": "end_turn", "usage": USAGE2, "request_id": "req_e"}]})
        self.assertEqual(rc, 0)
        lines = self.read_jsonl("reasoning.jsonl")
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["thinking"], "")
        self.assertEqual(lines[0]["thinking_blocks"], 1)

    def test_max_turns_is_a_scorable_stop(self):
        # A max-turns stop wrote its result event, so the cell did not fail (exit 0).
        turn = two_turn_script()["turns"][0]
        rc = self.run_script({"turns": [turn, turn]}, ["--max-turns", "1"])
        self.assertEqual(rc, 0)
        final = self.read_jsonl("stream.jsonl")[-1]
        self.assertEqual(final["subtype"], "error_max_turns")
        self.assertTrue(final["is_error"])
        with open(os.path.join(self.out_dir, "exit.txt")) as f:
            self.assertEqual(f.read().strip(), "exit=0")

    def test_max_tokens_is_a_scorable_stop_and_tools_not_run(self):
        turn = copy.deepcopy(two_turn_script()["turns"][0])
        turn["stop_reason"] = "max_tokens"
        rc = self.run_script({"turns": [turn]})
        self.assertEqual(rc, 0)
        final = self.read_jsonl("stream.jsonl")[-1]
        self.assertEqual(final["subtype"], "error_max_tokens")
        self.assertFalse([r for r in self.srv.requests if r["body"].get("method") == "tools/call"])

    def test_refusal_records_stop_details(self):
        rc = self.run_script({"turns": [{
            "content": [], "stop_reason": "refusal",
            "stop_details": {"type": "refusal", "category": "cyber", "explanation": "x"},
            "usage": USAGE1, "request_id": "req_r"}]})
        self.assertEqual(rc, 0)
        final = self.read_jsonl("stream.jsonl")[-1]
        self.assertEqual(final["subtype"], "error_refusal")
        self.assertEqual(final["stop_details"]["category"], "cyber")
        self.assertEqual(self.read_jsonl("reasoning.jsonl")[0]["stop_reason"], "refusal")

    def test_unknown_tool_is_error_not_dropped(self):
        script = two_turn_script()
        script["turns"][0]["content"] = [
            {"type": "tool_use", "id": "toolu_x", "name": "mcp__jaeger__no_such_tool", "input": {}},
        ]
        rc = self.run_script(script)
        self.assertEqual(rc, 0)
        users = [e for e in self.read_jsonl("stream.jsonl") if e["type"] == "user"]
        b = users[0]["message"]["content"][0]
        self.assertEqual(b["tool_use_id"], "toolu_x")
        self.assertTrue(b["is_error"])

    def test_schema_invalid_answer_gives_null_structured_output(self):
        bad = dict(VERDICT, confidence="certain")
        rc = self.run_script({"turns": [{
            "content": [{"type": "text", "text": json.dumps(bad)}],
            "stop_reason": "end_turn", "usage": USAGE2, "request_id": "req_b"}]})
        self.assertEqual(rc, 0)
        final = self.read_jsonl("stream.jsonl")[-1]
        self.assertIsNone(final["structured_output"])
        self.assertTrue(final["structured_output_errors"])

    def test_openai_schema_invalid_answer_gives_null_structured_output_too(self):
        bad = dict(VERDICT, confidence="certain")
        last = mock.Mock(content=[{"type": "text", "text": json.dumps(bad)}])
        with open(os.path.join(HARNESS_DIR, "verdict-schema.json")) as f:
            obj, errs = agent_loop.extract_json(last, json.load(f))
        self.assertIsNone(obj)
        self.assertTrue(errs)


class TestArgumentGuards(unittest.TestCase):
    def run_cli(self, *args):
        with tempfile.TemporaryDirectory() as root:
            out = os.path.join(root, "out")
            p = subprocess.run(
                [sys.executable, os.path.join(LOOP_DIR, "agent_loop.py"), "--validate-only", "--out-dir", out, *args],
                capture_output=True, text=True, stdin=subprocess.DEVNULL)
            return p, os.path.exists(out)

    def test_alias_model_rejected(self):
        for alias in ["sonnet", "opus", "claude-sonnet", "claude-opus-5-5[1m]"]:
            p, made = self.run_cli("--model", alias)
            self.assertEqual(p.returncode, 2, alias + ": " + p.stderr)
            self.assertIn("exact model id", p.stderr)
            self.assertFalse(made)

    def test_exact_model_accepted(self):
        p, _ = self.run_cli("--model", "claude-sonnet-5")
        self.assertEqual(p.returncode, 0, p.stderr)


# ---------------------------------------------------------------------------
# Review fixes (2026-09-25) and coverage gaps
# ---------------------------------------------------------------------------

class _RaisingOnCall(agent_loop.MCPClient):
    """Real client against the fake server, but call_tool blows up with a
    non-MCPError for one named tool."""
    def call_tool(self, name, arguments):
        if name == "get_services":
            raise RuntimeError("injected tool failure")
        return super().call_tool(name, arguments)


class _RaisingOnList(agent_loop.MCPClient):
    def list_tools(self):
        raise RuntimeError("injected list_tools failure")


class _RaisingProvider:
    def create(self, messages, tools, system):
        raise ConnectionError("injected provider failure")

    def api_version(self):
        return None

    def output_schema_sent(self):
        return None


def _verdict_turn(stop_reason="end_turn", request_id="req_v"):
    return {"content": [{"type": "text", "text": json.dumps(VERDICT)}],
            "stop_reason": stop_reason, "usage": USAGE2, "request_id": request_id}


class TestReviewFixes(LoopTestBase):
    run_script = LoopTestBase.main

    def final(self):
        return self.read_jsonl("stream.jsonl")[-1]

    def exit_line(self):
        with open(os.path.join(self.out_dir, "exit.txt")) as f:
            return f.read().strip()

    # finding 2: a non-MCPError from one tool call becomes an is_error tool_result
    def test_tool_call_exception_becomes_is_error_result(self):
        rc = self.run_script(two_turn_script(), mcp_client_factory=_RaisingOnCall)
        self.assertEqual(rc, 0)
        users = [e for e in self.read_jsonl("stream.jsonl") if e["type"] == "user"]
        blocks = users[0]["message"]["content"]
        self.assertTrue(blocks[0]["is_error"])
        self.assertIn("injected tool failure", blocks[0]["content"])
        self.assertEqual(self.final()["subtype"], "success")

    # finding 2: an exception escaping run_trial still leaves a result event
    def test_unhandled_exception_still_writes_result_event(self):
        with self.assertRaises(RuntimeError):
            self.run_script(two_turn_script(), mcp_client_factory=_RaisingOnList)
        final = self.final()
        self.assertEqual(final["type"], "result")
        self.assertEqual(final["subtype"], "error_during_execution")
        self.assertIn("injected list_tools failure", final["error"])
        self.assertEqual(final["num_turns"], 0)
        self.assertEqual(final["total_cost_usd"], 0.0)
        self.assertNotEqual(self.exit_line(), "exit=0")
        self.assertEqual(self.read_json("agent_loop.json")["agent_loop"]["result_subtype"], "error_during_execution")

    # finding 4: a failed notifications/initialized does not fail the session
    def test_initialized_notification_failure_is_recorded_not_fatal(self):
        self.srv.fail_initialized_notification = True
        rc = self.run_script(two_turn_script())
        self.assertEqual(rc, 0)
        raw = self.read_jsonl("mcp.jsonl")
        notes = [r for r in raw if r["request"].get("method") == "notifications/initialized"]
        self.assertEqual(notes[0]["http_status"], 500)
        init = self.read_jsonl("stream.jsonl")[0]
        self.assertIn("500", init["mcp_servers"][0]["initialized_notification_error"])

    # coverage: the budget stop actually stops a trial
    def test_budget_stop(self):
        turn = copy.deepcopy(two_turn_script()["turns"][0])
        turn["usage"] = {"input_tokens": 0, "output_tokens": 100000}  # $1.00 at $10/MTok
        rc = self.run_script({"turns": [turn, turn, _verdict_turn()]}, ["--max-budget-usd", "0.5"])
        self.assertEqual(rc, 0)
        final = self.final()
        self.assertEqual(final["subtype"], "error_max_budget_usd")
        self.assertEqual(final["num_turns"], 1)
        self.assertAlmostEqual(final["total_cost_usd"], 1.0, places=9)

    # coverage: pause_turn resends and continues
    def test_pause_turn_continues(self):
        pause = {"content": [{"type": "text", "text": ""}], "stop_reason": "pause_turn",
                 "usage": USAGE1, "request_id": "req_p"}
        rc = self.run_script({"turns": [pause, _verdict_turn()]})
        self.assertEqual(rc, 0)
        final = self.final()
        self.assertEqual(final["subtype"], "success")
        self.assertEqual(final["num_turns"], 2)
        self.assertEqual(final["structured_output"], VERDICT)

    # coverage: provider.create raising
    def test_provider_error_is_error_during_execution(self):
        rc = self.run_script(two_turn_script(), provider_factory=lambda a, s: _RaisingProvider())
        self.assertEqual(rc, 1)
        final = self.final()
        self.assertEqual(final["subtype"], "error_during_execution")
        self.assertIn("injected provider failure", final["error"])
        self.assertEqual(final["num_turns"], 0)
        self.assertEqual(self.exit_line(), "exit=1")

    # coverage: MCP server not reachable
    def test_mcp_connect_failure(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with open(self.mcp_config, "w") as f:
            json.dump({"mcpServers": {"jaeger": {"type": "http", "url": "http://127.0.0.1:%d/mcp/" % port}}}, f)
        rc = self.run_script(two_turn_script())
        self.assertEqual(rc, 1)
        final = self.final()
        self.assertEqual(final["subtype"], "error_during_execution")
        self.assertIn("transport error", final["error"])
        self.assertEqual(self.read_jsonl("stream.jsonl")[0]["mcp_servers"][0]["status"], "failed")
        self.assertEqual(self.exit_line(), "exit=1")

    # an arm with tools false: an empty mcpServers runs with no MCP connection and no tools
    def test_no_mcp_servers_runs_without_tools(self):
        with open(self.mcp_config, "w") as f:
            json.dump({"mcpServers": {}}, f)
        holder = {}

        def factory(args, output_schema):
            holder["p"] = FakeProvider(os.path.join(self.root, "script.json"), model=args.model)
            return holder["p"]

        rc = self.run_script({"turns": [_verdict_turn()]}, provider_factory=factory)
        self.assertEqual(rc, 0)
        init = self.read_jsonl("stream.jsonl")[0]
        self.assertEqual((init["tools"], init["mcp_servers"]), ([], []))
        self.assertEqual(holder["p"].calls[0]["tools"], [])
        self.assertEqual(self.srv.requests, [])
        self.assertEqual(self.final()["structured_output"], VERDICT)

    # coverage: an unmapped stop reason
    def test_unmapped_stop_reason(self):
        rc = self.run_script({"turns": [_verdict_turn(stop_reason="model_context_window_exceeded")]})
        self.assertEqual(rc, 0)
        self.assertEqual(self.final()["subtype"], "error_stop_model_context_window_exceeded")


class TestReviewArgumentGuards(unittest.TestCase):
    run_cli = TestArgumentGuards.run_cli

    # finding 1: unpriced models are refused (no budget ceiling without a price)
    def test_unpriced_model_rejected(self):
        p, made = self.run_cli("--model", "claude-opus-4-8")
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("price", p.stderr)
        self.assertIn("claude-sonnet-5", p.stderr)
        self.assertFalse(made)


# ---------------------------------------------------------------------------
# Messages API provider (stdlib HTTP), against a local fake endpoint.
# ---------------------------------------------------------------------------

class _FakeMessagesHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        self.server.seen.append((dict(self.headers), json.loads(self.rfile.read(n))))
        status, doc = self.server.replies.pop(0)
        body = json.dumps(doc).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("request-id", "req_local_%d" % len(self.server.seen))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestMessagesProvider(unittest.TestCase):
    MSG = {"id": "msg_local", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
           "content": [{"type": "thinking", "thinking": "plan", "signature": "sigL"},
                       {"type": "tool_use", "id": "toolu_L", "name": "mcp__jaeger__get_services", "input": {}}],
           "stop_reason": "tool_use", "stop_details": None, "usage": {"input_tokens": 10, "output_tokens": 30}}

    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeMessagesHandler)
        self.srv.seen, self.srv.replies = [], []
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        with open(os.path.join(HARNESS_DIR, "verdict-schema.json")) as f:
            self.schema = {k: v for k, v in json.load(f).items() if k != "$schema"}
        from providers.anthropic_provider import AnthropicProvider
        self.p = AnthropicProvider(model="claude-sonnet-5", max_tokens=64000, effort="xhigh",
                                   output_schema=self.schema, api_key="local-test-not-a-key",
                                   url="http://127.0.0.1:%d/v1/messages" % self.srv.server_address[1], backoff_s=0)
        self.tools = [{"name": "mcp__jaeger__get_services", "description": "d", "input_schema": {"type": "object"}}]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_request_shape_retry_and_response_mapping(self):
        self.srv.replies = [(529, {"type": "error", "error": {"type": "overloaded_error"}}), (200, self.MSG)]
        r = self.p.create([{"role": "user", "content": "hi"}], self.tools, "sys")
        self.assertEqual(len(self.srv.seen), 2)  # the 529 was retried
        headers, body = self.srv.seen[1]
        headers = {k.lower(): v for k, v in headers.items()}
        self.assertEqual(headers["x-api-key"], "local-test-not-a-key")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertNotIn("anthropic-beta", headers)
        self.assertEqual((body["model"], body["max_tokens"], body["system"]), ("claude-sonnet-5", 64000, "sys"))
        self.assertEqual(body["thinking"], {"type": "adaptive", "display": "summarized"})
        self.assertEqual(body["output_config"]["effort"], "xhigh")
        self.assertEqual(body["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(body["output_config"]["format"]["schema"]["required"], self.schema["required"])
        self.assertEqual((body["tools"], body["tool_choice"]), (self.tools, {"type": "auto"}))
        self.assertEqual(body["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("stream", body)
        for k in ["temperature", "top_p", "top_k"]:
            self.assertNotIn(k, body)
        self.assertEqual((r.request_id, r.stop_reason, r.model, r.content), ("req_local_2", "tool_use", "claude-sonnet-5",
                                                                           self.MSG["content"]))
        # No tools (the notools arm): neither tools nor tool_choice is sent.
        self.srv.replies = [(200, self.MSG)]
        self.p.create([{"role": "user", "content": "hi"}], [], "sys")
        self.assertFalse({"tools", "tool_choice"} & set(self.srv.seen[2][1]))

    def test_client_error_is_not_retried_and_carries_the_body(self):
        self.srv.replies = [(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "bad field"}})]
        from providers.anthropic_provider import APIError
        with self.assertRaises(APIError) as cm:
            self.p.create([{"role": "user", "content": "hi"}], self.tools, "sys")
        self.assertEqual(len(self.srv.seen), 1)
        self.assertIn("HTTP 400", str(cm.exception))
        self.assertIn("bad field", str(cm.exception))



# ---------------------------------------------------------------------------
# OpenAI-compatible chat completions provider, against the same local fake endpoint.
# ---------------------------------------------------------------------------

def _chat(message, finish_reason, usage=None):
    return {"id": "chatcmpl_local", "object": "chat.completion", "model": "gpt-local",
            "choices": [{"index": 0, "message": dict({"role": "assistant"}, **message), "finish_reason": finish_reason}],
            "usage": usage or {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}


TOOL_CALL = _chat({"content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {
    "name": "mcp__jaeger__get_trace_errors", "arguments": json.dumps({"trace_id": "abc"})}}]}, "tool_calls")


from providers.openai_provider import from_openai as agent_loop_from_openai  # noqa: E402


class TestOpenAIProvider(unittest.TestCase):
    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeMessagesHandler)
        self.srv.seen, self.srv.replies = [], []
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d/v1" % self.srv.server_address[1]
        with open(os.path.join(HARNESS_DIR, "verdict-schema.json")) as f:
            self.schema = {k: v for k, v in json.load(f).items() if k != "$schema"}
        from providers.openai_provider import OpenAIProvider
        self.p = OpenAIProvider(model="gpt-local", max_tokens=64000, output_schema=self.schema,
                                api_key="local-test-not-a-key", base_url=self.base + "/", backoff_s=0)
        self.tools = [{"name": "mcp__jaeger__get_trace_errors", "description": "d", "input_schema": {"type": "object"}}]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_tool_call_round_trip_and_request_shape(self):
        self.srv.replies = [(529, {"error": {"message": "overloaded"}}), (200, TOOL_CALL)]
        r = self.p.create([{"role": "user", "content": "hi"}], self.tools, "sys")
        self.assertEqual(len(self.srv.seen), 2)  # the 529 was retried
        headers, body = self.srv.seen[1]
        headers = {k.lower(): v for k, v in headers.items()}
        self.assertEqual(headers["authorization"], "Bearer local-test-not-a-key")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual((body["model"], body["max_tokens"]), ("gpt-local", 64000))
        self.assertEqual(body["messages"], [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}])
        self.assertEqual(body["tools"], [{"type": "function", "function": {
            "name": "mcp__jaeger__get_trace_errors", "description": "d", "parameters": {"type": "object"}}}])
        self.assertEqual(body["tool_choice"], "auto")
        rf = body["response_format"]
        self.assertEqual((rf["type"], rf["json_schema"]["name"], rf["json_schema"]["strict"]), ("json_schema", "verdict", False))
        for k in ("temperature", "thinking", "reasoning_effort", "stream", "top_p"):  # no effort set on this provider
            self.assertNotIn(k, body)
        self.assertEqual(r.content, [{"type": "tool_use", "id": "call_1", "name": "mcp__jaeger__get_trace_errors",
                                      "input": {"trace_id": "abc"}}])
        self.assertEqual((r.stop_reason, r.usage, r.request_id, r.model, r.message_id),
                         ("tool_use", {"input_tokens": 11, "output_tokens": 7, "reasoning_tokens": 0}, "req_local_2", "gpt-local", "chatcmpl_local"))

        # Second turn: the loop's Anthropic-shaped history goes out as assistant.tool_calls and a tool message.
        self.srv.replies = [(200, _chat({"content": "done"}, "stop"))]
        history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": r.content},
                   {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "boom",
                                                 "is_error": False}]}]
        self.p.create(history, self.tools, "sys")
        msgs = self.srv.seen[2][1]["messages"]
        self.assertEqual(msgs[2], {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function",
                                   "function": {"name": "mcp__jaeger__get_trace_errors", "arguments": '{"trace_id": "abc"}'}}]})
        self.assertEqual(msgs[3], {"role": "tool", "tool_call_id": "call_1", "content": "boom"})
        # No tools (the notools arm): neither tools nor tool_choice is sent.
        self.srv.replies = [(200, _chat({"content": "x"}, "stop"))]
        self.p.create([{"role": "user", "content": "hi"}], [], "sys")
        self.assertFalse({"tools", "tool_choice"} & set(self.srv.seen[3][1]))

    def test_finish_reason_mapping(self):
        for finish, stop in (("stop", "end_turn"), ("length", "max_tokens"), ("content_filter", "refusal"),
                             ("tool_calls", "tool_use")):
            msg = TOOL_CALL["choices"][0]["message"] if finish == "tool_calls" else {"content": "x"}
            self.srv.replies = [(200, _chat(msg, finish))]
            self.assertEqual(self.p.create([{"role": "user", "content": "hi"}], [], "sys").stop_reason, stop)

    def test_response_format_400_falls_back_once_for_the_run(self):
        self.srv.replies = [(400, {"error": {"message": "response_format json_schema is not supported"}}),
                            (200, _chat({"content": "x"}, "stop")), (200, _chat({"content": "y"}, "stop"))]
        self.p.create([{"role": "user", "content": "hi"}], [], "sys")
        self.p.create([{"role": "user", "content": "hi"}], [], "sys")
        self.assertEqual(["response_format" in b for _, b in self.srv.seen], [True, False, False])
        self.assertFalse(self.p.response_format_supported)
        # Any other 400 is not retried and carries the body.
        from providers.anthropic_provider import APIError
        self.srv.replies = [(400, {"error": {"message": "bad model"}})]
        with self.assertRaises(APIError) as cm:
            self.p.create([{"role": "user", "content": "hi"}], [], "sys")
        self.assertEqual(len(self.srv.seen), 4)
        self.assertIn("bad model", str(cm.exception))

    def test_loop_extracts_fenced_json_and_records_provider(self):
        srv, url = fake_mcp_server.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        with tempfile.TemporaryDirectory() as root:
            mcp = os.path.join(root, "mcp.json")
            with open(mcp, "w") as f:
                json.dump({"mcpServers": {"jaeger": {"type": "http", "url": url}}}, f)
            out = os.path.join(root, "out")
            fenced = "```json\n%s\n```" % json.dumps(VERDICT)
            self.srv.replies = [(200, TOOL_CALL), (200, _chat({"content": fenced}, "stop"))]
            env = {"OPENAI_API_KEY": "local-test-not-a-key", "OPENAI_BASE_URL": self.base}
            with mock.patch.dict(os.environ, env):
                rc = agent_loop.main([
                    "--scenario", "paymentFailure", "--arm", "noskill", "--model", "gpt-local", "--provider", "openai",
                    "--prompt-file", os.path.join(HARNESS_DIR, "prompts", "noskill.txt"),
                    "--system-prompt-file", os.path.join(HARNESS_DIR, "system-prompt.txt"),
                    "--schema-file", os.path.join(HARNESS_DIR, "verdict-schema.json"),
                    "--mcp-config", mcp, "--out-dir", out])
            self.assertEqual(rc, 0)
            with open(os.path.join(out, "stream.jsonl")) as f:
                final = [json.loads(l) for l in f][-1]
            self.assertEqual((final["subtype"], final["structured_output"], final["total_cost_usd"]), ("success", VERDICT, None))
            self.assertEqual(self.srv.seen[1][1]["messages"][-1]["role"], "tool")
            with open(os.path.join(out, "agent_loop.json")) as f:
                al = json.load(f)["agent_loop"]
            self.assertEqual((al["provider"], al["api_base_url"], al["thinking"], al["effort"], al["response_format_supported"]),
                             ("openai", "set", "reasoning_effort", "high", True))
            self.assertTrue(al["effort_applied"])  # never the URL itself
            # default_provider_factory strips the schema's "$schema" keyword before anything is sent
            with open(os.path.join(out, "output-schema-sent.json")) as f:
                self.assertNotIn("$schema", json.load(f))
            self.assertNotIn("$schema", self.srv.seen[0][1]["response_format"]["json_schema"]["schema"])
            for name in os.listdir(out):
                with open(os.path.join(out, name)) as f:
                    self.assertNotIn("local-test-not-a-key", f.read(), name)

    def test_effort_sends_reasoning_effort_and_echoes_signed_thinking_blocks(self):
        think = {"type": "thinking", "thinking": "plan", "signature": "sig1"}
        first = _chat({"content": None, "reasoning_content": "plan", "thinking_blocks": [think],
                       "tool_calls": TOOL_CALL["choices"][0]["message"]["tool_calls"]}, "tool_calls",
                      {"prompt_tokens": 5, "completion_tokens": 9, "completion_tokens_details": {"reasoning_tokens": 4}})
        self.srv.replies = [(200, first), (200, _chat({"content": "done"}, "stop"))]
        self.p.effort = "high"
        r = self.p.create([{"role": "user", "content": "hi"}], self.tools, "sys")
        self.assertEqual(self.srv.seen[0][1]["reasoning_effort"], "high")
        self.assertEqual((r.content[0], r.usage["reasoning_tokens"]), (think, 4))
        history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": r.content},
                   {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "x"}]}]
        self.p.create(history, self.tools, "sys")
        asst = self.srv.seen[1][1]["messages"][2]
        self.assertEqual((asst["role"], asst["thinking_blocks"]), ("assistant", [think]))
        # reasoning_content alone (no signature) is recorded but never echoed
        r2 = agent_loop_from_openai({"content": "x", "reasoning_content": "raw"})
        self.assertEqual(r2[0], {"type": "thinking", "thinking": "raw"})
        from providers.openai_provider import to_openai
        self.assertNotIn("thinking_blocks", to_openai([{"role": "assistant", "content": r2}], "s")[1])

    def test_probe_model_refuses_a_mismatch(self):
        for served, want in (("gpt-local", 0), ("other", 3)):
            self.srv.replies = [(200, dict(_chat({"content": "ok"}, "stop"), model=served))]
            env = {"OPENAI_API_KEY": "local-test-not-a-key", "OPENAI_BASE_URL": self.base}
            with mock.patch.dict(os.environ, env):
                rc = agent_loop.main(["--probe-model", "--provider", "openai", "--model", "gpt-local", "--effort", "high"])
            self.assertEqual(rc, want)
            body = self.srv.seen[-1][1]
            self.assertEqual((body["max_tokens"], "reasoning_effort" in body, "response_format" in body), (256, False, False))

    def test_validate_only_needs_no_endpoint(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("OPENAI_")}
        p = subprocess.run([sys.executable, os.path.join(LOOP_DIR, "agent_loop.py"), "--validate-only",
                            "--provider", "openai", "--model", "any/model-id:tag"],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)


if __name__ == "__main__":
    unittest.main()
