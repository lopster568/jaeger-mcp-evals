"""A fake MCP streamable-HTTP server on localhost for tests. Stdlib only.

initialize and tools/list answer with application/json; tools/call answers
with text/event-stream, so both response framings the client must handle are
exercised. One tool (get_trace_errors) always comes back with isError true.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SESSION_ID = "fake-session-123"

TOOLS = [
    {
        "name": "get_services",
        "description": "List service names.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_traces",
        "description": "Search traces by service.",
        "inputSchema": {
            "type": "object",
            "properties": {"service_name": {"type": "string"}},
            "required": ["service_name"],
        },
    },
    {
        "name": "get_trace_errors",
        "description": "List error spans of one trace.",
        "inputSchema": {
            "type": "object",
            "properties": {"trace_id": {"type": "string"}},
            "required": ["trace_id"],
        },
    },
]

CANNED = {
    "get_services": {"services": ["checkout", "frontend", "payment"], "total_count": 3},
    "search_traces": {"traces": [{"trace_id": "abc123", "has_errors": True,
                                   "status": "Payment request failed. Invalid token."}]},
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep test output quiet
        pass

    def _send_json(self, obj, extra_headers=None):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, obj):
        # A notification first, then the response, to prove the client picks
        # the message whose id matches its request.
        note = {"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "data": "x"}}
        body = ("event: message\ndata: " + json.dumps(note) + "\n\n"
                + "event: message\ndata: " + json.dumps(obj) + "\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        req = json.loads(self.rfile.read(n) or b"{}")
        self.server.requests.append({"headers": dict(self.headers), "body": req})
        method = req.get("method")
        rid = req.get("id")
        if method == "initialize":
            self._send_json({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "fake-jaeger", "version": "0.0.1"},
            }}, {"Mcp-Session-Id": SESSION_ID})
            return
        if method == "notifications/initialized":
            if getattr(self.server, "fail_initialized_notification", False):
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.headers.get("Mcp-Session-Id") != SESSION_ID:
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "tools/list":
            self._send_json({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
            return
        if method == "tools/call":
            name = req["params"]["name"]
            if name == "get_trace_errors":
                result = {"content": [{"type": "text", "text": "trace not found: " + str(req["params"].get("arguments"))}],
                          "isError": True}
            elif name in CANNED:
                text = json.dumps(CANNED[name])
                result = {"content": [{"type": "text", "text": text}], "structuredContent": CANNED[name]}
            else:
                self._send_sse({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "unknown tool " + name}})
                return
            self._send_sse({"jsonrpc": "2.0", "id": rid, "result": result})
            return
        self._send_json({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "method not found"}})


def start():
    """Start the server on an ephemeral port. Returns (server, url)."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.requests = []
    srv.fail_initialized_notification = False
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, "http://127.0.0.1:%d/mcp/" % srv.server_address[1]
