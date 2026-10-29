#!/usr/bin/env python3
"""First-move probe, 2026-10-28 (Roshan GO: "go ahead").

Question: does the search_traces.search_depth min/max pair (the only difference between the 2.20.0-based
and main+svcreq MCP handshakes) change whether the agent plans read_skill in its first message?

Two arms, identical except tools/list:
  v2200  - tools/list captured live from jaeger:v2200-mainskill-2044f311-rebuild
  svcreq - tools/list captured live from jaeger:main-238c5fa2-svcreq-36ad205e (adds the two bounds)
A local stub MCP server answers initialize (captured, identical on both builds) and tools/list verbatim, and
answers every tools/call with an error, because only the first assistant message is read. The CLI call is the
harness's own claude_argv (sonnet-5-5 pinned, effort high, same prompt, system prompt, verdict schema) with
--max-turns 1, billed to ANTHROPIC_API_KEY as in the real batches. Arms alternate in shuffled pairs.

Read, stated before the run: read_skill in the first message of most trials on both arms (near the 10/10
of the 2.20.0 batch) means neither the build nor the bounds explain the svcreq 11/20 (time or sampling);
both near 50% means it is not the bounds; v2200 clearly above svcreq means the bounds (#9642) move it.
"""
import json, os, random, re, subprocess, sys, threading, time, http.server
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "..", "..", "harness")
sys.path.insert(0, HARNESS)
import bench, config  # noqa: E402

CAPTURE = os.path.join(HERE, "initialize-tools-list-v2200-vs-svcreq.txt")
PROMPT = os.path.join(HARNESS, "prompts", "neutral.txt")
N = int(sys.argv[1]) if len(sys.argv) > 1 else 30
OUT = os.environ.get("PROBE_OUT") or os.path.join(HERE, "runs")


def load_capture():
    txt = open(CAPTURE).read()
    parts = re.split(r"^== (\d+) (INIT|TOOLS)\n", txt, flags=re.M)
    got = {}
    for i in range(1, len(parts), 3):
        m = re.search(r"^data: (.*)$", parts[i + 2], re.M)
        got[(parts[i], parts[i + 1])] = json.loads(m.group(1))["result"]
    assert got[("26686", "INIT")] == got[("26687", "INIT")], "initialize differs between builds"
    return got[("26686", "INIT")], {"v2200": got[("26686", "TOOLS")], "svcreq": got[("26687", "TOOLS")]}


INIT, TOOLS = load_capture()
# Third arm (Roshan GO 2026-10-28, 30 trials): main 238c5fa2's tools/list, captured live from the fixture.
_m = open(os.path.join(HERE, "main238-tools.sse")).read()
TOOLS["main"] = json.loads(re.search(r"^data: (.*)$", _m, re.M).group(1))["result"]
ARMS = (os.environ.get("PROBE_ARMS") or "v2200,svcreq").split(",")


def serve(arm, log):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _sse(self, obj, sid=None):
            body = ("event: message\ndata: " + json.dumps(obj) + "\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache, no-transform")
            if sid:
                self.send_header("Mcp-Session-Id", sid)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_DELETE(self):
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            method = req.get("method", "")
            log.append(method + (" " + json.dumps(req.get("params", {}).get("name")) if method == "tools/call" else ""))
            if method == "server/discover":  # real Jaeger (go-sdk v1.6.1) answers this way, checked live 2026-10-28
                body = b'JSON RPC not handled: "server/discover" unsupported\n'
                self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if "id" not in req:  # notification
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if method == "initialize":
                res = dict(INIT)  # real Jaeger echoes the client's version (2025-11-25 checked live)
                res["protocolVersion"] = req["params"].get("protocolVersion") or res["protocolVersion"]
                log.append("client protocolVersion " + str(req["params"].get("protocolVersion")))
                return self._sse({"jsonrpc": "2.0", "id": req["id"], "result": res}, sid="PROBESESSION")
            if method == "tools/list":
                return self._sse({"jsonrpc": "2.0", "id": req["id"], "result": TOOLS[arm]})
            if method == "tools/call":
                return self._sse({"jsonrpc": "2.0", "id": req["id"], "result": {
                    "content": [{"type": "text", "text": "unavailable"}], "isError": True}})
            return self._sse({"jsonrpc": "2.0", "id": req["id"], "result": {}})

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def first_message(stream_path):
    """Tool names in the first assistant message: every tool_use block whose message id is the first one seen.
    The CLI streams one event per content block and interleaves tool results, so no early break."""
    mid, names = None, []
    for line in open(stream_path):
        d = json.loads(line)
        if d.get("type") == "assistant":
            m = d["message"]
            mid = mid or m["id"]
            if m["id"] == mid:
                names += [c["name"].split("__")[-1] for c in m.get("content", []) if c.get("type") == "tool_use"]
    return names


def main():
    cfg = config.load()
    a = SimpleNamespace(model="claude-sonnet-5-5", effort="high", max_turns=1, max_budget_usd="2", provider=None)
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE",) + bench.SECRET_KEYS}
    env.update(bench.api_secrets(a, cfg))
    env["CLAUDE_CONFIG_DIR"] = os.path.expanduser("~/.claude")
    ver = subprocess.run(["claude", "--version"], capture_output=True, text=True, env=env).stdout.strip()
    assert ver.startswith("2.1.285"), "CLI is %r, need 2.1.285" % ver
    rng = random.Random(20261028)
    order = []
    for _ in range(N):
        pair = list(ARMS)
        rng.shuffle(pair)
        order += pair
    os.makedirs(OUT, exist_ok=True)
    summary = {arm: [] for arm in ARMS}
    for i, arm in enumerate(order):
        d = os.path.join(OUT, "%03d-%s" % (i, arm))
        os.makedirs(d, exist_ok=True)
        log = []
        srv = serve(arm, log)
        mcp = os.path.join(d, "mcp.json")
        json.dump({"mcpServers": {"jaeger": {"type": "http", "url": "http://127.0.0.1:%d/jaeger/ui/api/ai/mcp/" % srv.server_port}}}, open(mcp, "w"))
        argv = bench.claude_argv(a, PROMPT, mcp)
        t0 = time.time()
        with open(os.path.join(d, "stream.jsonl"), "w") as out, open(os.path.join(d, "stderr.txt"), "w") as err:
            rc = subprocess.run(argv, stdout=out, stderr=err, stdin=subprocess.DEVNULL, env=env, cwd=d, timeout=300).returncode
        srv.shutdown()
        srv.server_close()
        names = first_message(os.path.join(d, "stream.jsonl"))
        init = cost = None
        for line in open(os.path.join(d, "stream.jsonl")):
            e = json.loads(line)
            if e.get("type") == "system" and e.get("subtype") == "init":
                init = {"apiKeySource": e.get("apiKeySource"), "version": e.get("claude_code_version"), "model": e.get("model")}
            if e.get("type") == "result":
                cost = e.get("total_cost_usd")
        rec = {"i": i, "arm": arm, "rc": rc, "first_message": names, "read_skill_first": "read_skill" in names,
               "init": init, "cost": cost, "server_log": log, "secs": round(time.time() - t0, 1),
               "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        json.dump(rec, open(os.path.join(d, "meta.json"), "w"), indent=1)
        summary[arm].append(rec)
        print("%03d %-6s rc=%s first=%s cost=%s" % (i, arm, rc, names, cost), flush=True)
    tot = 0.0
    for arm, recs in summary.items():
        k = sum(r["read_skill_first"] for r in recs)
        c = sum(r["cost"] or 0 for r in recs)
        tot += c
        print("%s: read_skill in first message %d/%d, cost %.3f" % (arm, k, len(recs), c))
    print("total cost %.3f" % tot)
    json.dump({arm: [{k: r[k] for k in ("i", "first_message", "read_skill_first", "init", "cost")} for r in recs]
               for arm, recs in summary.items()}, open(os.path.join(OUT, "summary.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
