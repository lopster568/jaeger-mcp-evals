#!/usr/bin/env python3
"""Tests for harness/tools.py (capture and the description comparison).

A fake MCP streamable-HTTP server runs on 127.0.0.1 (http.server, stdlib)
and answers initialize / notifications/initialized / tools/list either as
application/json or as text/event-stream. No network beyond localhost, no
LLM, no ssh.

Run with: python3 -m unittest discover harness/tests
"""
import hashlib
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

HARNESS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(HARNESS_DIR, "tools.py")

sys.path.insert(0, HARNESS_DIR)
import tools as tools_mod  # noqa: E402

TOOLS_PAGE1 = [
    {"name": "get_critical_path", "description": "Critical path, old wording.",
     "inputSchema": {"type": "object", "properties": {"trace_id": {"type": "string"}}}},
    {"name": "get_trace_errors", "description": "Errors, old wording.",
     "inputSchema": {"type": "object"}},
]
TOOLS_PAGE2 = [
    {"name": "get_trace_topology", "description": "Topology, old wording.",
     "inputSchema": {"type": "object"}},
]
SESSION = "sess-123"


class FakeMCP(http.server.BaseHTTPRequestHandler):
    mode = "json"          # "json", "sse", "http500", "rpc_error"
    seen = []              # (method, session header) per request

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        msg = json.loads(body)
        method = msg.get("method")
        FakeMCP.seen.append((method, self.headers.get("Mcp-Session-Id")))
        if FakeMCP.mode == "http500":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"boom")
            return
        if method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return
        if method != "initialize" and self.headers.get("Mcp-Session-Id") != SESSION:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"missing session")
            return
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fake-jaeger", "version": "0"}}
            resp = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        elif method == "tools/list":
            if FakeMCP.mode == "rpc_error":
                resp = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "nope"}}
            elif (msg.get("params") or {}).get("cursor") == "p2":
                resp = {"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": TOOLS_PAGE2}}
            else:
                resp = {"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": TOOLS_PAGE1, "nextCursor": "p2"}}
        else:
            resp = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32601, "message": "unknown"}}

        if FakeMCP.mode == "sse":
            note = {"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info"}}
            payload = ("event: message\ndata: " + json.dumps(note) + "\n\n"
                       "event: message\ndata: " + json.dumps(resp) + "\n\n").encode()
            ctype = "text/event-stream"
        else:
            payload = json.dumps(resp).encode()
            ctype = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        if method == "initialize":
            self.send_header("Mcp-Session-Id", SESSION)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class TestCaptureTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeMCP)
        cls.port = str(cls.server.server_address[1])
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.out = os.path.join(self.tmp, "tools.json")
        self.env = dict(os.environ, FIXTURE_HOST="127.0.0.1", JAEGER_UI_PORT=self.port,
                        JAEGER_BASE_PATH="/jaeger/ui")
        FakeMCP.seen = []

    def tearDown(self):
        self._tmp.cleanup()

    def capture(self, mode):
        FakeMCP.mode = mode
        return subprocess.run([sys.executable, TOOLS, "capture", "--out", self.out, "--timeout", "5"],
                              capture_output=True, text=True, timeout=30, env=self.env)

    def assert_captured(self, r):
        self.assertEqual(r.returncode, 0, r.stderr)
        tools = json.load(open(self.out))
        self.assertEqual(tools, TOOLS_PAGE1 + TOOLS_PAGE2)
        digest = hashlib.sha256(open(self.out, "rb").read()).hexdigest()
        self.assertEqual(r.stdout.strip(), digest)
        # The recorded shape: tools.json is the array at indent 2, non-ASCII kept, one trailing
        # newline, and tools_list_sha256 is the sha256 of exactly those bytes.
        raw = open(self.out, "rb").read()
        self.assertEqual(raw, (json.dumps(TOOLS_PAGE1 + TOOLS_PAGE2, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        self.assertEqual(raw, tools_mod.tools_text(TOOLS_PAGE1 + TOOLS_PAGE2).encode("utf-8"))
        methods = [m for m, _ in FakeMCP.seen]
        self.assertEqual(methods, ["initialize", "notifications/initialized", "tools/list", "tools/list"])
        self.assertEqual([s for _, s in FakeMCP.seen[1:]], [SESSION] * 3)

    def test_capture_json_response(self):
        self.assert_captured(self.capture("json"))

    def test_capture_sse_response(self):
        self.assert_captured(self.capture("sse"))

    def test_capture_http_error_exits_nonzero_no_file(self):
        r = self.capture("http500")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("HTTP 500", r.stderr)
        self.assertFalse(os.path.exists(self.out))

    def test_capture_rpc_error_exits_nonzero(self):
        r = self.capture("rpc_error")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("tools/list", r.stderr)
        self.assertFalse(os.path.exists(self.out))

    def test_capture_unreachable_exits_nonzero(self):
        self.env["JAEGER_UI_PORT"] = "1"
        r = self.capture("json")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("tools: FAIL", r.stderr)


class TestToolsCheck(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, obj):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as f:
            json.dump(obj, f)
        return p

    def test_expected_descriptions_drops_status(self):
        exp = tools_mod.expected_descriptions({"status": "CONFIRMED", "get_trace_errors": "x"})
        self.assertEqual(exp, {"get_trace_errors": "x"})

    def test_match(self):
        tools = [dict(t) for t in TOOLS_PAGE1 + TOOLS_PAGE2]
        tools[0]["description"] = "New critical path wording."
        expected = {"get_critical_path": "New critical path wording.",
                    "get_trace_errors": "Errors, old wording."}
        self.assertEqual(tools_mod.compare_descriptions(tools, expected), [])

    def test_mismatch_reports_name_and_both_strings(self):
        tools = TOOLS_PAGE1 + TOOLS_PAGE2
        expected = {"get_critical_path": "New critical path wording."}
        mm = tools_mod.compare_descriptions(tools, expected)
        self.assertEqual(mm, [("get_critical_path", "New critical path wording.", "Critical path, old wording.")])

    def test_missing_tool_is_a_mismatch(self):
        mm = tools_mod.compare_descriptions(TOOLS_PAGE1, {"get_trace_topology": "Topology."})
        self.assertEqual(mm, [("get_trace_topology", "Topology.", None)])

    def test_whitespace_difference_is_a_mismatch(self):
        mm = tools_mod.compare_descriptions(TOOLS_PAGE1, {"get_trace_errors": "Errors, old wording. "})
        self.assertEqual(len(mm), 1)



if __name__ == "__main__":
    unittest.main()
