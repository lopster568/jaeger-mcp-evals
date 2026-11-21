#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""bench.py run end to end against a fake fixture.

ssh, docker and claude are PATH shims: ssh runs its command with bash inside a
fake home that holds an otel-demo checkout (a real flagd file, pristine copy and
git repo), docker answers ps/inspect, and claude writes a canned stream.jsonl.
One local http.server plays OFREP (reading the live fake flag file), the Jaeger
query API and the MCP endpoint. No network beyond localhost, no LLM.

Run with: python3 -m unittest discover harness/tests
"""
import contextlib
import glob
import hashlib
import http.server
import io
import json
import urllib.parse
import os
import pathlib
import re
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest import mock

HARNESS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HARNESS_DIR)
import bench  # noqa: E402
import codex_client  # noqa: E402

FLAGS = {"flags": {
    "paymentFailure": {"state": "ENABLED", "variants": {"100%": 1, "off": 0}, "defaultVariant": "off"},
    "adFailure": {"state": "ENABLED", "variants": {"on": True, "off": False}, "defaultVariant": "off"},
}}
DESC_CHANGE = {"status": "CONFIRMED", "get_trace_errors": "pre-registered wording of get_trace_errors"}
VARIANT_IMAGE = "jaeger-mcp-evals/jaeger:main-d22c65b9"
STOCK_IMAGE = "quay.io/jaegertracing/jaeger:2.20.0"
SYSTEM_NAMES = ("OpenTelemetry Demo", "otel demo", "opentelemetry-demo", "astronomy shop")
RUN = {"client": "cli", "provider": None, "model": "sonnet", "effort": "xhigh", "max_turns": 30, "max_budget_usd": 2,
       "n_per_arm": 1, "seed": None}


def arm(prompt, image=STOCK_IMAGE, tools=True, **extra):
    return dict({"prompt": prompt, "image": image, "tools": tools}, **extra)

SSH = """#!/bin/bash
cmd="${@: -1}"
printf '%s\\n' "$cmd" >> "$FAKE_HOME/ssh.log"
case "$cmd" in
  *"cp "*pristine*)
    [ -n "$FAKE_KILL_ON_RESTORE" ] && kill -"$FAKE_KILL_ON_RESTORE" "$PPID"
    [ -n "$FAKE_FAIL_RESTORE" ] && exit 1 ;;
esac
cd "$FAKE_HOME" && exec bash -c "$cmd"
"""
DOCKER = """#!/bin/bash
case "$1 $2" in
  "ps -q") for i in $(seq 1 "${FAKE_CONTAINERS:-22}"); do echo "c$i"; done ;;
  "ps -aq") for i in $(seq 1 "${FAKE_CONTAINERS:-22}"); do echo "c$i"; done ;;
  "ps --format") echo "Up 2 hours (healthy)" ;;
  "inspect jaeger") echo "$FAKE_IMAGE sha256:feedface" ;;
  "inspect -f")
    # container_snapshot's one-liner: inspect -f '<fmt>' <id...>; a stable RestartCount and
    # StartedAt for every id, so a trial's before/after snapshots match unless a test says otherwise.
    shift 3
    for id in "$@"; do echo "/$id 0 false 2026-01-01T00:00:00Z"; done
    ;;
  # The goose client: pre-flight, version, one trial replaying a recording, and the cleanup.
  "info --format") echo "Ubuntu 24.04" ;;
  "image inspect") [ -z "$FAKE_NO_GOOSE_IMAGE" ] ;;
  "rm -f") echo "$*" >> "$FAKE_HOME/docker-rm.log" ;;
  "run --rm")
    case " $* " in
      *" --version "*) [ -n "$FAKE_GOOSE_NO_VERSION" ] || echo " 1.57.0" ;;
      *) [ -n "$FAKE_GOOSE_HANG" ] && exec sleep 30
         printf '%s\n' "$@" > "$FAKE_HOME/goose-argv.txt"
         echo "key_set=${ANTHROPIC_API_KEY:+yes}" >> "$FAKE_HOME/goose-argv.txt"
         cat "$FAKE_GOOSE/stdout.jsonl"; cat "$FAKE_GOOSE/goose-stderr.txt" >&2 ;;
    esac ;;
  *) exit 1 ;;
esac
"""
# A calm, fixed vmstat: bi comfortably under bench.py's 50000 abort threshold, so pre-flight
# never slows down or aborts a test on the real vmstat (which "vmstat 5 3" would take ~10s to run).
VMSTAT = """#!/bin/bash
cat <<'EOF'
procs -----------memory---------- ---swap-- -----io---- -system-- ------cpu-----
 r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st
 0  0      0 4000000  30000 1000000    0    0   200   100 1000 1000  1  1 98  0  0
 0  0      0 4000000  30000 1000000    0    0   150    80 1000 1000  0  0 100  0  0
 0  0      0 4000000  30000 1000000    0    0   180    90 1000 1000  0  0 100  0  0
EOF
"""
CLAUDE = """#!/usr/bin/env python3
import json, os, sys, urllib.error, urllib.request
if sys.argv[1:] == ["--version"]:
    print("2.1.282 (Claude Code)"); sys.exit(0)
if sys.argv[1:] == ["--help"]:
    print(os.environ.get("FAKE_CLAUDE_HELP", "@FLAGS@")); sys.exit(0)
if "claude-fable-5-1" in sys.argv:  # the mechanism grader
    with open(os.path.join(os.environ["FAKE_HOME"], "grader-calls.jsonl"), "a") as f:
        f.write(json.dumps({"argv": sys.argv, "cwd": os.getcwd(), "claudecode": "CLAUDECODE" in os.environ,
                            "api_key": "ANTHROPIC_API_KEY" in os.environ, "client_env": [os.environ.get("ENABLE_TOOL_SEARCH"), os.environ.get("DISABLE_AUTOUPDATER")], "plugin_dirs": os.environ.get("CLAUDE_CODE_PLUGIN_DIRS")}) + "\\n")
    res = {"type": "result", "subtype": "success", "result": "",
           "structured_output": {"label": os.environ.get("FAKE_GRADE", "correct"), "reason": "fake"}}
    src = os.environ.get("FAKE_KEY_SOURCE", "ANTHROPIC_API_KEY" if "ANTHROPIC_API_KEY" in os.environ else "none")
    print(json.dumps([{"type": "system", "subtype": "init", "apiKeySource": src}, res] if "--verbose" in sys.argv else res))
    sys.exit(0)
if os.environ.get("ANTHROPIC_BASE_URL"):  # bench.py's sandbox probe: POST one canned request, as the CLI would
    mcp = json.load(open(sys.argv[sys.argv.index("--mcp-config") + 1]))
    names = ["StructuredOutput"] + (["mcp__jaeger__" + n for n in ("get_critical_path", "get_trace_errors",
                                     "get_trace_topology", "get_services")] if mcp["mcpServers"] else [])
    if mcp["mcpServers"] and os.environ.get("FAKE_SQL_ONLY"):
        names.insert(1, "mcp__jaeger__find_traces")
    tools, system, mode = [{"name": n, "input_schema": {}} for n in names], [{"type": "text", "text": "sys"}], os.environ.get("FAKE_PROBE")
    if mode == "extra_tool":
        tools.append({"name": "Bash", "input_schema": {}})
    if mode == "server_tool":
        tools.append({"type": "web_search_20250305", "name": "web_search"})
    if mode == "memory":
        system.append({"type": "text", "text": "Contents of /x/CLAUDE.md (project instructions)"})
    with open(os.path.join(os.environ["FAKE_HOME"], "probe-calls.jsonl"), "a") as f:
        f.write(json.dumps({"argv": sys.argv, "mcp": mcp, "cwd": os.getcwd(), "client_env": [os.environ.get("ENABLE_TOOL_SEARCH"), os.environ.get("DISABLE_AUTOUPDATER")], "plugin_dirs": os.environ.get("CLAUDE_CODE_PLUGIN_DIRS")}) + "\\n")
    body = {"model": "m", "system": system, "tools": tools,
            "messages": [{"role": "user", "content": "userEmail probe-secret@example.com"}]}
    if mode != "silent":
        try:
            urllib.request.urlopen(urllib.request.Request(os.environ["ANTHROPIC_BASE_URL"] + "/v1/messages?beta=true",
                                                          json.dumps(body).encode(),
                                                          dict({"Content-Type": "application/json"}, **({"x-api-key": os.environ["ANTHROPIC_API_KEY"]} if "ANTHROPIC_API_KEY" in os.environ else {}))))
        except urllib.error.HTTPError:
            sys.exit(1)
    sys.exit(0)
with open(os.path.join(os.environ["FAKE_HOME"], "claude-calls.jsonl"), "a") as f:
    f.write(json.dumps({"argv": sys.argv, "claudecode": "CLAUDECODE" in os.environ,
                        "api_key": any(k in os.environ for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL")), "cwd_listing": os.listdir("."), "client_env": [os.environ.get("ENABLE_TOOL_SEARCH"), os.environ.get("DISABLE_AUTOUPDATER")], "plugin_dirs": os.environ.get("CLAUDE_CODE_PLUGIN_DIRS")}) + "\\n")
verdict = {"root_cause_service": "payment", "root_cause_operation": "charge", "mechanism": "invalid token", "cascading": [{"service": "checkout"}], "confidence": "high",
           "evidence_span_ids": [], "abstain": False}
if os.environ.get("FAKE_JUNK"):
    print(json.dumps("not an event"))
for e in [
    {"type": "system", "subtype": "init", "model": "claude-sonnet-5", "claude_code_version": "2.1.282",
     "apiKeySource": os.environ.get("FAKE_KEY_SOURCE", "ANTHROPIC_API_KEY" if "ANTHROPIC_API_KEY" in os.environ else "none"),
     "tools": ["StructuredOutput"] + ["mcp__jaeger__" + n for n in ("get_critical_path", "get_trace_errors", "get_trace_topology", "get_services")],
     "mcp_servers": [{"name": "jaeger", "status": "connected"}], "skills": ["s1"], "agents": ["a1"]},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "mcp__jaeger__get_trace_errors", "input": {}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "Payment request failed. Invalid token."}]}},
    {"type": "result", "subtype": os.environ.get("FAKE_STOP", "success"), "num_turns": 2, "total_cost_usd": 0.01, "duration_ms": 1000,
     "usage": {"input_tokens": 10, "output_tokens": 20}, "result": "done", "structured_output": verdict},
]:
    print(json.dumps(e))
if os.environ.get("FAKE_CLAUDE_HANG"):
    import time; time.sleep(60)
sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
"""

# Event names from codex 0.153.2 (help text and binary strings); field shapes are a guess
# until the first live run.
CODEX = """#!/usr/bin/env python3
import json, os, sys
if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.153.2"); sys.exit(0)
with open(os.path.join(os.environ["FAKE_HOME"], "codex-calls.jsonl"), "a") as f:
    f.write(json.dumps({"argv": sys.argv, "stdin": sys.stdin.read(),
                        "api_key": any(k in os.environ for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL"))}) + "\\n")
verdict = {"root_cause_service": "payment", "root_cause_operation": "charge", "mechanism": "invalid token", "cascading": [], "confidence": "high", "evidence_span_ids": [], "abstain": False}
call = {"id": "item_0", "type": "mcp_tool_call", "server": "jaeger", "tool": "get_trace_errors", "arguments": {}}
for e in [
    {"type": "thread.started", "thread_id": "th-1"},
    {"type": "turn.started"},
    {"type": "item.started", "item": dict(call, status="in_progress")},
    {"type": "item.completed", "item": dict(call, status="completed", error=None,
        result={"content": [{"type": "text", "text": "Payment request failed. Invalid token."}]})},
    {"type": "item.completed", "item": {"id": "item_e", "type": "error", "message": "Model metadata for gpt-t not found"}},
    {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": json.dumps(verdict)}},
    {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 20}},
]:
    print(json.dumps(e))
"""


def read(path):
    return pathlib.Path(path).read_text(encoding="utf-8")


def load(path):
    return json.loads(read(path))


def trace(fault):
    tags = [{"key": "rpc.method", "value": "Charge"}]
    if fault:
        tags.append({"key": "exception.message", "value": "Payment request failed. Invalid token."})
    return {"traceID": "t", "processes": {"p1": {"serviceName": "payment"}},
            "spans": [{"operationName": "charge", "processID": "p1", "tags": tags, "logs": []}]}


def v3_body(traces):
    """Jaeger's /api/v3/traces body for v1-shaped traces: spans flattened across resourceSpans, OTLP attributes."""
    rs = [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": t["processes"][sp["processID"]]["serviceName"]}}]},
           "scopeSpans": [{"spans": [{"traceId": "%s%d" % (t["traceID"], i), "name": sp["operationName"], "status": {},
                                      "attributes": [{"key": a["key"], "value": {"stringValue": a["value"]}} for a in sp["tags"]]}]}]}
          for i, t in enumerate(traces) for sp in t["spans"]]
    return {"result": {"resourceSpans": rs}}


class Fake(http.server.BaseHTTPRequestHandler):
    home = None
    served_descriptions = {}
    served_instructions = None
    oracle_signal = True
    oracle_malformed = False
    store_services = None  # get_services answers this list when set; "error" makes the call fail
    sql_only = False  # tools/list serves find_traces with the jaeger#10083 sql-only schema
    mcp_calls = []
    otlp_posts, otlp_status = [], 200
    projects = [{"name": "t", "id": "UHJvamVjdDoy"}]  # GET /v1/projects, Phoenix's {"data": [{"name", "id"}]}; None answers 404

    def log_message(self, *a):
        pass

    def reply(self, doc, headers=()):
        body = json.dumps(doc).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def flags(self):
        return load(os.path.join(Fake.home, "otel-demo-3.0.0", "src", "flagd", "demo.flagd.json"))["flags"]

    def do_GET(self):
        if self.path == "/v1/projects" and Fake.projects is not None:
            return self.reply({"data": Fake.projects})
        if self.path.startswith("/jaeger/ui/api/v3/services"):
            return self.reply({"services": ["payment", "checkout"]})
        if self.path.startswith("/jaeger/ui/api/v3/traces"):
            fault = self.flags()["paymentFailure"]["defaultVariant"] != "off"
            return self.reply(v3_body([trace(fault) for _ in range(5)]))
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path.startswith("/ofrep/v1/evaluate/flags/"):
            flag = self.path.rsplit("/", 1)[1]
            return self.reply({"key": flag, "variant": self.flags()[flag]["defaultVariant"]})
        if self.path == "/v1/traces":  # Phoenix's OTLP HTTP receiver
            Fake.otlp_posts.append((self.headers.get("Content-Type"), body))
            self.send_response(Fake.otlp_status)
            self.end_headers()
            return
        msg = json.loads(body)
        Fake.mcp_calls.append((msg.get("method"), (msg.get("params") or {}).get("name"), (msg.get("params") or {}).get("arguments")))
        if msg.get("method") == "tools/call" and msg["params"]["name"] == "get_services" and Fake.store_services:
            if Fake.store_services == "error":
                return self.reply({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32000, "message": "store down"}})
            return self.reply({"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [
                {"type": "text", "text": json.dumps({"services": Fake.store_services})}]}})
        if msg.get("method") == "tools/call":
            if Fake.oracle_malformed:
                return self.reply({"jsonrpc": "2.0", "id": msg["id"], "result": "not-a-dict"})
            fault = self.flags()["paymentFailure"]["defaultVariant"] != "off" and Fake.oracle_signal
            doc = {"traces": [{"trace_id": "aa11"}, {"trace_id": "bb22"}]} if msg["params"]["name"] in ("search_traces", "find_traces") else trace(fault)
            return self.reply({"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": json.dumps(doc)}]}})
        if msg.get("method") == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return
        if msg.get("method") == "initialize":
            return self.reply({"jsonrpc": "2.0", "id": msg["id"], "result": dict(
                {"protocolVersion": "2025-06-18"}, **({"instructions": Fake.served_instructions} if Fake.served_instructions else {}))},
                              [("Mcp-Session-Id", "s1")])
        served = [{"name": n, "description": Fake.served_descriptions.get(n, "stock wording"), "inputSchema": {}}
                  for n in ("get_critical_path", "get_trace_errors", "get_trace_topology", "get_services")]
        if Fake.sql_only:
            served.append({"name": "find_traces", "description": "stock wording", "inputSchema": {"properties": {"sql": {}}}})
        self.reply({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": served}})


class BenchCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        cls.port = str(cls.server.server_address[1])
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.phoenix = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)  # the OTLP receiver, never the demo's port
        cls.phoenix_port = str(cls.phoenix.server_address[1])
        threading.Thread(target=cls.phoenix.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for srv in (cls.server, cls.phoenix):
            srv.shutdown()
            srv.server_close()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        t = self._tmp.name
        self.home, self.runs, binp, self.records = (os.path.join(t, d) for d in ("home", "runs", "bin", "records"))
        patcher = mock.patch.object(bench, "RECORDS", self.records)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The batch tests run the recorded prompts (noskill, skill), which name the system under
        # test; the leak gate itself is tested in test_leak_readiness.py.
        words = [w for w in bench.score.leak_words(HARNESS_DIR) if w not in SYSTEM_NAMES]
        patcher = mock.patch.object(bench.score, "leak_words", lambda harness_dir=None: words)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.demo = os.path.join(self.home, "otel-demo-3.0.0")
        os.makedirs(os.path.join(self.demo, "src", "flagd"))
        os.makedirs(os.path.join(self.demo, "overlay"))
        os.makedirs(binp)
        for rel in ("src/flagd/demo.flagd.json", "overlay/demo.flagd.json.pristine"):
            pathlib.Path(self.demo, rel).write_text(json.dumps(FLAGS, indent=2) + "\n")
        pathlib.Path(self.demo, ".env.override").write_text("X=1\n")
        git = ["git", "-C", self.demo, "-c", "user.email=t@example.com", "-c", "user.name=t"]
        subprocess.run(git + ["init", "-q"], check=True)
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "-qm", "demo"], check=True)
        for name, text in (("ssh", SSH), ("docker", DOCKER), ("vmstat", VMSTAT), ("claude", CLAUDE), ("codex", CODEX)):
            p = os.path.join(binp, name)
            pathlib.Path(p).write_text(text.replace("@FLAGS@", " ".join(bench.CLAUDE_FLAGS)))
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        Fake.home, Fake.served_descriptions, Fake.mcp_calls = self.home, {}, []
        Fake.served_instructions = None
        Fake.oracle_signal, Fake.oracle_malformed, Fake.sql_only, Fake.store_services = True, False, False, None
        Fake.otlp_posts, Fake.otlp_status = [], 200
        Fake.projects = [{"name": "t", "id": "UHJvamVjdDoy"}]
        self.env = {"PATH": binp + os.pathsep + os.environ["PATH"], "FAKE_HOME": self.home, "FAKE_IMAGE": STOCK_IMAGE,
                    "FIXTURE_HOST": "127.0.0.1", "FIXTURE_SSH_USER": "tester", "FIXTURE_DEMO_DIR": "otel-demo-3.0.0",
                    "JAEGER_UI_PORT": self.port, "OFREP_PORT": self.port, "JAEGER_BASE_PATH": "/jaeger/ui",
                    "RUNS_DIR": self.runs, "MIN_CONTAINERS": "19", "KNOWN_COMMITS": "2.20.0=abc123",
                    "CLAUDECODE": "1", "CODEX_BIN": "codex", "PHOENIX_PORT": "1"}  # port 1: no Phoenix unless a test starts one

    def tearDown(self):
        self._tmp.cleanup()

    def bench(self, *argv, **env):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, dict(self.env, **env)), mock.patch.object(bench, "sleep"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = bench.main(list(argv))
        self.out, self.err = out.getvalue(), err.getvalue()
        return rc

    def knob(self, arms=None, name="t", baseline_arm="noskill", **run):
        """Write an experiment file; the default is the old two-prompt batch on the stock image."""
        doc = {"name": name, "version": 2, "scenario": "paymentFailure", "hypothesis": "h", "run": dict(RUN, **run),
               "arms": arms or {"noskill": arm("noskill"), "skill": arm("skill")}, "baseline_arm": baseline_arm,
               "thresholds": {}, "status": "CONFIRMED: test"}
        self.knob_path = os.path.join(self._tmp.name, name + ".json")
        pathlib.Path(self.knob_path).write_text(json.dumps(doc))
        return self.knob_path

    def run_batch(self, n=1, *flags, arms=None, run=None, **env):
        return self.bench("run", self.knob(arms, **dict(run or {}, n_per_arm=n)), *flags, **env)

    def batch_dir(self):
        dirs = glob.glob(os.path.join(self.runs, "paymentFailure", "batch-*"))
        self.assertEqual(len(dirs), 1, dirs)
        return dirs[0]

    def calls(self):
        p = os.path.join(self.home, "claude-calls.jsonl")
        return bench.read_jsonl(p)

    def flag_file(self):
        return read(os.path.join(self.demo, "src", "flagd", "demo.flagd.json"))

    def pristine(self):
        return read(os.path.join(self.demo, "overlay", "demo.flagd.json.pristine"))


META_V4_KEYS = [
    "schema_version", "run_id", "client", "provider", "batch_id", "order_index", "trial_index", "seed", "scenario", "scenario_sha256",
    "arm", "prompt_name", "prompt_sha256", "system_prompt_sha256", "verdict_schema_sha256", "model_requested",
    "effort", "max_turns", "max_budget_usd", "client_argv", "history_policy", "client_version_pre", "mcp_endpoint",
    "mcp_config_sha256", "tools_list_sha256", "tools_count", "tool_descriptions_file", "tool_descriptions_sha256",
    "tool_descriptions_check", "jaeger_image", "jaeger_image_id", "jaeger_commit", "otel_demo_ref",
    "fixture_overlay_sha256", "fault", "flag_names", "preflight", "experiment", "harness_git_sha", "harness_dirty",
    "score_py_sha256", "started_utc", "ended_utc", "wall_time_s", "exit_code", "observed", "agent_loop",
    "system_under_test", "fixture_changes", "fixture_ok"]


def text(name):
    return read(os.path.join(HARNESS_DIR, name)).rstrip("\n")


class TestBatch(BenchCase):
    def test_full_batch(self):
        Fake.served_descriptions = {"get_services": "Summaries with an error flag per trace."}  # stock Jaeger wording passes
        self.assertEqual(self.run_batch(1, ANTHROPIC_API_KEY="sk-test",
                                    OPENAI_API_KEY="sk-o", OPENAI_BASE_URL="http://x"), 0, self.err)
        b = self.batch_dir()
        for f in ("manifest.json", "cells.jsonl", "scores.jsonl", "restore.json", "preflight.json", "batch.log", "tools.json"):
            self.assertTrue(os.path.isfile(os.path.join(b, f)), f)
        m = load(os.path.join(b, "manifest.json"))
        for k in ("scenario", "batch_id", "n_per_arm", "model_requested", "effort", "arms", "seed", "order",
                  "file_hashes_sha256", "system_under_test", "tools_list_sha256", "tools_list_path", "schema_version"):
            self.assertIn(k, m)
        self.assertEqual(m["schema_version"], 5)
        self.assertEqual(m["arms"], ["noskill", "skill"])
        self.assertEqual(m["batch_id"], os.path.basename(b))
        self.assertEqual(m["tools_list_sha256"], bench.sha256_file(os.path.join(b, "tools.json")))
        self.assertEqual(m["jaeger_commit"], "abc123")
        # The knob file rides along verbatim, with its hash. It lives outside the repo in this
        # test (the tmp dir), so "file" is just its basename: a relpath would otherwise carry
        # the tmp dir's absolute path into the record (bench.repo_relpath).
        self.assertEqual(m["experiment"], {"name": "t", "file": os.path.basename(self.knob_path),
                                           "sha256": bench.sha256_file(self.knob_path), "content": load(self.knob_path)})
        cells = bench.read_jsonl(os.path.join(b, "cells.jsonl"))
        self.assertEqual(sorted(c["arm"] for c in cells), ["noskill", "skill"])
        scores = bench.read_jsonl(os.path.join(b, "scores.jsonl"))
        self.assertEqual([s["verdict"] for s in scores], ["PASS", "PASS"])
        self.assertNotIn("read_skill_called", scores[0])
        for c in cells:
            trial = os.path.join(b, "%d-%s" % (c["order_index"], c["arm"]))
            self.assertEqual(os.path.join(b, c["out_dir"]), trial)
            meta = load(os.path.join(trial, "meta.json"))
            self.assertEqual(list(meta), META_V4_KEYS)
            self.assertEqual((meta["schema_version"], meta["client"], meta["provider"], meta["agent_loop"]), (5, "cli", None, None))
            self.assertEqual(meta["experiment"], {k: m["experiment"][k] for k in ("name", "file", "sha256")})
            self.assertEqual(meta["run_id"], "%s/%d-%s" % (m["batch_id"], c["order_index"], c["arm"]))
            self.assertEqual(meta["arm"], c["arm"])
            self.assertEqual(meta["exit_code"], 0)
            self.assertEqual(meta["observed"], {"client_version": "2.1.282", "model": "claude-sonnet-5",
                                                "tools": ["StructuredOutput"] + ["mcp__jaeger__" + n for n in (
                                                    "get_critical_path", "get_trace_errors", "get_trace_topology", "get_services")],
                                                "skills": ["s1"], "agents": ["a1"], "compaction_events": 0})
            self.assertEqual(meta["client_version_pre"], "2.1.282 (Claude Code)")
            self.assertEqual(meta["jaeger_image"], STOCK_IMAGE)
            self.assertEqual(meta["jaeger_image_id"], "sha256:feedface")
            self.assertEqual(len(meta["otel_demo_ref"]), 40)
            self.assertEqual(meta["tools_list_sha256"], m["tools_list_sha256"])
            self.assertEqual(meta["tools_count"], 4)
            self.assertEqual(meta["tool_descriptions_check"], "recorded_only")
            self.assertTrue(meta["fault"]["ofrep_confirmed"])
            self.assertEqual(meta["fault"]["signal_traces_seen"], 5)
            self.assertEqual(meta["preflight"]["fixture_leak_under_fault"], "PASS")
            self.assertEqual(meta["max_budget_usd"], 2.0)
            self.assertEqual(meta["mcp_config_sha256"], bench.sha256_file(os.path.join(trial, "mcp.json")))
            self.assertEqual(load(os.path.join(trial, "mcp.json")),
                             {"mcpServers": {"jaeger": {"type": "http", "url": "http://127.0.0.1:%s/jaeger/ui/api/ai/mcp/" % self.port}}})
            self.assertEqual(read(os.path.join(trial, "exit.txt")), "exit=0\n")
            self.assertEqual(meta["client_argv"][2], "sha256:" + meta["prompt_sha256"])
            self.assertEqual(meta["client_argv"][meta["client_argv"].index("--mcp-config") + 1], "mcp.json")
        # The exact cli argv, pinned.
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        first = cells[0]
        mcp = os.path.join(b, "%d-%s" % (first["order_index"], first["arm"]), "mcp.json")
        self.assertEqual(calls[0]["argv"][1:], [
            "-p", text("prompts/%s.txt" % first["arm"]), "--output-format", "stream-json", "--verbose",
            "--mcp-config", mcp, "--strict-mcp-config", "--setting-sources", "", "--restricted", "--tools", "",
            "--allowedTools", "mcp__jaeger__*", "--system-prompt", text("system-prompt.txt"),
            "--json-schema", text("verdict-schema.json"), "--model", "sonnet", "--effort", "xhigh",
            "--max-turns", "30", "--max-budget-usd", "2"])
        self.assertFalse(calls[0]["claudecode"])
        self.assertFalse(calls[0]["api_key"])
        self.assertEqual([c["cwd_listing"] for c in calls], [[], []])
        # The grader: sandboxed like the agent; both trials gave the same answer, so one call and one cache hit.
        graders = bench.read_jsonl(os.path.join(self.home, "grader-calls.jsonl"))
        self.assertEqual(len(graders), 1)
        g = graders[0]["argv"]
        for flag in ("--strict-mcp-config", "--restricted", "--no-session-persistence"):
            self.assertIn(flag, g)
        self.assertEqual([g[g.index(k) + 1] for k in ("--tools", "--setting-sources", "--model", "--system-prompt")],
                         ["", "", "claude-fable-5-1", text("grader-prompt.txt")])
        self.assertFalse(graders[0]["claudecode"] or graders[0]["api_key"])
        self.assertNotEqual(graders[0]["cwd"], bench.ROOT)
        self.assertEqual(m["grader"], {"model": "claude-fable-5-1", "cli_version": "2.1.282 (Claude Code)",
                                       "prompt_sha256": bench.sha256_file(os.path.join(HARNESS_DIR, "grader-prompt.txt"))})
        self.assertEqual(sorted((s["mechanism_grade"], s["grader_cached"]) for s in scores), [("correct", False), ("correct", True)])
        self.assertEqual(m["preflight"]["fixture_leak_under_fault"], "PASS")
        r = load(os.path.join(b, "restore.json"))
        self.assertTrue(r["cp_ok"] and r["default_confirmed"])
        self.assertEqual(self.flag_file(), self.pristine())
        blog = read(os.path.join(b, "batch.log"))
        self.assertIn("band paymentFailure/noskill: n=1 passes=1", blog)
        self.assertIn("pre-flight PASS", blog)
        self.assertEqual(m["scenario_version"], 2)
        self.assertEqual(m["preflight"]["oracle"], "PASS")
        self.assertIn("noskill: n=1 PASS=1 PARTIAL=0 FAIL=0 ABSTAIN=0 ERROR=0 INVALID=0 LEAK=0 stops=- ", blog)
        # One stream: everything a human reads is on stderr, and batch.log carries every screen line too.
        self.assertEqual(self.out, "")
        for line in ("ok    paymentFailure ready", "[1/2] ", "tools used: get_trace_errors 1", "Records "):
            self.assertIn(line, self.err)
            self.assertIn(line, blog)
        # The results table: a header, then one row per arm.
        lines = self.err.splitlines()
        head = next(i for i, l in enumerate(lines) if l.split()[:2] == ["arm", "pass"])
        self.assertEqual([l.split()[:2] for l in lines[head + 1:] if l.startswith("  ") and not l.startswith("    ")][:2],
                         [["noskill", "1/1"], ["skill", "1/1"]])
        self.assertEqual(sum(1 for l in lines if l.lstrip().startswith(("noskill ", "skill ")) and "/1 " in l), 2)

    def test_cli_refused_before_anything_is_touched_when_claude_lacks_a_flag(self):
        self.assertEqual(self.run_batch(1, FAKE_CLAUDE_HELP="--restricted --effort"), 1)
        out = self.err + self.out
        self.assertIn("does not accept --setting-sources --strict-mcp-config --json-schema", out)
        self.assertIn("claude update", out)
        self.assertEqual(self.calls(), [])
        self.assertEqual(glob.glob(os.path.join(self.runs, "paymentFailure", "batch-*")), [])

    def test_cli_says_the_api_key_is_ignored(self):
        self.assertEqual(self.run_batch(1, "--dry-run", ANTHROPIC_API_KEY="sk-x"), 0, self.err)
        self.assertIn("runs on the logged-in plan and ignores it", self.err + self.out)

    def test_bill_api_key_runs_cli_and_grader_on_the_key(self):
        self.addCleanup(setattr, bench.grade, "API_KEY", None)
        self.assertEqual(self.run_batch(1, run={"bill_api_key": True}, ANTHROPIC_API_KEY="sk-test-secret"), 0, self.err)
        self.assertIn("agent and grader bill ANTHROPIC_API_KEY", self.err + self.out)
        self.assertTrue(all(c["api_key"] for c in self.calls()))
        graders = bench.read_jsonl(os.path.join(self.home, "grader-calls.jsonl"))
        self.assertTrue(graders and all(g["api_key"] and "--verbose" in g["argv"] for g in graders))
        b = self.batch_dir()
        self.assertEqual(load(os.path.join(b, "manifest.json"))["billing"], "ANTHROPIC_API_KEY")
        for path in glob.glob(os.path.join(b, "**"), recursive=True) + glob.glob(os.path.join(self.records, "**"), recursive=True):
            if os.path.isfile(path):
                self.assertNotIn("sk-test-secret", read(path), path)
        self.assertNotIn("sk-test-secret", json.dumps([c["argv"] for c in self.calls()]))

    def test_bill_api_key_aborts_when_the_stream_reports_another_source(self):
        self.addCleanup(setattr, bench.grade, "API_KEY", None)
        self.assertEqual(self.run_batch(2, run={"bill_api_key": True}, ANTHROPIC_API_KEY="sk-x", FAKE_KEY_SOURCE="none"), 4)
        self.assertIn("billing: run.bill_api_key is true but the stream init reports apiKeySource 'none'", self.err + self.out)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.flag_file(), self.pristine())

    def test_bill_api_key_needs_the_key(self):
        self.assertEqual(self.run_batch(1, run={"bill_api_key": True}, ANTHROPIC_API_KEY=""), 1)
        self.assertIn("run.bill_api_key is true but ANTHROPIC_API_KEY is empty", self.err)
        self.assertEqual(self.calls(), [])

    def test_default_cli_still_strips_the_key(self):
        self.assertEqual(self.run_batch(1, ANTHROPIC_API_KEY="sk-x"), 0, self.err)
        self.assertFalse(any(c["api_key"] for c in self.calls()))
        self.assertFalse(any(g["api_key"] for g in bench.read_jsonl(os.path.join(self.home, "grader-calls.jsonl"))))
        self.assertNotIn("billing", load(os.path.join(self.batch_dir(), "manifest.json")))

    def test_codex_client_scores_through_the_stream(self):
        codex = {"client": "codex", "model": "gpt-t"}
        self.assertEqual(self.run_batch(1, "--dry-run", run=codex), 0, self.err)
        self.assertIn("codex gpt-t xhigh", self.err)
        self.assertIn("codex_client.py", self.err)
        self.assertEqual(self.run_batch(1, run=codex, OPENAI_API_KEY="sk-o", ANTHROPIC_API_KEY="sk-a"), 0, self.err)
        b = self.batch_dir()
        self.assertEqual([s["verdict"] for s in bench.read_jsonl(os.path.join(b, "scores.jsonl"))], ["PASS", "PASS"])
        m = load(os.path.join(b, "manifest.json"))
        self.assertIn("codex_client.py", m["file_hashes_sha256"])
        calls = bench.read_jsonl(os.path.join(self.home, "codex-calls.jsonl"))
        self.assertEqual(len(calls), 2)
        url = "http://127.0.0.1:%s/jaeger/ui/api/ai/mcp/" % self.port
        for c in bench.read_jsonl(os.path.join(b, "cells.jsonl")):
            trial = os.path.join(b, "%d-%s" % (c["order_index"], c["arm"]))
            meta = load(os.path.join(trial, "meta.json"))
            self.assertEqual((meta["client"], meta["model_requested"], meta["client_version_pre"]), ("codex", "gpt-t", "codex-cli 0.153.2"))
            self.assertEqual(meta["history_policy"], {"harness_truncation": "none", "cli_autocompact": "unknown"})
            self.assertEqual(meta["client_argv"][:2], [os.path.basename(sys.executable), "harness/codex_client.py"])
            self.assertFalse([x for x in meta["client_argv"] if os.path.isabs(x)])
            init = bench.read_jsonl(os.path.join(trial, "stream.jsonl"))[0]
            endpoint = ":%s/jaeger/ui/api/ai/mcp/" % self.port
            self.assertEqual(init["mcp_servers"], [{"name": "jaeger", "url": endpoint}])
            self.assertEqual(init["codex_bin"], "codex")
            self.assertIn('mcp_servers.jaeger.url="%s"' % endpoint, init["codex_argv"])
            self.assertEqual(init["codex_argv"][init["codex_argv"].index("--output-schema") + 1], "harness/verdict-schema.json")
            self.assertNotIn("127.0.0.1", json.dumps(init))
            self.assertEqual(meta["observed"]["client_version"], "codex-cli 0.153.2")
            s, _ = bench.score.score(trial)
            self.assertEqual((s["tool_calls"], s["steps_to_evidence"], s["stop"], s["input_tokens_total"]),
                             (1, 1, "success", 100))
            stream = bench.read_jsonl(os.path.join(trial, "stream.jsonl"))
            tool_use_names = [b["name"] for e in stream if e.get("type") == "assistant"
                               for b in e["message"].get("content", []) if b.get("type") == "tool_use"]
            self.assertNotIn("error", tool_use_names)
            self.assertIn({"type": "system", "subtype": "codex_error", "message": "Model metadata for gpt-t not found"}, stream)
        argv = calls[0]["argv"]
        self.assertEqual(argv[argv.index("--output-schema") + 1], bench.VERDICT_SCHEMA)
        self.assertIn('mcp_servers.jaeger.url="%s"' % url, argv)
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-t")
        self.assertIn(text("system-prompt.txt"), calls[0]["stdin"])
        self.assertFalse(calls[0]["api_key"])
        exec_i = argv.index("exec")
        self.assertNotIn("-c", argv[exec_i + 1:])
        c_indices = [i for i, v in enumerate(argv) if v == "-c"]
        self.assertTrue(c_indices and all(i < exec_i for i in c_indices))

    def test_goose_client_runs_a_trial_and_records_no_key(self):
        goose = {"client": "goose", "provider": "anthropic", "model": "claude-sonnet-5-5"}
        self.assertEqual(self.run_batch(1, "--dry-run", run=goose, ANTHROPIC_API_KEY="sk-goose-secret-123"), 0, self.err)
        self.assertIn("goose_client.py", self.err)
        self.assertEqual(self.run_batch(1, run=goose, arms={"noskill": arm("noskill")}, ANTHROPIC_API_KEY="sk-goose-secret-123",
                                        FAKE_GOOSE=os.path.join(HARNESS_DIR, "tests", "data", "goose", "success")), 0, self.err)
        b = self.batch_dir()
        for d, _, names in os.walk(b):
            for n in names:
                self.assertNotIn("sk-goose-secret-123", read(os.path.join(d, n)), os.path.join(d, n))
        argv = read(os.path.join(self.home, "goose-argv.txt")).splitlines()
        self.assertEqual(argv[-1], "key_set=yes")  # docker got the value from the environment
        self.assertEqual(argv[argv.index("ANTHROPIC_API_KEY") - 1], "-e")
        self.assertFalse([x for x in argv if "API_KEY=" in x])
        self.assertIn(bench.goose_client.IMAGE, argv)
        self.assertEqual(argv[argv.index("--network") + 1], "host")
        m = load(os.path.join(b, "manifest.json"))
        self.assertEqual((m["client"], m["provider"], m["goose_image"], m["client_version_pre"]),
                         ("goose", "anthropic", bench.goose_client.IMAGE, "1.57.0"))
        for f in ("goose_client.py", "goose/goose-without-extensions.sh"):
            self.assertIn(f, m["file_hashes_sha256"])
        trial = os.path.join(b, "0-noskill")
        self.assertEqual(read(os.path.join(self.home, "docker-rm.log")).strip(), "rm -f goose-%s-0-noskill" % os.path.basename(b))
        recipe = load(os.path.join(trial, "goose", "recipe.yaml"))
        self.assertEqual(recipe["extensions"][0]["uri"], "http://127.0.0.1:%s/jaeger/ui/api/ai/mcp/" % self.port)
        self.assertEqual(recipe["instructions"], text("system-prompt.txt"))
        self.assertTrue(os.path.isfile(os.path.join(trial, "goose-events.jsonl")))
        [s] = bench.read_jsonl(os.path.join(b, "scores.jsonl"))
        self.assertEqual((s["verdict"], s["sandbox_ok"], s["call_sequence"], s["client_version"]),
                         ("ABSTAIN", True, ["get_services"], "1.57.0"))

    def test_goose_needs_its_image_and_key(self):
        goose = {"client": "goose", "provider": "openrouter", "model": "m"}
        self.assertEqual(self.run_batch(1, "--dry-run", run=goose, OPENROUTER_API_KEY="k", FAKE_NO_GOOSE_IMAGE="1"), 1)
        self.assertIn("fix: docker pull " + bench.goose_client.IMAGE, self.err)
        self.assertEqual(self.run_batch(1, "--dry-run", run=goose), 1)
        self.assertIn("OPENROUTER_API_KEY is empty in fixture.env", self.err)
        self.assertEqual(self.run_batch(1, "--dry-run", run=goose, OPENROUTER_API_KEY="k", FAKE_GOOSE_NO_VERSION="1"), 1)
        self.assertIn("the Goose image reported no version", self.err)

    def test_hung_goose_cell_is_stopped_and_its_container_removed(self):
        goose = {"client": "goose", "provider": "anthropic", "model": "m"}
        with mock.patch.object(bench, "CELL_TIMEOUT_S", 0.5):
            self.assertEqual(self.run_batch(1, run=goose, arms={"noskill": arm("noskill")}, ANTHROPIC_API_KEY="k",
                                            FAKE_GOOSE_HANG="1"), 0, self.err)
        b = self.batch_dir()
        [c] = bench.read_jsonl(os.path.join(b, "cells.jsonl"))
        self.assertEqual((c["failed"], c["trial_exit_code"]), (True, 124))
        self.assertEqual(read(os.path.join(self.home, "docker-rm.log")).strip(), "rm -f goose-%s-0-noskill" % os.path.basename(b))
        stream = bench.read_jsonl(os.path.join(b, "0-noskill", "stream.jsonl"))
        self.assertEqual((stream[0]["subtype"], stream[-1]["subtype"]), ("init", "error_during_execution"))

    def test_codex_client_stops_at_max_turns(self):
        out = os.path.join(self.home, "trial")
        os.makedirs(out)
        mcp = os.path.join(out, "mcp.json")
        pathlib.Path(mcp).write_text('{"mcpServers": {}}')
        r = subprocess.run([sys.executable, bench.CODEX_CLIENT, "--model", "m", "--effort", "high", "--max-turns", "1",
                            "--prompt-file", os.path.join(HARNESS_DIR, "prompts", "noskill.txt"),
                            "--system-prompt-file", bench.SYSTEM_PROMPT, "--schema-file", bench.VERDICT_SCHEMA,
                            "--mcp-config", mcp, "--out-dir", out], env=dict(os.environ, **self.env), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        final = bench.read_jsonl(os.path.join(out, "stream.jsonl"))[-1]
        self.assertEqual((final["subtype"], final["num_tool_calls"], final["structured_output"]), ("error_max_turns", 1, None))
        self.assertNotIn("mcp_servers", " ".join(bench.read_jsonl(os.path.join(self.home, "codex-calls.jsonl"))[0]["argv"]))

    def test_codex_answer_must_match_the_verdict_schema(self):
        verdict = {"root_cause_service": "payment", "root_cause_operation": "charge", "mechanism": "invalid token", "cascading": [], "confidence": "high", "evidence_span_ids": [], "abstain": False}
        schema = load(bench.VERDICT_SCHEMA)
        self.assertEqual(codex_client.extract_json("```json\n%s\n```" % json.dumps(verdict), schema), (verdict, None))
        obj, errs = codex_client.extract_json(json.dumps(dict(verdict, confidence="certain")), schema)
        self.assertIsNone(obj)
        self.assertTrue(errs)

    def test_summary_counts_errors_and_stops(self):
        self.assertEqual(self.run_batch(1, FAKE_STOP="error_max_turns"), 0, self.err)
        blog = read(os.path.join(self.batch_dir(), "batch.log"))
        self.assertIn("noskill: n=1 PASS=1 PARTIAL=0 FAIL=0 ABSTAIN=0 ERROR=0 INVALID=0 LEAK=0 stops=error_max_turns:1 ", blog)
        self.assertIn("ERROR=0 INVALID=0 LEAK=0 stops=error_max_turns:1 read_skill_attempted", blog)
        self.assertIn("stops error_max_turns:1", self.err)
        shutil.rmtree(self.runs)
        self.assertEqual(self.run_batch(1, FAKE_JUNK="1"), 7, self.err)
        blog = read(os.path.join(self.batch_dir(), "batch.log"))
        self.assertIn("noskill: n=1 PASS=0 PARTIAL=0 FAIL=0 ABSTAIN=0 ERROR=1 INVALID=0 LEAK=0 stops=- ", blog)
        self.assertIn("band paymentFailure/noskill: n=1 passes=0", blog)
        self.assertIn("ERROR=1 INVALID=0 LEAK=0 stops=- read_skill_attempted", blog)
        self.assertRegex(self.err, r"noskill #0 +ERROR +- .*unscorable")

    def test_local_mode_runs_without_ssh(self):
        local = {"FIXTURE_SSH_USER": "", "FIXTURE_DEMO_DIR": "~/otel-demo-3.0.0", "HOME": self.home}
        self.assertEqual(self.run_batch(1, "--dry-run", **local), 0, self.err)
        self.assertIn("local: python3 - src/flagd/demo.flagd.json paymentFailure 100%", self.err)
        self.assertEqual(self.run_batch(1, **local), 0, self.err)
        self.assertFalse(os.path.exists(os.path.join(self.home, "ssh.log")))
        meta = load(glob.glob(os.path.join(self.batch_dir(), "0-*", "meta.json"))[0])
        self.assertTrue(meta["fault"]["ofrep_confirmed"])  # OFREP reads the flag file in the temp demo dir
        self.assertEqual(len(meta["otel_demo_ref"]), 40)
        self.assertTrue(load(os.path.join(self.batch_dir(), "restore.json"))["default_confirmed"])
        self.assertEqual(self.flag_file(), self.pristine())

    def test_dry_run(self):
        self.assertEqual(self.run_batch(2, "--dry-run"), 0, self.err)
        self.assertEqual(self.err.count(" arm="), 4)
        self.assertEqual(self.calls(), [])
        self.assertIn("DRY RUN", self.err)
        self.assertIn("manifest.json", self.err)
        self.assertNotIn("python3 -", read(os.path.join(self.home, "ssh.log")))
        self.assertEqual(self.flag_file(), self.pristine())

    def test_dry_run_seed_matches_a_real_run_with_the_same_seed(self):
        self.assertEqual(self.run_batch(3, "--dry-run", run={"seed": 42}), 0, self.err)
        planned = [l.strip().split(" -> ")[0] for l in self.err.splitlines() if l.strip().startswith("order_index=")]
        self.assertIn("seed=42", self.err)
        self.assertEqual(self.run_batch(3, run={"seed": 42}), 0, self.err)
        m = load(os.path.join(self.batch_dir(), "manifest.json"))
        self.assertEqual(m["seed"], 42)
        self.assertEqual(planned, ["order_index=%d arm=%s trial_index=%d prompt=%s" % (c["order_index"], c["arm"], c["trial_index"], c["arm"])
                                   for c in m["order"]])

    def test_dry_run_without_seed_says_the_real_order_differs(self):
        self.assertEqual(self.run_batch(1, "--dry-run"), 0, self.err)
        self.assertIn("set run.seed to", self.err)

    def test_dry_run_leaves_runs_dir_unchanged(self):
        os.makedirs(os.path.join(self.runs, "paymentFailure"))
        pathlib.Path(self.runs, "INDEX.md").write_text("x\n")
        before = sorted((r, sorted(f)) for r, _, f in os.walk(self.runs))
        self.assertEqual(self.run_batch(1, "--dry-run"), 0, self.err)
        self.assertEqual(sorted((r, sorted(f)) for r, _, f in os.walk(self.runs)), before)

    def test_first_only(self):
        self.assertEqual(self.run_batch(3, "--first-only"), 0, self.err)
        self.assertEqual(len(bench.read_jsonl(os.path.join(self.batch_dir(), "cells.jsonl"))), 1)
        self.assertEqual(len(self.calls()), 1)

    def test_hung_cell_is_killed_recorded_failed_and_flag_restored(self):
        with mock.patch.object(bench, "CELL_TIMEOUT_S", 0.5):
            self.assertEqual(self.run_batch(3, FAKE_CLAUDE_HANG="1"), 4, self.err)
        b = self.assert_restored()
        cells = bench.read_jsonl(os.path.join(b, "cells.jsonl"))
        self.assertEqual([(c["failed"], c["trial_exit_code"]) for c in cells], [(True, 124)] * 3)
        self.assertIn("cell killed after", read(os.path.join(b, "%d-%s" % (cells[0]["order_index"], cells[0]["arm"]), "stderr.txt")))

    def test_three_failures_abort_exit_4(self):
        self.assertEqual(self.run_batch(3, FAKE_CLAUDE_EXIT="1"), 4, self.err)
        b = self.batch_dir()
        cells = bench.read_jsonl(os.path.join(b, "cells.jsonl"))
        self.assertEqual([c["failed"] for c in cells], [True] * 3)
        self.assertTrue(load(os.path.join(b, "restore.json"))["default_confirmed"])
        self.assertEqual(self.flag_file(), self.pristine())

    def test_interrupted_exit_6(self):
        real = bench.run_cell
        n = {"calls": 0}

        def flaky(*a, **k):
            n["calls"] += 1
            if n["calls"] == 2:
                raise KeyboardInterrupt
            return real(*a, **k)

        with mock.patch.object(bench, "run_cell", flaky):
            self.assertEqual(self.run_batch(2), 6, self.err)
        self.assertEqual(len(bench.read_jsonl(os.path.join(self.batch_dir(), "cells.jsonl"))), 1)
        self.assertEqual(self.flag_file(), self.pristine())

    def assert_restored(self):
        b = self.batch_dir()
        self.assertTrue(load(os.path.join(b, "restore.json"))["default_confirmed"])
        self.assertIn("cp overlay/demo.flagd.json.pristine", read(os.path.join(self.home, "ssh.log")))
        self.assertEqual(self.flag_file(), self.pristine())
        self.assertEqual(glob.glob(os.path.join(b, "**", "*.tmp"), recursive=True), [])
        return b

    def test_signal_during_restore_is_ignored(self):
        for sig in ("TERM", "INT", "HUP"):
            with self.subTest(sig=sig):
                self.assertEqual(self.run_batch(1, FAKE_KILL_ON_RESTORE=sig), 0, self.err)
                self.assert_restored()
                shutil.rmtree(self.runs)

    def test_dead_stderr_at_restore_still_copies_the_flag_back(self):
        class Dying(io.StringIO):  # tee gone: every write from the Restore section on fails
            def write(self, s):
                if "Restore" in s:
                    self.dead = True
                if getattr(self, "dead", False):
                    raise BrokenPipeError
                return super().write(s)

            def flush(self):
                if getattr(self, "dead", False):
                    raise BrokenPipeError

        with mock.patch.dict(os.environ, self.env), mock.patch.object(bench, "sleep"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(Dying()):
            rc = bench.main(["run", self.knob()])
        self.assertEqual(rc, 0)
        b = self.assert_restored()
        self.assertIn("fault flag back at default", read(os.path.join(b, "batch.log")))

    def test_interrupt_during_fault_wait_scores_and_exits_6(self):
        calls = {"n": 0}

        def interrupt_once(_s):
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyboardInterrupt

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env), mock.patch.object(bench, "sleep", interrupt_once), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = bench.main(["run", self.knob()])
        self.assertEqual(rc, 6, err.getvalue())
        b = self.assert_restored()
        self.assertTrue(os.path.isfile(os.path.join(b, "scores.jsonl")))
        self.assertEqual(self.calls(), [])

    def test_a_trial_that_fails_to_score_exits_7(self):
        with mock.patch.object(bench.score, "score", side_effect=RuntimeError("grader timeout")):
            self.assertEqual(self.run_batch(1), 7, self.err)
        self.assertIn("2 of 2 trials failed to score", self.err)
        rows = bench.read_jsonl(os.path.join(self.batch_dir(), "scores.jsonl"))
        self.assertTrue(rows and all("grader timeout" in r["error"] for r in rows))

    def test_unconfirmed_restore_exits_5(self):
        self.assertEqual(self.run_batch(1, FAKE_FAIL_RESTORE="1"), 5, self.err)
        r = load(os.path.join(self.batch_dir(), "restore.json"))
        self.assertFalse(r["cp_ok"] or r["default_confirmed"])

    def ofrep_blind_until(self, reads):
        """An ofrep_variant that reports nothing for the first `reads` reads after the restore cp has run."""
        real, n = bench.ofrep_variant, {"after_cp": 0}

        def blind(cfg, flag):
            if "cp overlay/demo.flagd.json.pristine" in read(os.path.join(self.home, "ssh.log")):
                n["after_cp"] += 1
                if n["after_cp"] <= reads:
                    return ""
            return real(cfg, flag)

        return blind, n

    def test_no_baseline_traffic_aborts_with_a_fix(self):
        with mock.patch.object(bench, "traces", return_value=[]):
            self.assertEqual(self.run_batch(1), 1, self.err)
        self.assertIn("ABORT - only 0 baseline traces", self.err)
        self.assertIn("fix: run `docker compose ps -a` on the demo host", self.err)

    def test_ofrep_not_showing_the_flip_aborts_with_a_fix(self):
        real, first = bench.ofrep_variant, []

        def stuck(cfg, flag):
            first.append(first[0] if first else real(cfg, flag))
            return first[0]

        with mock.patch.object(bench, "ofrep_variant", stuck), mock.patch.object(bench, "sleep", lambda s: None):
            self.assertEqual(self.run_batch(1), 1, self.err)
        self.assertIn("ABORT - OFREP never showed", self.err)
        self.assertIn("fix: the flag file is restored automatically", self.err)
        self.assertIn("fault flag back at default", self.err.split("ABORT - OFREP never showed")[1])  # the restore really ran after the abort

    def test_slow_restore_is_confirmed_by_polling(self):
        blind, n = self.ofrep_blind_until(2)
        with mock.patch.object(bench, "ofrep_variant", blind):
            self.assertEqual(self.run_batch(1), 0, self.err)
        self.assertEqual(n["after_cp"], 3)
        self.assert_restored()

    def test_restore_never_confirmed_exits_5(self):
        blind, n = self.ofrep_blind_until(99)
        with mock.patch.object(bench, "ofrep_variant", blind):
            self.assertEqual(self.run_batch(1), 5, self.err)
        self.assertEqual(n["after_cp"], 5)
        self.assertFalse(load(os.path.join(self.batch_dir(), "restore.json"))["default_confirmed"])

    def test_interrupt_inside_a_cell_leaves_a_row(self):
        real, n = bench.observe, {"calls": 0}

        def observe(path):
            n["calls"] += 1
            if n["calls"] == 2:
                raise KeyboardInterrupt
            return real(path)

        with mock.patch.object(bench, "observe", observe):
            self.assertEqual(self.run_batch(2), 6, self.err)
        b = self.assert_restored()
        cells = bench.read_jsonl(os.path.join(b, "cells.jsonl"))
        self.assertEqual(len(cells), 2)
        self.assertTrue(cells[1]["interrupted"] and cells[1]["failed"])
        on_disk = sorted(d for d in os.listdir(b) if os.path.isdir(os.path.join(b, d)))
        self.assertEqual(on_disk, sorted(os.path.basename(c["out_dir"]) for c in cells))
        # exit.txt is written as soon as claude returns, before anything that can fail
        self.assertEqual(read(os.path.join(b, "%d-%s" % (cells[1]["order_index"], cells[1]["arm"]), "exit.txt")), "exit=0\n")

    def test_observe_skips_non_object_lines(self):
        p = os.path.join(self.home, "s.jsonl")
        pathlib.Path(p).write_text('[1, 2]\n"x"\n{"type": "system", "subtype": "init", "model": "m"}\n')
        self.assertEqual(bench.observe(p)["model"], "m")

    def test_preflight_failure_exit_1_touches_nothing(self):
        self.assertEqual(self.run_batch(1, FAKE_CONTAINERS="3"), 1)
        self.assertIn("only 3 containers running", self.err)
        self.assertEqual(glob.glob(os.path.join(self.runs, "*", "batch-*")), [])
        self.assertEqual(self.calls(), [])

    def test_failed_check_shows_its_detail_on_screen(self):
        self.assertEqual(self.run_batch(1, FIXTURE_DEMO_DIR="no-such-dir"), 1)
        self.assertIn("FAIL  demo reachable", self.err)
        self.assertIn("pre-flight: FAIL - fixture unreachable or no demo dir", self.err)  # a detail line
        self.assertIn("        fix: check FIXTURE_HOST and FIXTURE_DEMO_DIR", self.err)  # die(fix=) prints the way out
        self.assertEqual(self.err.count("bench: ABORT"), 1)
        self.assertTrue(self.err.rstrip().endswith("the fault flag was not touched"))  # one ABORT line, last
        self.assertNotIn("readiness paymentFailure: version", self.err)  # a passed step stays compact

    def test_no_colour_off_a_tty_force_colour_on_screen_only(self):
        self.assertEqual(self.run_batch(1, FORCE_COLOR="", NO_COLOR=""), 0, self.err)
        self.assertNotIn("\x1b", self.err)
        shutil.rmtree(self.runs)
        self.assertEqual(self.run_batch(1, FORCE_COLOR="1"), 0, self.err)
        self.assertIn("\x1b[32mok  \x1b[0m", self.err)
        b = self.batch_dir()
        for f in glob.glob(os.path.join(b, "**", "*.json*"), recursive=True) + [os.path.join(b, "batch.log")]:
            self.assertNotIn("\x1b", read(f), f)
        self.assertIn("  ok    demo reachable", read(os.path.join(b, "batch.log")))

    def test_live_line_only_on_a_tty(self):
        class TTY(io.StringIO):
            def isatty(self):
                return True
        err = TTY()
        with mock.patch.dict(os.environ, dict(self.env, FORCE_COLOR="", NO_COLOR="1")), mock.patch.object(bench, "sleep"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(bench.main(["run", self.knob()]), 0)
        self.assertIn("running...", err.getvalue())
        self.assertIn("\r\x1b[K", err.getvalue())
        self.assertNotIn("\x1b[3", err.getvalue())  # NO_COLOR: no colours even on a TTY
        self.assertNotIn("running...", read(os.path.join(self.batch_dir(), "batch.log")))


class TestStageAndFix(unittest.TestCase):
    def test_stage_header_and_fix_line_on_a_pipe(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"FORCE_COLOR": "", "NO_COLOR": "1"}), contextlib.redirect_stderr(err):
            log = bench.Log()
            log.stage(4, 6, "Demo", "healthy and quiet")
            log.check(False, "host busy")
            log.detail("a detail line")
            log.abort(None)
            log.sub("fix: wait")
        self.assertEqual(err.getvalue(), "\n[4/6] Demo               healthy and quiet\n  FAIL  host busy\n        a detail line\n        fix: wait\n")


class TestLogGlyphs(unittest.TestCase):
    def render(self, tty, glyphs):
        class S(io.StringIO):
            def isatty(self):
                return tty
        err = S()
        with mock.patch.dict(os.environ, {"FORCE_COLOR": "", "NO_COLOR": "1"}), mock.patch.object(bench, "glyphs", lambda s: glyphs), \
                contextlib.redirect_stderr(err):
            log = bench.Log()
            log.check(True, "claude accepts the flags")
            log.sub("arms here: baseline")
            log.check(True, "graded 1 answer", tty_only=True)
        return err.getvalue(), log.lines

    def test_glyph_tty_reads_glyph_then_text(self):
        screen, lines = self.render(True, True)
        self.assertEqual(screen, "  \u2713 claude accepts the flags\n    arms here: baseline\n  \u2713 graded 1 answer\n")
        self.assertEqual(lines, ["  ok    claude accepts the flags", "        arms here: baseline"])

    def test_pipe_unchanged_and_tty_only_line_absent(self):
        screen, lines = self.render(False, False)
        self.assertEqual(screen, "  ok    claude accepts the flags\n        arms here: baseline\n")
        self.assertEqual(lines, ["  ok    claude accepts the flags", "        arms here: baseline"])


class TestExperiment(BenchCase):
    """The shipped release-v2200-vs-main-paymentFailure.json, cut to one trial per arm. Its variant arm's
    image record (main-d22c65b9) names no descriptions, so tests that need the description check add the key."""

    def shipped(self):
        doc = load(os.path.join(HARNESS_DIR, "experiments", "release-v2200-vs-main-paymentFailure.json"))
        doc["run"]["n_per_arm"] = 1
        for k in ("client_version", "bill_api_key"):  # the CLI and key of the batch's host; the fake has neither
            doc["run"].pop(k)
        doc["run"]["model"] = "sonnet"  # the fake CLI's stream reports claude-sonnet-5, which a full id would not match
        path = os.path.join(self._tmp.name, "release-v2200-vs-main-paymentFailure.json")
        pathlib.Path(path).write_text(json.dumps(doc))
        return path

    def with_record(self, **keys):
        """Run the shipped experiment on the variant image with extra keys on that image's record (its real
        instructions key is dropped: the fake serves none). A path in the temp dir stays as is under the joins
        in experiments/descriptions/ and experiments/instructions/."""
        real = bench.load_json
        rec = lambda p: dict(real(p), **{"instructions": None, **keys}) if real(p).get("tag") == VARIANT_IMAGE else real(p)
        with mock.patch.object(bench, "load_json", rec):
            return self.bench("run", self.shipped(), FAKE_IMAGE=VARIANT_IMAGE)

    def descriptions_file(self):
        f = os.path.join(self._tmp.name, "descriptions.json")
        pathlib.Path(f).write_text(json.dumps(DESC_CHANGE))
        return f

    def test_variant_arm_runs_with_matching_descriptions(self):
        Fake.served_descriptions = {k: v for k, v in DESC_CHANGE.items() if k != "status"}
        rc = self.with_record(descriptions=self.descriptions_file())
        self.assertEqual(rc, 0, self.err)
        b = self.batch_dir()
        m = load(os.path.join(b, "manifest.json"))
        self.assertEqual(m["arms"], ["variant"])
        self.assertEqual(m["arm_pin"], {"arm": "variant", "expected_image": VARIANT_IMAGE, "sut_jaeger_image_at_run": VARIANT_IMAGE})
        self.assertEqual(m["tools_description_check"]["mode"], "compared")
        self.assertEqual(m["experiment"]["name"], "release-v2200-vs-main-paymentFailure")
        self.assertEqual(m["jaeger_commit"], "d22c65b9bcbf7261172d5c31fb3ffa6b393f1d6c")
        trial = os.path.join(b, "0-variant")
        meta = load(os.path.join(trial, "meta.json"))
        self.assertEqual((meta["arm"], meta["prompt_name"], meta["model_requested"]), ("variant", "neutral", "sonnet"))
        self.assertEqual(meta["tool_descriptions_check"], "compared")
        self.assertEqual(meta["tool_descriptions_file"], bench.repo_relpath(self.descriptions_file()))
        self.assertEqual(meta["experiment"]["name"], "release-v2200-vs-main-paymentFailure")
        self.assertEqual(meta["tools_list_sha256"], bench.sha256_file(os.path.join(b, "tools.json")))
        self.assertEqual(bench.read_jsonl(os.path.join(b, "cells.jsonl"))[0]["arm"], "variant")

    def with_instructions(self, text):
        """Run the shipped experiment with the variant image record naming an instructions file."""
        f = os.path.join(self._tmp.name, "instructions.md")
        pathlib.Path(f).write_text(text + "\n")
        return self.with_record(instructions=f)

    def test_instructions_mismatch_aborts(self):
        Fake.served_instructions = "served wording"
        self.assertEqual(self.with_instructions("pre-registered wording"), 1)
        self.assertIn("not serving the pre-registered MCP instructions", self.err)
        self.assertEqual(self.calls(), [])
        self.assertEqual(glob.glob(os.path.join(self.runs, "*", "batch-*")), [])

    def test_instructions_match_is_recorded(self):
        Fake.served_instructions = "served wording"
        self.assertEqual(self.with_instructions("served wording"), 0, self.err)
        m = load(os.path.join(self.batch_dir(), "manifest.json"))
        self.assertEqual(m["instructions_sha256"], hashlib.sha256(b"served wording").hexdigest())

    def test_baseline_arm_is_recorded_only(self):
        self.assertEqual(self.bench("run", self.shipped()), 0, self.err)
        meta = load(os.path.join(self.batch_dir(), "0-baseline", "meta.json"))
        self.assertEqual(meta["tool_descriptions_check"], "recorded_only")

    def test_description_mismatch_aborts(self):
        rc = self.with_record(descriptions=self.descriptions_file())
        self.assertEqual(rc, 1)
        self.assertIn("MISMATCH get_trace_errors", self.err)
        self.assertEqual(self.calls(), [])
        self.assertEqual(glob.glob(os.path.join(self.runs, "*", "batch-*")), [])
        self.assertEqual(self.flag_file(), self.pristine())

    def test_leak_in_served_descriptions_aborts_before_the_flag(self):
        Fake.served_descriptions = {"get_trace_errors": "Errors of a trace, e.g. the paymentFailure path."}
        self.assertEqual(self.run_batch(1), 1)
        self.assertIn("served tools/list:9:paymentFailure", self.err)
        self.assertIn("ABORT - a leak word in what the agent would see", self.err)
        self.assertIn("fix: remove the named word from the prompt", self.err)
        self.assertEqual(self.flag_file(), self.pristine())
        self.assertFalse(os.path.exists(os.path.join(self.home, "claude-calls.jsonl")))

    def test_image_pin_refuses_a_mismatch(self):
        rc = self.bench("run", self.shipped(), FAKE_IMAGE="other/jaeger:1")
        self.assertEqual(rc, 1)
        self.assertIn("which no arm of experiment release-v2200-vs-main-paymentFailure expects", self.err)
        self.assertEqual(self.calls(), [])
        self.assertEqual(glob.glob(os.path.join(self.runs, "*", "batch-*")), [])
        self.assertFalse(os.path.exists(self.records))


class TestSharedImage(BenchCase):
    """Two arms on one image: the non-baseline arm serves the baseline's wording,
    so no descriptions check applies."""

    def test_same_image_without_descriptions_is_recorded_only(self):
        self.assertEqual(self.run_batch(1), 0, self.err)
        m = load(os.path.join(self.batch_dir(), "manifest.json"))
        self.assertEqual(m["arms"], ["noskill", "skill"])
        self.assertNotIn("arm_pin", m)
        for d in glob.glob(os.path.join(self.batch_dir(), "*-*", "meta.json")):
            self.assertEqual(load(d)["tool_descriptions_check"], "recorded_only")


class TestKnobFile(BenchCase):
    def refused(self, doc_edit, message):
        self.knob()
        doc = load(self.knob_path)
        doc_edit(doc)
        pathlib.Path(self.knob_path).write_text(json.dumps(doc))
        self.assertEqual(self.bench("run", self.knob_path), 1)
        self.assertIn(message, self.err)
        self.assertEqual(os.listdir(self.home), ["otel-demo-3.0.0"])  # nothing ran, not even ssh
        self.assertFalse(os.path.exists(self.runs) or os.path.exists(self.records))

    def test_unknown_key_rejected(self):
        self.refused(lambda d: d["run"].update(temperature=0), "run: unknown key(s): temperature")
        self.refused(lambda d: d.update(n_per_arm=10), "experiment: unknown key(s): n_per_arm")

    def test_missing_model_rejected(self):
        self.refused(lambda d: d["run"].pop("model"), "run: missing key(s): model")

    def test_provider_must_be_null(self):
        self.refused(lambda d: d["run"].update(provider="openai"), "run.provider must be null")

    def test_goose_needs_a_known_provider(self):
        self.refused(lambda d: d["run"].update(client="goose"), "run.provider must be one of anthropic, openrouter for client goose")

    def test_bill_api_key_only_for_cli(self):
        self.refused(lambda d: d["run"].update(client="codex", bill_api_key=True),
                     "run.bill_api_key must be true or false, and true only for client cli")

    def test_draft_refused(self):
        self.refused(lambda d: d.update(status="DRAFT"), "DRAFT")

    def test_client_version_must_be_a_non_empty_string(self):
        self.refused(lambda d: d["run"].update(client_version=""), "run.client_version must be a non-empty string")

    def test_client_version_mismatch_refuses_before_the_fixture(self):
        self.refused(lambda d: d["run"].update(client_version="2.1.283"),
                     "run.client_version is 2.1.283 but the cli client reports 2.1.282")
        self.assertIn("fix: remove run.client_version from the experiment file to run on your CLI, or install 2.1.283 for an exact re-run", self.err)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.flag_file(), self.pristine())

    def test_client_version_match_runs_and_is_recorded(self):
        self.assertEqual(self.run_batch(1, run={"client_version": "2.1.282"}), 0, self.err)
        m = load(os.path.join(self.batch_dir(), "manifest.json"))
        self.assertEqual(m["preflight"]["client_version"], "PASS")
        self.assertEqual(m["experiment"]["content"]["run"]["client_version"], "2.1.282")
        self.assertEqual(load(os.path.join(self.batch_dir(), "preflight.json"))["client_version"], "PASS")

    def test_client_version_absent_is_not_checked(self):
        self.assertEqual(self.run_batch(1), 0, self.err)
        m = load(os.path.join(self.batch_dir(), "manifest.json"))
        self.assertIsNone(m["preflight"]["client_version"])
        self.assertEqual(m["system_under_test"]["python_version"], platform.python_version())

    def test_shipped_experiments_validate(self):
        for f in glob.glob(os.path.join(HARNESS_DIR, "experiments", "*.json")):
            bench.load_experiment(f)


class TestRecords(BenchCase):
    def kept(self):
        dirs = glob.glob(os.path.join(self.records, "t", "batch-*"))
        self.assertEqual(len(dirs), 1, dirs)
        return dirs[0]

    def test_smoke_batch_is_not_a_record(self):
        log = mock.MagicMock()
        bench.keep_records(self.runs, bench.SMOKE_EXPERIMENT, log)
        self.assertFalse(os.path.exists(self.records))
        log.assert_not_called()

    def test_batch_line_is_what_measure_sh_parses(self):
        """measure.sh reads the batch dir with sed -n 's/^Batch  *\\([^ ]*\\).*/\\1/p' from the screen log."""
        self.assertEqual(self.run_batch(1), 0, self.err)
        found = re.findall(r"(?m)^Batch +([^ ]+)", bench.ANSI.sub("", self.err))
        self.assertEqual(found, [bench.shown(self.batch_dir())])

    def test_batch_records_copied_without_trajectories(self):
        self.assertEqual(self.run_batch(1), 0, self.err)
        b, kept = self.batch_dir(), self.kept()
        self.assertEqual(os.path.basename(kept), os.path.basename(b))
        self.assertEqual(sorted(os.listdir(kept)), sorted(bench.RECORD_FILES))
        for f in bench.RECORD_FILES:
            self.assertEqual(read(os.path.join(kept, f)), read(os.path.join(b, f)))
        # cells point at trial dirs by name only, so no local path reaches the repository
        self.assertEqual({c["out_dir"] for c in bench.read_jsonl(os.path.join(kept, "cells.jsonl"))},
                         {d for d in os.listdir(b) if os.path.isdir(os.path.join(b, d))})
        self.assertNotIn(self.runs, "".join(read(os.path.join(kept, f)) for f in bench.RECORD_FILES))
        # the run itself regenerated the index, so verify passes with no `index` step
        self.assertIn("| [t/%s](t/%s/) |" % (os.path.basename(b), os.path.basename(b)), read(os.path.join(self.records, "INDEX.md")))
        self.assertEqual(self.bench("verify"), 0, self.out)
        # judge writes RESULT.md and regenerates the index through the same write_index
        bench.judge.write_result(["x: PASS", "EXPERIMENT PASS"], kept, b)
        result = read(os.path.join(self.records, "t", "RESULT.md"))
        self.assertIn("- baseline batch: `%s`" % os.path.basename(b), result)
        self.assertIn("| t | EXPERIMENT PASS | [RESULT.md](t/RESULT.md) |", read(os.path.join(self.records, "INDEX.md")))
        self.assertEqual(self.bench("verify"), 0, self.out)

    def test_batch_records_never_carry_fixture_identity_or_local_paths(self):
        """manifest/preflight/cells/scores/restore get committed publicly, so FIXTURE_HOST,
        FIXTURE_SSH_USER, FIXTURE_DEMO_DIR and any absolute local path must never reach them."""
        host, port = "localhost", self.port  # the class server; "localhost" is a needle distinct from "127.0.0.1"
        ssh_user, demo_name = "distinct-ssh-user-b7e1", "distinct-demo-dir-b7e1"
        os.symlink(self.demo, os.path.join(self.home, demo_name))  # same files, a distinctive name
        self.assertEqual(self.run_batch(1, ANTHROPIC_API_KEY="sk-test", OPENAI_API_KEY="sk-o",
                                    OPENAI_BASE_URL="http://x", FIXTURE_HOST=host, JAEGER_UI_PORT=port,
                                    OFREP_PORT=port, FIXTURE_SSH_USER=ssh_user, FIXTURE_DEMO_DIR=demo_name),
                         0, self.err)
        needles = [host, ssh_user, demo_name, self._tmp.name, os.path.expanduser("~")]
        kept = self.kept()
        for f in bench.RECORD_FILES:
            content = read(os.path.join(kept, f))
            for needle in needles:
                self.assertNotIn(needle, content, (f, needle))

    def pack(self, **env):
        with mock.patch.object(bench, "DIST", os.path.join(self._tmp.name, "dist")):
            rc = self.bench("pack", "t", **env)
        return rc, os.path.join(self._tmp.name, "dist", "t-trajectories.tar.gz")

    def test_pack_is_reproducible_and_host_free(self):
        self.assertEqual(self.run_batch(1), 0, self.err)
        rc, tgz = self.pack()
        self.assertEqual(rc, 0, self.out)
        digest = bench.sha256_file(tgz)
        self.assertEqual(read(os.path.join(self.records, "t", "trajectories.sha256")), "%s  t-trajectories.tar.gz\n" % digest)
        self.assertEqual(self.pack()[0], 0, self.out)
        self.assertEqual(bench.sha256_file(tgz), digest)
        with tarfile.open(tgz) as tar:
            names = tar.getnames()
            self.assertEqual(names, sorted(names))
            self.assertTrue(all(n.startswith("paymentFailure/" + os.path.basename(self.batch_dir()) + "/") for n in names))
            self.assertTrue(all(m.mtime == 0 and m.uid == 0 and m.gid == 0 for m in tar.getmembers()))
            mcp = [n for n in names if n.endswith("/mcp.json")]
            self.assertEqual(len(mcp), 2)
            for n in mcp:
                url = json.loads(tar.extractfile(n).read())["mcpServers"]["jaeger"]["url"]
                self.assertEqual(url, ":%s/jaeger/ui/api/ai/mcp/" % self.port)
            self.assertIn("stream.jsonl", {os.path.basename(n) for n in names})
            self.assertNotIn("batch.log", {os.path.basename(n) for n in names})
        # the file under RUNS_DIR is untouched
        self.assertIn("127.0.0.1", read(glob.glob(os.path.join(self.batch_dir(), "*", "mcp.json"))[0]))

    def test_pack_refuses_a_planted_host_or_key(self):
        self.assertEqual(self.run_batch(1), 0, self.err)
        stream = glob.glob(os.path.join(self.batch_dir(), "0-*", "stream.jsonl"))[0]
        with open(stream, "a") as f:
            f.write(json.dumps({"type": "x", "text": "http://fixture.example.test:1/ key sk-planted-4f2a"}) + "\n")
        rc, tgz = self.pack(ANTHROPIC_API_KEY="sk-planted-4f2a", FIXTURE_HOST="fixture.example.test")
        self.assertEqual(rc, 1, self.out)
        self.assertFalse(os.path.exists(tgz))
        self.assertFalse(os.path.exists(os.path.join(self.records, "t", "trajectories.sha256")))
        self.assertIn("stream.jsonl: FIXTURE_HOST (1)", self.out)
        self.assertIn("stream.jsonl: ANTHROPIC_API_KEY (1)", self.out)
        self.assertNotIn("sk-planted-4f2a", self.out)

    def test_pack_passes_a_fresh_run_under_home(self):
        """RUNS_DIR under HOME: any absolute path a run records would carry the home dir and refuse the pack."""
        home = os.path.join(self._tmp.name, "fakehome")
        env = dict(HOME=home, RUNS_DIR=os.path.join(home, "runs"))
        self.assertEqual(self.run_batch(1, **env), 0, self.err)
        rc, tgz = self.pack(**env)
        self.assertEqual(rc, 0, self.out)
        self.assertTrue(os.path.isfile(tgz))

    def test_records_kept_after_an_interrupt(self):
        with mock.patch.object(bench, "run_cell", mock.Mock(side_effect=KeyboardInterrupt)):
            self.assertEqual(self.run_batch(1), 6, self.err)
        self.assertEqual(sorted(os.listdir(self.kept())), sorted(bench.RECORD_FILES))


class TestOracle(BenchCase):
    def flip(self, value):
        doc = json.loads(self.flag_file())
        doc["flags"]["paymentFailure"]["defaultVariant"] = value
        pathlib.Path(self.demo, "src", "flagd", "demo.flagd.json").write_text(json.dumps(doc))

    def test_pass_under_fault_names_the_matching_call(self):
        self.flip("100%")
        self.assertEqual(self.bench("oracle", "paymentFailure"), 0, self.out)
        self.assertIn('oracle paymentFailure: PASS', self.out)
        self.assertIn('call 2: get_trace_errors {"trace_id": "aa11"}', self.out)
        calls = [c for c in Fake.mcp_calls if c[0] == "tools/call"]
        self.assertEqual(calls, [("tools/call", "search_traces", {"service_name": "checkout", "with_errors": True}),
                                 ("tools/call", "get_trace_errors", {"trace_id": "aa11"})])

    def test_sql_only_server_gets_a_statement_and_still_reaches_the_signal(self):
        Fake.sql_only = True
        self.flip("100%")
        self.assertEqual(self.bench("oracle", "paymentFailure"), 0, self.out)
        stmt = ("SELECT DISTINCT trace_id FROM spans WHERE between(start_time, now() - '1h', now()) "
                "AND service = 'checkout' AND status = 'error' LIMIT 10")
        calls = [c for c in Fake.mcp_calls if c[0] == "tools/call"]
        self.assertEqual(calls, [("tools/call", "find_traces", {"sql": stmt}),
                                 ("tools/call", "get_trace_errors", {"trace_id": "aa11"})])
        self.assertIn(stmt, self.out)

    def test_batch_oracle_sends_sql_to_a_sql_only_server(self):
        Fake.sql_only = True
        self.env["FAKE_SQL_ONLY"] = "1"
        self.assertEqual(self.run_batch(1), 0, self.err)
        self.assertEqual(load(os.path.join(self.batch_dir(), "preflight.json"))["oracle"], "PASS")
        searches = [c for c in Fake.mcp_calls if c[1] == "find_traces"]
        self.assertTrue(searches and all(set(c[2]) == {"sql"} for c in searches))

    def test_fail_without_fault_tries_every_trace(self):
        self.assertEqual(self.bench("oracle", "paymentFailure"), 1, self.out)
        self.assertIn("oracle paymentFailure: FAIL", self.out)
        self.assertEqual([c[2] for c in Fake.mcp_calls if c[1] == "get_trace_errors"], [{"trace_id": "aa11"}, {"trace_id": "bb22"}])

    def test_unreachable_endpoint_fails(self):
        self.assertEqual(self.bench("oracle", "paymentFailure", JAEGER_UI_PORT="1"), 1, self.out)
        self.assertIn("oracle paymentFailure: FAIL", self.out)

    def test_malformed_result_fails_cleanly(self):
        Fake.oracle_malformed = True
        self.assertEqual(self.bench("oracle", "paymentFailure"), 1, self.out)
        self.assertIn("oracle paymentFailure: FAIL", self.out)

    def test_batch_survives_a_malformed_oracle_result(self):
        Fake.oracle_malformed = True
        self.assertEqual(self.run_batch(1), 0, self.err)
        self.assertEqual(load(os.path.join(self.batch_dir(), "preflight.json"))["oracle"], "FAIL")
        self.assertTrue(load(os.path.join(self.batch_dir(), "restore.json"))["default_confirmed"])

    def test_shipped_scenarios_have_an_oracle(self):
        for f in glob.glob(os.path.join(HARNESS_DIR, "scenarios", "*.json")):
            calls = load(f)["oracle"]
            self.assertTrue(calls and all(isinstance(c.get("tool"), str) and isinstance(c.get("arguments"), dict) for c in calls), f)

    def test_batch_warns_and_runs_trials_when_oracle_fails(self):
        Fake.oracle_signal = False
        self.assertEqual(self.run_batch(1), 0, self.err)
        self.assertEqual(len(self.calls()), 2)  # trials ran
        self.assertTrue(load(os.path.join(self.batch_dir(), "restore.json"))["default_confirmed"])
        self.assertEqual(self.flag_file(), self.pristine())
        pre = load(os.path.join(self.batch_dir(), "preflight.json"))
        self.assertEqual(pre["oracle"], "FAIL")
        self.assertIn("never matched", pre["oracle_message"])
        self.assertEqual(load(os.path.join(self.batch_dir(), "manifest.json"))["preflight"]["oracle"], "FAIL")
        self.assertIn("trials go on (the fault is in the traces)", self.err.lower().replace("\n", " ").replace("        ", ""))
        url = next(l.split("Report it: ")[1] for l in self.err.splitlines() if "Report it: https://github.com/lopster568/jaeger-mcp-evals/issues/new?" in l)
        self.assertLess(len(url), 6000)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(q["title"], ["oracle fails on %s: paymentFailure" % STOCK_IMAGE])
        self.assertIn("scenario: paymentFailure (version ", q["body"][0])
        self.assertIn("jaeger image: " + STOCK_IMAGE, q["body"][0])
        self.assertIn("served tools: ", q["body"][0])
        self.assertIn("oracle: FAIL - ", q["body"][0])

    def test_issue_url_cuts_the_error_text_not_the_structure(self):
        lines = bench.oracle_warning("s", 3, "img:1", ["a", "b"], "x" * 20000)
        url = lines[-1].split("Report it: ")[1]
        self.assertLess(len(url), 6000)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertIn("served tools: a, b\noracle: FAIL - x", q["body"][0])

    def test_batch_records_oracle_pass(self):
        self.assertEqual(self.run_batch(1), 0, self.err)
        pre = load(os.path.join(self.batch_dir(), "manifest.json"))["preflight"]
        self.assertEqual(pre["oracle"], "PASS")
        self.assertNotIn("issues/new", self.err)


class TestNoTools(BenchCase):
    def test_notools_arm_has_no_mcp_server(self):
        arms = {"notools": arm("noskill", tools=False)}
        self.assertEqual(self.bench("run", self.knob(arms, baseline_arm="notools", n_per_arm=2)), 0, self.err)
        b = self.batch_dir()
        m = load(os.path.join(b, "manifest.json"))
        self.assertEqual(m["arms"], ["notools"])
        self.assertIsNone(m["tools_list_sha256"])
        self.assertIsNone(m["mcp_endpoint"])
        self.assertIsNone(m["preflight"]["oracle"])
        self.assertFalse(os.path.exists(os.path.join(b, "tools.json")))
        self.assertEqual(Fake.mcp_calls, [])
        for c in bench.read_jsonl(os.path.join(b, "cells.jsonl")):
            trial = os.path.join(b, "%d-notools" % c["order_index"])
            self.assertEqual(load(os.path.join(trial, "mcp.json")), {"mcpServers": {}})
            meta = load(os.path.join(trial, "meta.json"))
            self.assertEqual(list(meta), META_V4_KEYS)
            self.assertEqual((meta["arm"], meta["prompt_name"]), ("notools", "noskill"))
            self.assertEqual(meta["tool_descriptions_check"], "no_tools")
            self.assertIsNone(meta["tools_count"])
        # Same argv as a tools arm: only the mcp.json it points at differs.
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[2], text("prompts/noskill.txt"))
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(len(bench.read_jsonl(os.path.join(b, "scores.jsonl"))), 2)

    def test_notools_beside_a_tools_arm(self):
        arms = {"noskill": arm("noskill"), "notools": arm("noskill", tools=False)}
        self.assertEqual(self.run_batch(1, arms=arms), 0, self.err)
        b = self.batch_dir()
        self.assertEqual(load(os.path.join(b, "preflight.json"))["oracle"], "PASS")
        meta = {load(p)["arm"]: load(p) for p in glob.glob(os.path.join(b, "*-*", "meta.json"))}
        self.assertIsNone(meta["notools"]["mcp_endpoint"])
        self.assertEqual(load(os.path.join(b, "%d-notools" % meta["notools"]["order_index"], "mcp.json")), {"mcpServers": {}})
        self.assertTrue(meta["noskill"]["mcp_endpoint"])


def pb(b):
    """{field: [values]} of one protobuf message: bytes when length-delimited, else int."""
    out, i = {}, 0

    def varint():
        nonlocal i
        n = shift = 0
        while True:
            c = b[i]
            i += 1
            n |= (c & 0x7F) << shift
            shift += 7
            if c < 0x80:
                return n
    while i < len(b):
        key = varint()
        if key & 7 == 0:
            v = varint()
        elif key & 7 == 1:
            v, i = int.from_bytes(b[i:i + 8], "little"), i + 8
        else:
            n = varint()
            v, i = b[i:i + n], i + n
        out.setdefault(key >> 3, []).append(v)
    return out


def pb_attrs(msgs):
    attrs = {}
    for raw in msgs:
        kv = pb(raw)
        v = pb(kv[2][0])
        attrs[kv[1][0].decode()] = v[1][0].decode() if 1 in v else v[3][0]
    return attrs


class TestOtlpSpans(unittest.TestCase):
    def events(self, stamped=True, usage=None):
        ts = lambda n: {"timestamp": "2026-11-13T05:45:%02d.500Z" % n} if stamped else {}
        use = lambda i, n: dict(type="assistant", message={"content": [{"type": "tool_use", "id": i, "name": "a", "input": {}}]}, **ts(n))
        res = lambda i, n: dict(type="user", message={"content": [{"type": "tool_result", "tool_use_id": i, "content": "ok"}]}, **ts(n))
        r = {"type": "result", "duration_ms": 9000, "result": "done",
             "usage": {"input_tokens": 1, "cache_creation_input_tokens": 2, "cache_read_input_tokens": 3, "output_tokens": 4}}
        r.update(usage or {"modelUsage": {"m1": {"inputTokens": 10, "cacheReadInputTokens": 20, "cacheCreationInputTokens": 30, "outputTokens": 5}}})
        return [use("t1", 41), use("t2", 42), res("t1", 43), res("t2", 47), r]

    def attrs(self, span):
        return {a["key"]: next(iter(a["value"].values())) for a in span["attributes"]}

    def test_cost_attributes_come_from_model_usage_on_the_llm_span_only(self):
        spans = bench.otlp.trace_spans("k", self.events(), 10**9, [], "the prompt")
        llm = [s for s in spans if self.attrs(s)["openinference.span.kind"] == "LLM"]
        self.assertEqual(len(llm), 1)
        a = self.attrs(llm[0])
        self.assertEqual((a["llm.model_name"], a["llm.token_count.prompt"], a["llm.token_count.completion"],
                          a["llm.token_count.prompt_details.cache_read"], a["llm.token_count.prompt_details.cache_write"]),
                         ("m1", "60", "5", "20", "30"))
        self.assertNotIn("llm.model_name", self.attrs(spans[0]))
        self.assertEqual(a["llm.provider"], "anthropic")  # the default
        spans = bench.otlp.trace_spans("k", self.events(), 10**9, [bench.otlp.kv("provider", "openrouter")])
        self.assertEqual([self.attrs(s)["llm.provider"] for s in spans if self.attrs(s)["openinference.span.kind"] == "LLM"],
                         ["openrouter"])

    def test_one_llm_span_per_model_and_usage_fallback(self):
        two = {"modelUsage": {"m1": {"inputTokens": 1}, "m2": {"inputTokens": 2, "outputTokens": 3}}}
        llm = [self.attrs(s) for s in bench.otlp.trace_spans("k", self.events(usage=two), 0, [])
               if self.attrs(s)["openinference.span.kind"] == "LLM"]
        self.assertEqual([a["llm.model_name"] for a in llm], ["m1", "m2"])
        self.assertIn("output.value", llm[0])
        self.assertNotIn("output.value", llm[1])
        old = self.events(usage={"modelUsage": None})
        llm = [self.attrs(s) for s in bench.otlp.trace_spans("k", old, 0, [bench.otlp.kv("model", "m9")])
               if self.attrs(s)["openinference.span.kind"] == "LLM"]
        self.assertEqual((llm[0]["llm.model_name"], llm[0]["llm.token_count.prompt"]), ("m9", "6"))

    def test_real_timestamps_bound_tool_spans_and_the_root_holds_them(self):
        t0 = int(1.7e9) * 10**9
        spans = bench.otlp.trace_spans("k", self.events(), t0, [])
        root, tools = spans[0], spans[1:3]
        ms = lambda s, k: int(s[k]) // 10**6 % 100000
        self.assertEqual([(ms(s, "startTimeUnixNano"), ms(s, "endTimeUnixNano")) for s in tools],
                         [(ms(tools[0], "startTimeUnixNano"), ms(tools[0], "startTimeUnixNano") + 2000),
                          (ms(tools[1], "startTimeUnixNano"), ms(tools[1], "startTimeUnixNano") + 5000)])
        self.assertEqual(int(tools[1]["startTimeUnixNano"]) - int(tools[0]["startTimeUnixNano"]), 10**9)
        for s in spans[1:]:
            self.assertGreaterEqual(int(s["startTimeUnixNano"]), int(root["startTimeUnixNano"]))
            self.assertLessEqual(int(s["endTimeUnixNano"]), int(root["endTimeUnixNano"]))
        self.assertNotIn("timestamp.synthetic", self.attrs(root))

    def test_answer_span_has_a_real_interval_and_the_root_reaches_the_last_event(self):
        at = lambda s: bench.otlp._ns("2026-11-13T05:45:%sZ" % s)
        ev = self.events()  # tool results end at :47.5; t0 + duration_ms is :49
        verdict = [dict(type="assistant", timestamp="2026-11-13T05:45:55.500Z",
                        message={"content": [{"type": "tool_use", "id": "v", "name": "StructuredOutput", "input": {}}]}),
                   dict(type="user", timestamp="2026-11-13T05:45:56.500Z",
                        message={"content": [{"type": "tool_result", "tool_use_id": "v", "content": "ok"}]})]
        spans = bench.otlp.trace_spans("k", ev[:-1] + verdict + ev[-1:], at("40"), [])
        answer = next(s for s in spans if s["name"] == "final-answer")
        self.assertEqual((int(answer["startTimeUnixNano"]), int(answer["endTimeUnixNano"])), (at("47.500"), at("56.500")))
        text = [dict(type="assistant", timestamp="2026-11-13T05:45:58.500Z", message={"content": [{"type": "text", "text": "x"}]})]
        root = bench.otlp.trace_spans("k", ev[:-1] + text + ev[-1:], at("40"), [])[0]
        self.assertEqual(int(root["endTimeUnixNano"]), at("58.500"))

    def test_missing_timestamps_fall_back_to_an_even_split_marked_synthetic(self):
        spans = bench.otlp.trace_spans("k", self.events(stamped=False), 10**9, [])
        self.assertEqual(self.attrs(spans[0])["timestamp.synthetic"], "true")
        self.assertEqual([int(s["endTimeUnixNano"]) - int(s["startTimeUnixNano"]) for s in spans[1:]], [3 * 10**9] * 3)

    def test_root_carries_prompt_and_answer_truncated(self):
        a = self.attrs(bench.otlp.trace_spans("k", self.events(), 0, [], "p" * 20000)[0])
        self.assertTrue(a["input.value"].endswith("...[truncated]"))
        self.assertEqual(a["output.value"], "done")


class TestExport(BenchCase):
    def spans(self, body):
        rs = pb(pb(body)[1][0])
        resource = pb_attrs(pb(rs[1][0])[1])
        return resource, [dict(pb(s), attrs=pb_attrs(pb(s).get(9, []))) for s in pb(rs[2][0])[2]]

    def test_export_posts_one_trace_per_trial_with_run_attributes(self):
        self.assertEqual(self.run_batch(1, "--no-export"), 0, self.err)
        b = self.batch_dir()
        self.assertEqual(self.bench("export", b, PHOENIX_PORT=self.phoenix_port), 0, self.out)  # default endpoint from fixture.env
        self.assertEqual(self.bench("export", b, "--endpoint", "http://127.0.0.1:%s/" % self.phoenix_port), 0, self.out)
        (ctype, body), (_, again) = Fake.otlp_posts
        self.assertEqual(ctype, "application/x-protobuf")
        self.assertEqual(body, again)  # deterministic: a re-export sends the same trace and span ids
        resource, spans = self.spans(body)
        self.assertEqual(resource["openinference.project.name"], "t")
        kinds = [s["attrs"]["openinference.span.kind"] for s in spans]
        self.assertEqual(sorted(kinds), ["AGENT", "AGENT", "LLM", "LLM", "TOOL", "TOOL"])
        batch_id = os.path.basename(b)
        for root in (s for s in spans if s["attrs"]["openinference.span.kind"] == "AGENT"):
            a = root["attrs"]
            self.assertEqual({k: a[k] for k in ("experiment", "batch_id", "model", "client", "verdict", "trial_index")},
                             {"experiment": "t", "batch_id": batch_id, "model": "claude-sonnet-5", "client": "cli",
                              "verdict": "PASS", "trial_index": 0})
            trial = [d for d in os.listdir(b) if d.endswith("-" + a["arm"])][0]
            tid = root[1][0]
            self.assertEqual(tid.hex(), bench.hashlib.sha256(("paymentFailure/%s/%s" % (batch_id, trial)).encode()).hexdigest()[:32])
            children = [s for s in spans if s[1][0] == tid and s is not root]
            self.assertEqual(len(children), 2)
            self.assertTrue(all(s[4][0] == root[2][0] for s in children))  # parent is the AGENT span
            tool = next(s for s in children if s["attrs"]["openinference.span.kind"] == "TOOL")
            self.assertEqual(tool["attrs"]["tool.name"], "mcp__jaeger__get_trace_errors")
            self.assertEqual(tool["attrs"]["output.value"], "Payment request failed. Invalid token.")

    def snapshot(self):
        return {os.path.relpath(os.path.join(d, n), self._tmp.name): pathlib.Path(d, n).read_bytes()
                for d, _, names in os.walk(self._tmp.name) for n in names}

    def test_export_refuses_a_planted_host(self):
        self.assertEqual(self.run_batch(1, "--no-export"), 0, self.err)
        stream = glob.glob(os.path.join(self.batch_dir(), "0-*", "stream.jsonl"))[0]
        lines = read(stream).splitlines()
        lines[2] = lines[2].replace("Invalid token.", "Invalid token at fixture.example.test")
        pathlib.Path(stream).write_text("\n".join(lines) + "\n")
        before = self.snapshot()
        rc = self.bench("export", self.batch_dir(), "--endpoint", "http://127.0.0.1:%s" % self.phoenix_port,
                        FIXTURE_HOST="fixture.example.test")
        self.assertEqual(rc, 1, self.out)
        self.assertEqual(Fake.otlp_posts, [])
        self.assertIn("export: FOUND 0-", self.out)
        self.assertIn(": FIXTURE_HOST (1)", self.out)
        self.assertNotIn("fixture.example.test", self.out)
        self.assertEqual(self.snapshot(), before)

    def test_export_http_error_is_nonzero_and_writes_nothing(self):
        self.assertEqual(self.run_batch(1, "--no-export"), 0, self.err)
        before = self.snapshot()
        Fake.otlp_status = 415
        self.assertEqual(self.bench("export", self.batch_dir(), PHOENIX_PORT=self.phoenix_port), 1, self.out)
        self.assertIn("export: FAILED - Phoenix at http://127.0.0.1:%s/v1/traces: HTTP Error 415" % self.phoenix_port, self.out)
        self.assertEqual(self.snapshot(), before)

    def test_guard_refuses_the_demo_ports_and_accepts_phoenix(self):
        self.assertEqual(self.run_batch(1, "--no-export"), 0, self.err)
        b = self.batch_dir()
        for port in (self.port, "4317", "4318"):
            self.assertEqual(self.bench("export", b, "--endpoint", "http://127.0.0.1:%s" % port), 1, self.out)
            self.assertIn("export: REFUSED - endpoint port %s is" % port, self.out)
        self.assertEqual(self.bench("export", b, "--endpoint", "http://127.0.0.1:9999", JAEGER_UI_PORT="1234", OFREP_PORT="9999"), 1)
        self.assertIn("the demo's OFREP port; trajectories must never reach the store the agent reads. Point --endpoint at Phoenix", self.out)
        self.assertNotIn("of the demo", self.out)
        self.assertEqual(Fake.otlp_posts, [])
        self.assertEqual(self.bench("export", b, "--endpoint", "http://127.0.0.1:%s" % self.phoenix_port), 0, self.out)
        self.assertEqual(len(Fake.otlp_posts), 1)

    def test_run_exports_at_the_end_and_names_the_project(self):
        self.assertEqual(self.run_batch(1, PHOENIX_PORT=self.phoenix_port), 0, self.err)
        self.assertEqual(len(Fake.otlp_posts), 1)
        lines = self.err.splitlines()
        self.assertEqual(lines[-1], "Phoenix http://127.0.0.1:%s/projects/UHJvamVjdDoy   project t: one trace per trial" % self.phoenix_port)
        self.assertTrue(lines[-2].startswith("Records"), lines[-2])

    def test_phoenix_line_falls_back_to_the_base_url_when_the_lookup_fails(self):
        for projects in (None, [{"name": "other", "id": "x"}]):  # 404, then a project list without this one
            Fake.projects = projects
            shutil.rmtree(self.runs, ignore_errors=True)  # two runs in one UTC second would share a batch directory
            self.assertEqual(self.run_batch(1, PHOENIX_PORT=self.phoenix_port), 0, self.err)
            self.assertEqual(self.err.splitlines()[-1],
                             "Phoenix http://127.0.0.1:%s   project t: open the project, one trace per trial" % self.phoenix_port)

    def test_no_export_dry_run_and_aborted_run_send_nothing(self):
        self.assertEqual(self.run_batch(1, "--no-export", PHOENIX_PORT=self.phoenix_port), 0, self.err)
        self.assertNotIn("Phoenix", self.err)
        self.assertEqual(self.run_batch(1, "--dry-run", PHOENIX_PORT=self.phoenix_port), 0, self.err)
        self.assertEqual(self.run_batch(1, PHOENIX_PORT=self.phoenix_port, FAKE_IMAGE="other:1"), 1)  # pre-flight abort
        self.assertEqual(Fake.otlp_posts, [])

    def test_export_failure_leaves_the_exit_code_and_batch_line_alone(self):
        self.assertEqual(self.run_batch(1, "--no-export"), 0, self.err)
        plain = [l.split("batch-")[0] for l in self.err.splitlines() if l.startswith("Batch")]
        shutil.rmtree(self.runs)
        Fake.otlp_status = 415
        self.assertEqual(self.run_batch(1, PHOENIX_PORT=self.phoenix_port), 0, self.err)
        lines = self.err.splitlines()
        self.assertEqual([l.split("batch-")[0] for l in lines if l.startswith("Batch")], plain)
        self.assertIn("  WARN  not exported to Phoenix: Phoenix at http://127.0.0.1:%s/v1/traces: HTTP Error 415" % self.phoenix_port, self.err)
        self.assertIn("the trials are on disk in", self.err)
        self.assertIn("once fixed: python3 harness/bench.py export", self.err)
        self.assertLess(next(i for i, l in enumerate(lines) if l.startswith("Batch")), next(i for i, l in enumerate(lines) if "WARN  not exported" in l))

    def test_an_export_exception_is_a_warn_and_the_run_still_exits_zero(self):
        with mock.patch.object(bench, "export", side_effect=TypeError("bad span")):
            self.assertEqual(self.run_batch(1), 0, self.err)
        self.assertIn("  WARN  not exported to Phoenix: TypeError('bad span')", self.err)

    def test_a_non_http_reply_fails_the_export_with_one_line(self):
        self.assertEqual(self.run_batch(1, "--no-export"), 0, self.err)
        with mock.patch.object(bench.urllib.request, "urlopen", side_effect=http.client.BadStatusLine("junk")):
            self.assertEqual(self.bench("export", self.batch_dir(), PHOENIX_PORT=self.phoenix_port), 1, self.out)
        self.assertIn("export: FAILED - Phoenix at http://127.0.0.1:%s/v1/traces: junk" % self.phoenix_port, self.out)

    def test_phoenix_down_is_one_warn_line(self):
        self.assertEqual(self.run_batch(1), 0, self.err)  # PHOENIX_PORT=1: nothing listens
        warn = [l for l in self.err.splitlines() if "Phoenix" in l]
        self.assertEqual(len(warn), 1, warn)
        self.assertRegex(warn[0], r"^  WARN  Phoenix not running; start it with `make phoenix`, then: python3 harness/bench.py export \S+batch-")


class TestStoreCheck(BenchCase):
    def test_store_holding_exported_trajectories_aborts_before_the_flag(self):
        Fake.store_services = ["payment", "jaeger-mcp-evals"]
        self.assertEqual(self.run_batch(1), 1, self.err)
        self.assertIn("FAIL  no harness trajectories in Jaeger's store", self.err)
        self.assertIn("fix: Jaeger's store holds exported trajectories: restart the jaeger container on the fixture host to empty it, "
                      "and check bench.py export --endpoint points at Phoenix", self.err)
        self.assertIn("ABORT - stopped before any trial", self.err)
        self.assertEqual(self.flag_file(), self.pristine())

    def test_clean_store_passes_and_is_recorded(self):
        Fake.store_services = ["payment", "checkout"]
        self.assertEqual(self.run_batch(1, "--no-export"), 0, self.err)
        self.assertEqual(load(os.path.join(self.batch_dir(), "preflight.json"))["store_clean"], "PASS")

    def test_unreadable_store_is_a_warn_and_the_run_goes_on(self):
        Fake.store_services = "error"
        self.assertEqual(self.run_batch(1, "--no-export"), 0, self.err)
        self.assertIn("  WARN  could not list the services in Jaeger's store", self.err)
        self.assertIsNone(load(os.path.join(self.batch_dir(), "preflight.json"))["store_clean"])


class TestSoak(BenchCase):
    def test_one_sample_writes_log(self):
        self.assertEqual(self.bench("soak", "paymentFailure", "1", "0"), 0, self.err)
        logs = glob.glob(os.path.join(self.runs, "paymentFailure", "soak-*.log"))
        self.assertEqual(len(logs), 1)
        self.assertIn("payment_calls_180s=5", read(logs[0]))


class TestSandboxProbe(BenchCase):
    """Pre-flight's sandbox probe: the trial's own claude argv against a local 400 server."""
    SERVED = ["StructuredOutput"] + ["mcp__jaeger__" + n for n in
                                     ("get_critical_path", "get_trace_errors", "get_trace_topology", "get_services")]

    def probes(self):
        p = os.path.join(self.home, "probe-calls.jsonl")
        return bench.read_jsonl(p) if os.path.isfile(p) else []

    def test_pass_records_result_and_tool_names_and_runs_the_trial_argv(self):
        arms = {"noskill": arm("noskill"), "off": arm("noskill", tools=False)}
        self.assertEqual(self.run_batch(1, arms=arms, ANTHROPIC_API_KEY="sk-test"), 0, self.err)
        pre = load(os.path.join(self.batch_dir(), "preflight.json"))
        self.assertEqual(pre["sandbox_probe"], {"result": "PASS", "tools": {"noskill": self.SERVED, "off": ["StructuredOutput"]}})
        probes = {bool(p["mcp"]["mcpServers"]): p for p in self.probes()}
        url = "http://127.0.0.1:%s/jaeger/ui/api/ai/mcp/" % self.port
        self.assertEqual(probes[True]["mcp"], {"mcpServers": {"jaeger": {"type": "http", "url": url}}})
        trial = self.calls()[0]["argv"]  # both arms use prompt noskill: argvs differ only in --mcp-config
        self.assertEqual(probes[True]["argv"][-1], "--no-session-persistence")
        drop = lambda argv: [x for i, x in enumerate(argv) if i == 0 or argv[i - 1] != "--mcp-config"]
        self.assertEqual(drop(probes[True]["argv"][1:-1]), drop(trial[1:]))
        self.assertNotIn(self.home, probes[True]["cwd"])
        for root in (self.runs, self.records):
            for dirpath, _, files in os.walk(root):
                for f in files:
                    self.assertNotIn("probe-secret", read(os.path.join(dirpath, f)), f)

    def test_trial_probe_and_grader_get_the_client_env_and_it_is_not_in_argv(self):
        self.assertEqual(self.run_batch(1), 0, self.err)
        want = ["false", "1"]
        for seen in (self.calls(), self.probes(), bench.read_jsonl(os.path.join(self.home, "grader-calls.jsonl"))):
            self.assertTrue(seen)
            self.assertEqual({tuple(c["client_env"]) for c in seen}, {tuple(want)})
        self.assertNotIn("ENABLE_TOOL_SEARCH", json.dumps(load(glob.glob(os.path.join(self.batch_dir(), "0-*", "meta.json"))[0])["client_argv"]))

    def test_user_plugin_dirs_never_reach_the_trial_probe_or_grader(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_PLUGIN_DIRS": "/somewhere/my-plugin"}):
            self.assertEqual(self.run_batch(1), 0, self.err)
        for seen in (self.calls(), self.probes(), bench.read_jsonl(os.path.join(self.home, "grader-calls.jsonl"))):
            self.assertTrue(seen)
            self.assertEqual({c["plugin_dirs"] for c in seen}, {None})

    def test_failure_names_the_unexpected_tool_and_the_cli_version_in_the_fix(self):
        self.assertEqual(self.run_batch(1, FAKE_PROBE="extra_tool"), 1, self.err)
        self.assertIn("fix: claude CLI 2.1.282 sends a tool the harness does not expect (Bash). If it is new in this CLI version, "
                      "find the setting that turns it off and add it to grade.CLIENT_ENV, or run a CLI version the probe accepts", self.err)

    def test_failures_abort_before_the_flag(self):
        for mode, why in (("extra_tool", "tools sent"), ("server_tool", "server-side tool web_search"),
                          ("memory", "memory marker 'CLAUDE.md'"), ("silent", "no model request")):
            with self.subTest(mode=mode):
                self.assertEqual(self.run_batch(1, FAKE_PROBE=mode), 1, self.err)
                self.assertIn("sandbox probe", self.err)
                self.assertIn(why, self.err)
                self.assertNotIn("probe-secret", self.err)
                self.assertEqual(self.calls(), [])
                self.assertEqual(self.flag_file(), self.pristine())
                self.assertNotIn("python3 -", read(os.path.join(self.home, "ssh.log")))
                self.assertFalse(os.path.exists(self.runs))

    def write_recorded(self, rows):
        os.makedirs(os.path.join(self.records, "old", "batch-1"))
        with open(os.path.join(self.records, "old", "batch-1", "manifest.json"), "w") as f:  # as every record has
            json.dump({"client": "cli"}, f)
        with open(os.path.join(self.records, "old", "batch-1", "scores.jsonl"), "w") as f:
            f.writelines(json.dumps(dict({"scenario_used": "paymentFailure", "model_asserted": "sonnet"}, **r)) + "\n" for r in rows)

    def test_stage_one_names_the_cli_version_and_says_when_cost_is_unknown(self):
        self.assertEqual(self.run_batch(1, "--dry-run"), 0, self.err)
        self.assertIn("ok    claude CLI 2.1.282 accepts the flags the harness passes", self.err)
        self.assertIn("ok    expected cost unknown: no recorded cli trial of paymentFailure on sonnet", self.err)

    def test_expected_cost_is_per_arm_median_times_n(self):
        self.write_recorded([{"cost_usd": x} for x in (0.1, 0.2, 0.3)])
        self.assertEqual(bench.expected_cost("paymentFailure", "sonnet", 10, 2, records=self.records),
                         [(True, "expected cost about 2.00 USD per arm of 10 trials (recorded max 0.30 per trial, cap 2)")])
        # recorded cli trials say nothing about a goose run on the same model id
        self.assertEqual(bench.expected_cost("paymentFailure", "sonnet", 10, 2, records=self.records, client="goose"),
                         [(True, "expected cost unknown: no recorded goose trial of paymentFailure on sonnet")])
        os.remove(os.path.join(self.records, "old", "batch-1", "manifest.json"))  # no manifest: no known client, not read
        self.assertEqual(bench.expected_cost("paymentFailure", "sonnet", 10, 2, records=self.records),
                         [(True, "expected cost unknown: no recorded cli trial of paymentFailure on sonnet")])

    def test_expected_cost_from_recorded_trials_and_no_warning_when_far_below_the_cap(self):
        self.write_recorded([{"cost_usd": c, "num_turns": 10} for c in (0.1, 0.2, 0.3)] +
                            [{"cost_usd": 9, "num_turns": 30, "scenario_used": "other"}, {"cost_usd": 9, "model_asserted": "m2"}])
        self.assertEqual(self.run_batch(1, "--dry-run"), 0, self.err)
        self.assertRegex(self.err, r"ok    expected cost about 0\.\d\d USD per arm of \d+ trials \(recorded max 0\.30 per trial, cap 2\)")
        self.assertNotIn("cut off", self.err)
        self.assertNotIn("turns;", self.err)

    def test_warns_before_spending_when_a_recorded_trial_is_near_the_cap(self):
        self.write_recorded([{"cost_usd": 1.2, "num_turns": 25}, {"cost_usd": 0.4, "num_turns": 9}])
        self.assertEqual(self.run_batch(1, "--dry-run"), 0, self.err)
        self.assertIn("WARNING: a trial may be cut off by the per-trial cap and lose its verdict; raise run.max_budget_usd "
                      "or expect ERROR trials", self.err)
        self.assertNotIn("of 30 turns", self.err)

    def test_dry_run_probes_too(self):
        self.assertEqual(self.run_batch(1, "--dry-run"), 0, self.err)
        self.assertEqual(len(self.probes()), 2)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.run_batch(1, "--dry-run", FAKE_PROBE="extra_tool"), 1, self.err)

    def test_codex_is_not_probed(self):
        self.assertEqual(self.run_batch(1, run={"client": "codex", "model": "gpt-t"}), 0, self.err)
        self.assertIsNone(load(os.path.join(self.batch_dir(), "preflight.json"))["sandbox_probe"])
        self.assertEqual(self.probes(), [])

    def test_a_hung_cli_is_killed_at_the_timeout(self):
        t0 = bench.time.monotonic()
        problems, names = bench.sandbox_probe([sys.executable, "-c", "import time; time.sleep(60)"], dict(os.environ), [], timeout=1)
        self.assertLess(bench.time.monotonic() - t0, 10)
        self.assertEqual((names, len(problems)), ([], 1))
        self.assertIn("no model request", problems[0])

    def test_a_logged_out_cli_says_so_in_the_problem(self):
        code = "import sys; sys.stderr.write('Not logged in \\u00b7 Please run /login\\n'); sys.exit(1)"
        problems, names = bench.sandbox_probe([sys.executable, "-c", code], dict(os.environ), [], timeout=10)
        self.assertEqual(names, [])
        self.assertIn("claude said: Not logged in \u00b7 Please run /login", problems[0])

    def test_need_key_requires_an_x_api_key_header_and_reports_only_booleans(self):
        code = ("import os, urllib.request as u, urllib.error as e\n"
                "r = u.Request(os.environ['ANTHROPIC_BASE_URL'] + '/v1/messages', b'{}', {'authorization': 'Bearer SECRETTOKEN'}"
                " if os.environ.get('NOKEY') else {'x-api-key': 'SECRETKEY'})\n"
                "try: u.urlopen(r)\n"
                "except e.HTTPError: pass\n")
        ok, _ = bench.sandbox_probe([sys.executable, "-c", code], dict(os.environ), [], timeout=10, need_key=True)
        self.assertFalse([p for p in ok if "billing" in p])
        bad, _ = bench.sandbox_probe([sys.executable, "-c", code], dict(os.environ, NOKEY="1"), [], timeout=10, need_key=True)
        msg = [p for p in bad if "billing" in p]
        self.assertEqual(len(msg), 1)
        self.assertIn("authorization header present: True", msg[0])
        self.assertNotIn("SECRET", " ".join(bad))
        off, _ = bench.sandbox_probe([sys.executable, "-c", code], dict(os.environ, NOKEY="1"), [], timeout=10)
        self.assertFalse([p for p in off if "billing" in p])


if __name__ == "__main__":
    unittest.main()
