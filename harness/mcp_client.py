"""Minimal MCP streamable-HTTP client (JSON-RPC 2.0 over POST). Stdlib only.

Covers exactly what the benchmark needs: initialize,
notifications/initialized, tools/list (with cursor pagination) and
tools/call. A response may come back as application/json or as a
text/event-stream; both are handled, and in the stream case the message
whose id matches the request is picked (server notifications interleaved
before it are skipped). Every raw HTTP exchange is handed to `record`, a
callable taking one dict, so the run directory keeps the bytes the server
actually sent.
"""
import json
import time
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2025-06-18"


class MCPError(Exception):
    pass


def parse_sse(text):
    """Return the list of JSON objects carried in the data: lines of an SSE body."""
    messages, data_lines = [], []

    def flush():
        if data_lines:
            payload = "\n".join(data_lines)
            try:
                messages.append(json.loads(payload))
            except ValueError:
                pass
            del data_lines[:]

    for line in text.splitlines():
        if line == "":
            flush()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" ") if line[5:6] == " " else line[5:])
        # event:, id:, retry: and comments carry nothing we need
    flush()
    return messages


class MCPClient:
    def __init__(self, url, record=None, timeout=120.0, client_name="jaeger-mcp-evals-owned-agent-loop", client_version="0"):
        self.url = url
        self.record = record or (lambda _entry: None)
        self.timeout = timeout
        self.client_info = {"name": client_name, "version": client_version}
        self.session_id = None
        self.protocol_version = None
        self.server_info = None
        self.initialized_notification_error = None
        self._next_id = 0

    def _headers(self):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            h["MCP-Protocol-Version"] = self.protocol_version
        return h

    def _post(self, payload):
        """POST one JSON-RPC message. Returns (http_status, headers, body_text)."""
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url, data=data, headers=self._headers(), method="POST")
        t0 = time.time()
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "request": payload}
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status, headers = resp.status, resp.headers
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            e.close()
            entry.update({"http_status": e.code, "content_type": e.headers.get("Content-Type", "") if e.headers else "",
                          "body": body, "elapsed_ms": int((time.time() - t0) * 1000)})
            self.record(entry)
            raise MCPError("HTTP %d from MCP server: %s" % (e.code, body[:500]))
        except (urllib.error.URLError, OSError) as e:
            entry.update({"transport_error": repr(e), "elapsed_ms": int((time.time() - t0) * 1000)})
            self.record(entry)
            raise MCPError("transport error talking to MCP server: %r" % (e,))
        entry.update({"http_status": status, "content_type": headers.get("Content-Type", ""),
                      "body": body, "elapsed_ms": int((time.time() - t0) * 1000)})
        self.record(entry)
        return status, headers, body

    def _request(self, method, params=None):
        self._next_id += 1
        rid = self._next_id
        payload = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        status, headers, body = self._post(payload)
        ctype = (headers.get("Content-Type") or "").lower()
        if ctype.startswith("text/event-stream"):
            candidates = parse_sse(body)
        else:
            try:
                parsed = json.loads(body) if body.strip() else None
            except ValueError:
                raise MCPError("MCP server returned non-JSON body for %s: %s" % (method, body[:500]))
            candidates = parsed if isinstance(parsed, list) else [parsed]
        for m in candidates:
            if isinstance(m, dict) and m.get("id") == rid and ("result" in m or "error" in m):
                return m, headers
        raise MCPError("no JSON-RPC response with id %d for %s" % (rid, method))

    def initialize(self):
        msg, headers = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": self.client_info,
        })
        if "error" in msg:
            raise MCPError("initialize failed: %s" % json.dumps(msg["error"]))
        sid = headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        result = msg["result"]
        self.protocol_version = result.get("protocolVersion") or PROTOCOL_VERSION
        self.server_info = result.get("serverInfo")
        # A failed notification alone does not end the session: _post has
        # already logged the raw exchange to mcp.jsonl, keep the error here
        # for the init event, and carry on to tools/list.
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except MCPError as e:
            self.initialized_notification_error = str(e)
        return result

    def list_tools(self):
        tools, cursor = [], None
        for _ in range(100):
            msg, _h = self._request("tools/list", {"cursor": cursor} if cursor else {})
            if "error" in msg:
                raise MCPError("tools/list failed: %s" % json.dumps(msg["error"]))
            result = msg["result"]
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
        raise MCPError("tools/list pagination did not terminate")

    def call_tool(self, name, arguments):
        """Return {"is_error": bool, "text": str, "raw": <result or error object>}.

        Never raises for a tool-level failure: a JSON-RPC error, an isError
        result, or a transport failure all come back as is_error=True with
        the message as text, so the model sees it and nothing is dropped.
        """
        try:
            msg, _h = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        except MCPError as e:
            return {"is_error": True, "text": str(e), "raw": {"client_error": str(e)}}
        if "error" in msg:
            err = msg["error"]
            return {"is_error": True, "text": "MCP error %s: %s" % (err.get("code"), err.get("message")), "raw": msg}
        result = msg.get("result") or {}
        parts = []
        for item in result.get("content", []) or []:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            else:
                parts.append(json.dumps(item))
        text = "".join(parts)
        if not parts and "structuredContent" in result:
            text = json.dumps(result["structuredContent"])
        return {"is_error": bool(result.get("isError")), "text": text, "raw": result}
