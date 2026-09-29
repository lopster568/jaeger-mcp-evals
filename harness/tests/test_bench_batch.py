#!/usr/bin/env python3
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
import http.server
import io
import json
import os
import pathlib
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import types
import unittest
from unittest import mock

HARNESS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HARNESS_DIR)
import bench  # noqa: E402

FLAGS = {"flags": {
    "paymentFailure": {"state": "ENABLED", "variants": {"100%": 1, "off": 0}, "defaultVariant": "off"},
    "adFailure": {"state": "ENABLED", "variants": {"on": True, "off": False}, "defaultVariant": "off"},
}}
DESC_CHANGE = json.loads(pathlib.Path(HARNESS_DIR, "experiments", "descriptions", "desc-change.json").read_text())
VARIANT_IMAGE = "jaeger-mcp-evals/jaeger:desc-change-sep28-4c355981"
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
                            "api_key": "ANTHROPIC_API_KEY" in os.environ}) + "\\n")
    print(json.dumps({"type": "result", "subtype": "success", "result": "",
                      "structured_output": {"label": os.environ.get("FAKE_GRADE", "correct"), "reason": "fake"}}))
    sys.exit(0)
if os.environ.get("ANTHROPIC_BASE_URL"):  # bench.py's sandbox probe: POST one canned request, as the CLI would
    mcp = json.load(open(sys.argv[sys.argv.index("--mcp-config") + 1]))
    names = ["StructuredOutput"] + (["mcp__jaeger__" + n for n in ("get_critical_path", "get_trace_errors",
                                     "get_trace_topology", "get_services")] if mcp["mcpServers"] else [])
    tools, system, mode = [{"name": n, "input_schema": {}} for n in names], [{"type": "text", "text": "sys"}], os.environ.get("FAKE_PROBE")
    if mode == "extra_tool":
        tools.append({"name": "Bash", "input_schema": {}})
    if mode == "server_tool":
        tools.append({"type": "web_search_20250305", "name": "web_search"})
    if mode == "memory":
        system.append({"type": "text", "text": "Contents of /x/CLAUDE.md (project instructions)"})
    with open(os.path.join(os.environ["FAKE_HOME"], "probe-calls.jsonl"), "a") as f:
        f.write(json.dumps({"argv": sys.argv, "mcp": mcp, "cwd": os.getcwd()}) + "\\n")
    body = {"model": "m", "system": system, "tools": tools,
            "messages": [{"role": "user", "content": "userEmail probe-secret@example.com"}]}
    if mode != "silent":
        try:
            urllib.request.urlopen(urllib.request.Request(os.environ["ANTHROPIC_BASE_URL"] + "/v1/messages?beta=true",
                                                          json.dumps(body).encode(), {"Content-Type": "application/json"}))
        except urllib.error.HTTPError:
            sys.exit(1)
    sys.exit(0)
with open(os.path.join(os.environ["FAKE_HOME"], "claude-calls.jsonl"), "a") as f:
    f.write(json.dumps({"argv": sys.argv, "claudecode": "CLAUDECODE" in os.environ,
                        "api_key": any(k in os.environ for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL")), "cwd_listing": os.listdir(".")}) + "\\n")
verdict = {"root_cause_service": "payment", "root_cause_operation": "charge", "mechanism": "invalid token", "cascading": [{"service": "checkout"}], "confidence": "high",
           "evidence_span_ids": [], "abstain": False}
if os.environ.get("FAKE_JUNK"):
    print(json.dumps("not an event"))
for e in [
    {"type": "system", "subtype": "init", "model": "claude-sonnet-5", "claude_code_version": "2.1.282",
     "tools": ["StructuredOutput"] + ["mcp__jaeger__" + n for n in ("get_critical_path", "get_trace_errors", "get_trace_topology", "get_services")],
     "mcp_servers": [{"name": "jaeger", "status": "connected"}], "skills": ["s1"], "agents": ["a1"]},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "mcp__jaeger__get_trace_errors", "input": {}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "Payment request failed. Invalid token."}]}},
    {"type": "result", "subtype": os.environ.get("FAKE_STOP", "success"), "num_turns": 2, "total_cost_usd": 0.01, "duration_ms": 1000,
     "usage": {"input_tokens": 10, "output_tokens": 20}, "result": "done", "structured_output": verdict},
]:
    print(json.dumps(e))
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


class Fake(http.server.BaseHTTPRequestHandler):
    home = None
    served_descriptions = {}
    oracle_signal = True
    oracle_malformed = False
    mcp_calls = []
    otlp_posts, otlp_status = [], 200

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
        if self.path.startswith("/jaeger/ui/api/services"):
            return self.reply({"data": ["payment", "checkout"]})
        if self.path.startswith("/jaeger/ui/api/traces"):
            fault = self.flags()["paymentFailure"]["defaultVariant"] != "off"
            return self.reply({"data": [trace(fault) for _ in range(5)]})
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
        if msg.get("method") == "tools/call":
            if Fake.oracle_malformed:
                return self.reply({"jsonrpc": "2.0", "id": msg["id"], "result": "not-a-dict"})
            fault = self.flags()["paymentFailure"]["defaultVariant"] != "off" and Fake.oracle_signal
            doc = {"traces": [{"trace_id": "aa11"}, {"trace_id": "bb22"}]} if msg["params"]["name"] == "search_traces" else trace(fault)
            return self.reply({"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": json.dumps(doc)}]}})
        if msg.get("method") == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return
        if msg.get("method") == "initialize":
            return self.reply({"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": "2025-06-18"}},
                              [("Mcp-Session-Id", "s1")])
        served = [{"name": n, "description": Fake.served_descriptions.get(n, "stock wording"), "inputSchema": {}}
                  for n in ("get_critical_path", "get_trace_errors", "get_trace_topology", "get_services")]
        self.reply({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": served}})


class BenchCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        cls.port = str(cls.server.server_address[1])
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

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
        Fake.oracle_signal, Fake.oracle_malformed = True, False
        Fake.otlp_posts, Fake.otlp_status = [], 200
        self.env = {"PATH": binp + os.pathsep + os.environ["PATH"], "FAKE_HOME": self.home, "FAKE_IMAGE": STOCK_IMAGE,
                    "FIXTURE_HOST": "127.0.0.1", "FIXTURE_SSH_USER": "tester", "FIXTURE_DEMO_DIR": "otel-demo-3.0.0",
                    "JAEGER_UI_PORT": self.port, "OFREP_PORT": self.port, "JAEGER_BASE_PATH": "/jaeger/ui",
                    "RUNS_DIR": self.runs, "MIN_CONTAINERS": "19", "KNOWN_COMMITS": "2.20.0=abc123",
                    "CLAUDECODE": "1", "CODEX_BIN": "codex"}

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
        for line in ("ok    scenario paymentFailure ready", "[1/2] ", "tools used: get_trace_errors 1", "Records "):
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

    def test_api_client_runs_the_owned_loop(self):
        verdict = {"root_cause_service": "payment", "root_cause_operation": "charge", "mechanism": "invalid token", "cascading": [], "confidence": "high", "evidence_span_ids": [], "abstain": False}
        usage = {"input_tokens": 10, "output_tokens": 20}
        script = os.path.join(self.home, "script.json")
        pathlib.Path(script).write_text(json.dumps({"turns": [
            {"content": [{"type": "tool_use", "id": "t1", "name": "mcp__jaeger__get_trace_errors", "input": {}}],
             "stop_reason": "tool_use", "usage": usage},
            {"content": [{"type": "text", "text": json.dumps(verdict)}], "stop_reason": "end_turn", "usage": usage}]}))
        real = bench.client_argv
        key = "sk-test-9f3a-must-never-reach-a-record"
        fake_loop = os.path.join(HARNESS_DIR, "tests", "fake_loop.py")
        # The real api argv, with the loop swapped for the same loop on a scripted provider.
        self.assertEqual(real(types.SimpleNamespace(client="api", provider="anthropic", scenario="paymentFailure", model="m",
                                                    effort="high", max_turns=1, max_budget_usd="1"), "a", "p", "c", "t")[:2],
                         [sys.executable, bench.AGENT_LOOP])
        real_probe = bench.probe_argv
        with mock.patch.object(bench, "client_argv", lambda *a: [sys.executable, fake_loop, script] + real(*a)[2:]), \
                mock.patch.object(bench, "probe_argv", lambda a: [sys.executable, fake_loop, script] + real_probe(a)[2:]):
            # Without a key the api client (the default) is refused in pre-flight, before any batch directory exists.
            api = {"client": "api", "provider": "anthropic", "model": "claude-sonnet-5"}
            self.assertEqual(self.run_batch(1, run=api, ANTHROPIC_API_KEY=""), 1)
            self.assertIn("bench: set ANTHROPIC_API_KEY in fixture.env (or set run.client to cli to use the Claude Code CLI)\n",
                          self.err + self.out)
            self.assertEqual(glob.glob(os.path.join(self.runs, "paymentFailure", "batch-*")), [])
            self.assertEqual(self.run_batch(1, run=api, ANTHROPIC_API_KEY=key), 0, self.err)
        self.assertEqual(self.calls(), [])  # claude never ran

        leaks = [p for p in glob.glob(os.path.join(self.runs, "**"), recursive=True)
                 if os.path.isfile(p) and key in read(p)]
        self.assertEqual(leaks, [])
        b = self.batch_dir()
        m = load(os.path.join(b, "manifest.json"))
        self.assertEqual((m["client"], m["provider"], m["model_requested"], m["schema_version"]),
                         ("api", "anthropic", "claude-sonnet-5", 5))
        self.assertIn("providers/anthropic_provider.py", m["file_hashes_sha256"])
        self.assertEqual(load(os.path.join(b, "preflight.json"))["client"], "PASS")
        self.assertEqual([s["verdict"] for s in bench.read_jsonl(os.path.join(b, "scores.jsonl"))], ["PASS", "PASS"])
        for c in bench.read_jsonl(os.path.join(b, "cells.jsonl")):
            self.assertFalse(c["failed"])
            meta = load(os.path.join(b, "%d-%s" % (c["order_index"], c["arm"]), "meta.json"))
            self.assertEqual(list(meta), META_V4_KEYS)
            self.assertEqual((meta["schema_version"], meta["client"], meta["model_requested"]), (5, "api", "claude-sonnet-5"))
            # recorded without absolute paths: repo-relative, trial-relative, else the basename
            self.assertEqual(meta["client_argv"][:3], [os.path.basename(sys.executable), "harness/tests/fake_loop.py", "script.json"])
            argv = meta["client_argv"]
            self.assertEqual([argv[argv.index(k) + 1] for k in ("--prompt-file", "--scenario-file", "--mcp-config", "--out-dir")],
                             ["harness/prompts/%s.txt" % c["arm"], "harness/scenarios/paymentFailure.json", "mcp.json", "."])
            self.assertEqual(meta["history_policy"], {"harness_truncation": "none", "cli_autocompact": "n/a"})
            self.assertEqual(meta["client_version_pre"], "agent_loop 0.1.0")
            self.assertEqual(meta["observed"]["client_version"], "0.1.0")
            self.assertEqual((meta["agent_loop"]["effort"], meta["agent_loop"]["result_subtype"]), ("xhigh", "success"))

    def test_openai_provider_records_provider_and_base_url_never_the_key(self):
        verdict = {"root_cause_service": "payment", "root_cause_operation": "charge", "mechanism": "invalid token", "cascading": [], "confidence": "high", "evidence_span_ids": [], "abstain": False}
        seen, wrong = [], []

        class Chat(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append((self.path, self.headers["Authorization"]))
                msg = ({"role": "assistant", "content": json.dumps(verdict)} if body["messages"][-1]["role"] == "tool" else
                       {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
                           "name": "mcp__jaeger__get_trace_errors", "arguments": "{}"}}]})
                out = json.dumps({"id": "x", "model": "other-model" if wrong else body["model"], "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                                  "choices": [{"message": msg, "finish_reason": "stop" if msg["content"] else "tool_calls"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Chat)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base, key = "http://127.0.0.1:%d/v1" % srv.server_address[1], "sk-openai-must-never-reach-a-record"
        openai = {"client": "api", "provider": "openai", "model": "vendor/model-x"}
        self.assertEqual(self.run_batch(1, run=openai, OPENAI_API_KEY="", OPENAI_BASE_URL=base), 1)
        self.assertIn("bench: set OPENAI_API_KEY and OPENAI_BASE_URL in fixture.env for provider openai\n", self.err + self.out)
        wrong.append(1)  # the endpoint answers as another model: refused before any batch directory exists
        self.assertEqual(self.run_batch(1, run=openai, OPENAI_API_KEY=key, OPENAI_BASE_URL=base), 1)
        self.assertIn("run.model is vendor/model-x but the endpoint answered as other-model", self.err + self.out)
        self.assertEqual(glob.glob(os.path.join(self.runs, "paymentFailure", "batch-*")), [])
        wrong.clear()
        self.assertEqual(self.run_batch(1, run=openai, OPENAI_API_KEY=key, OPENAI_BASE_URL=base, ANTHROPIC_API_KEY=""), 0, self.err)
        self.assertEqual({s for s in seen}, {("/v1/chat/completions", "Bearer " + key)})
        leaks = [p for p in glob.glob(os.path.join(self.runs, "**"), recursive=True) if os.path.isfile(p) and key in read(p)]
        self.assertEqual(leaks, [])
        b = self.batch_dir()
        m = load(os.path.join(b, "manifest.json"))
        self.assertEqual((m["client"], m["provider"], m["model_requested"]), ("api", "openai", "vendor/model-x"))
        self.assertEqual(list(m).index("provider"), list(m).index("client") + 1)
        self.assertEqual([s["verdict"] for s in bench.read_jsonl(os.path.join(b, "scores.jsonl"))], ["PASS", "PASS"])
        for c in bench.read_jsonl(os.path.join(b, "cells.jsonl")):
            meta = load(os.path.join(b, "%d-%s" % (c["order_index"], c["arm"]), "meta.json"))
            self.assertEqual(meta["provider"], "openai")
            self.assertEqual((meta["agent_loop"]["provider"], meta["agent_loop"]["api_base_url"]), ("openai", "set"))

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

    def test_summary_counts_errors_and_stops(self):
        self.assertEqual(self.run_batch(1, FAKE_STOP="error_max_turns"), 0, self.err)
        blog = read(os.path.join(self.batch_dir(), "batch.log"))
        self.assertIn("noskill: n=1 PASS=1 PARTIAL=0 FAIL=0 ABSTAIN=0 ERROR=0 INVALID=0 LEAK=0 stops=error_max_turns:1 ", blog)
        self.assertIn("ERROR=0 INVALID=0 LEAK=0 stops=error_max_turns:1 read_skill_attempted", blog)
        self.assertIn("stops error_max_turns:1", self.err)
        shutil.rmtree(self.runs)
        self.assertEqual(self.run_batch(1, FAKE_JUNK="1"), 0, self.err)
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

    def test_unconfirmed_restore_exits_5(self):
        self.assertEqual(self.run_batch(1, FAKE_FAIL_RESTORE="1"), 5, self.err)
        r = load(os.path.join(self.batch_dir(), "restore.json"))
        self.assertFalse(r["cp_ok"] or r["default_confirmed"])

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
        self.assertIn("only 3 running containers", self.err)
        self.assertEqual(glob.glob(os.path.join(self.runs, "*", "batch-*")), [])
        self.assertEqual(self.calls(), [])

    def test_failed_check_shows_its_detail_on_screen(self):
        self.assertEqual(self.run_batch(1, FIXTURE_DEMO_DIR="no-such-dir"), 1)
        self.assertIn("FAIL  fixture reachable", self.err)
        self.assertIn("pre-flight: FAIL - fixture unreachable or no demo dir", self.err)  # a detail line
        self.assertIn("bench: ABORT - pre-flight failed", self.err)
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
        self.assertIn("  ok    fixture reachable", read(os.path.join(b, "batch.log")))

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


class TestExperiment(BenchCase):
    """The shipped desc-change-sep28.json, cut to one trial per arm."""

    def shipped(self):
        doc = load(os.path.join(HARNESS_DIR, "experiments", "desc-change-sep28.json"))
        doc["run"]["n_per_arm"] = 1
        path = os.path.join(self._tmp.name, "desc-change-sep28.json")
        pathlib.Path(path).write_text(json.dumps(doc))
        return path

    def test_variant_arm_runs_with_matching_descriptions(self):
        Fake.served_descriptions = {k: v for k, v in DESC_CHANGE.items() if k != "status"}
        rc = self.bench("run", self.shipped(), FAKE_IMAGE=VARIANT_IMAGE)
        self.assertEqual(rc, 0, self.err)
        b = self.batch_dir()
        m = load(os.path.join(b, "manifest.json"))
        self.assertEqual(m["arms"], ["descchange"])
        self.assertEqual(m["arm_pin"], {"arm": "descchange", "expected_image": VARIANT_IMAGE, "sut_jaeger_image_at_run": VARIANT_IMAGE})
        self.assertEqual(m["tools_description_check"]["mode"], "compared")
        self.assertEqual(m["experiment"]["name"], "desc-change-sep28")
        self.assertEqual(m["jaeger_commit"], "4c355981415835b062d6137573e0ddd2eebe6773")
        trial = os.path.join(b, "0-descchange")
        meta = load(os.path.join(trial, "meta.json"))
        self.assertEqual((meta["arm"], meta["prompt_name"], meta["model_requested"]), ("descchange", "noskill", "sonnet"))
        self.assertEqual(meta["tool_descriptions_check"], "compared")
        self.assertEqual(meta["tool_descriptions_file"], "harness/experiments/descriptions/desc-change.json")
        self.assertEqual(meta["experiment"]["name"], "desc-change-sep28")
        self.assertEqual(meta["tools_list_sha256"], bench.sha256_file(os.path.join(b, "tools.json")))
        self.assertEqual(bench.read_jsonl(os.path.join(b, "cells.jsonl"))[0]["arm"], "descchange")

    def test_baseline_arm_is_recorded_only(self):
        self.assertEqual(self.bench("run", self.shipped()), 0, self.err)
        meta = load(os.path.join(self.batch_dir(), "0-baseline", "meta.json"))
        self.assertEqual(meta["tool_descriptions_check"], "recorded_only")

    def test_description_mismatch_aborts(self):
        rc = self.bench("run", self.shipped(), FAKE_IMAGE=VARIANT_IMAGE)
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
        self.assertEqual(self.flag_file(), self.pristine())
        self.assertFalse(os.path.exists(os.path.join(self.home, "claude-calls.jsonl")))

    def test_image_pin_refuses_a_mismatch(self):
        rc = self.bench("run", self.shipped(), FAKE_IMAGE="other/jaeger:1")
        self.assertEqual(rc, 1)
        self.assertIn("which no arm of experiment desc-change-sep28 expects", self.err)
        self.assertEqual(self.calls(), [])
        self.assertEqual(glob.glob(os.path.join(self.runs, "*", "batch-*")), [])
        self.assertFalse(os.path.exists(self.records))


class TestSharedImage(BenchCase):
    """Two arms on one image: the non-baseline arm serves the baseline's wording,
    so the desc-change.json check must not apply."""

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

    def test_provider_only_for_api(self):
        self.refused(lambda d: d["run"].update(provider="openai"), "run.provider applies only to client api")

    def test_draft_refused(self):
        self.refused(lambda d: d.update(status="DRAFT"), "DRAFT")

    def test_client_version_must_be_a_non_empty_string(self):
        self.refused(lambda d: d["run"].update(client_version=""), "run.client_version must be a non-empty string")

    def test_client_version_mismatch_refuses_before_the_fixture(self):
        self.refused(lambda d: d["run"].update(client_version="2.1.283"),
                     "run.client_version is 2.1.283 but the cli client reports 2.1.282")
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
        host = "127.0.0.9"  # a loopback alias, distinct from the class-shared 127.0.0.1 server
        srv = http.server.ThreadingHTTPServer((host, 0), Fake)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        port = str(srv.server_address[1])
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
        self.assertEqual(self.run_batch(1), 1, self.err)
        self.assertEqual(self.calls(), [])
        self.assertEqual(load(os.path.join(self.batch_dir(), "preflight.json"))["oracle"], "FAIL")
        self.assertTrue(load(os.path.join(self.batch_dir(), "restore.json"))["default_confirmed"])

    def test_shipped_scenarios_have_an_oracle(self):
        for f in glob.glob(os.path.join(HARNESS_DIR, "scenarios", "*.json")):
            calls = load(f)["oracle"]
            self.assertTrue(calls and all(isinstance(c.get("tool"), str) and isinstance(c.get("arguments"), dict) for c in calls), f)

    def test_batch_aborts_and_restores_when_oracle_fails(self):
        Fake.oracle_signal = False
        self.assertEqual(self.run_batch(1), 1, self.err)
        self.assertIn("oracle", self.err)
        self.assertEqual(self.calls(), [])
        self.assertTrue(load(os.path.join(self.batch_dir(), "restore.json"))["default_confirmed"])
        self.assertEqual(self.flag_file(), self.pristine())
        self.assertEqual(load(os.path.join(self.batch_dir(), "preflight.json"))["oracle"], "FAIL")


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


class TestExport(BenchCase):
    def spans(self, body):
        rs = pb(pb(body)[1][0])
        resource = pb_attrs(pb(rs[1][0])[1])
        return resource, [dict(pb(s), attrs=pb_attrs(pb(s).get(9, []))) for s in pb(rs[2][0])[2]]

    def test_export_posts_one_trace_per_trial_with_run_attributes(self):
        self.assertEqual(self.run_batch(1), 0, self.err)
        b = self.batch_dir()
        self.assertEqual(self.bench("export", b, PHOENIX_PORT=self.port), 0, self.out)  # default endpoint from fixture.env
        self.assertEqual(self.bench("export", b, "--endpoint", "http://127.0.0.1:%s/" % self.port), 0, self.out)
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
        self.assertEqual(self.run_batch(1), 0, self.err)
        stream = glob.glob(os.path.join(self.batch_dir(), "0-*", "stream.jsonl"))[0]
        lines = read(stream).splitlines()
        lines[2] = lines[2].replace("Invalid token.", "Invalid token at fixture.example.test")
        pathlib.Path(stream).write_text("\n".join(lines) + "\n")
        before = self.snapshot()
        rc = self.bench("export", self.batch_dir(), "--endpoint", "http://127.0.0.1:%s" % self.port,
                        FIXTURE_HOST="fixture.example.test")
        self.assertEqual(rc, 1, self.out)
        self.assertEqual(Fake.otlp_posts, [])
        self.assertIn("export: FOUND 0-", self.out)
        self.assertIn(": FIXTURE_HOST (1)", self.out)
        self.assertNotIn("fixture.example.test", self.out)
        self.assertEqual(self.snapshot(), before)

    def test_export_http_error_is_nonzero_and_writes_nothing(self):
        self.assertEqual(self.run_batch(1), 0, self.err)
        before = self.snapshot()
        Fake.otlp_status = 415
        self.assertEqual(self.bench("export", self.batch_dir(), PHOENIX_PORT=self.port), 1, self.out)
        self.assertIn("export: FAILED - Phoenix at http://127.0.0.1:%s/v1/traces: HTTP Error 415" % self.port, self.out)
        self.assertEqual(self.snapshot(), before)


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

    def test_dry_run_probes_too(self):
        self.assertEqual(self.run_batch(1, "--dry-run"), 0, self.err)
        self.assertEqual(len(self.probes()), 2)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.run_batch(1, "--dry-run", FAKE_PROBE="extra_tool"), 1, self.err)

    def test_api_and_codex_are_not_probed(self):
        self.assertEqual(self.run_batch(1, run={"client": "codex", "model": "gpt-t"}), 0, self.err)
        self.assertIsNone(load(os.path.join(self.batch_dir(), "preflight.json"))["sandbox_probe"])
        self.assertEqual(self.probes(), [])

    def test_a_hung_cli_is_killed_at_the_timeout(self):
        t0 = bench.time.monotonic()
        problems, names = bench.sandbox_probe([sys.executable, "-c", "import time; time.sleep(60)"], dict(os.environ), [], timeout=1)
        self.assertLess(bench.time.monotonic() - t0, 10)
        self.assertEqual((names, len(problems)), ([], 1))
        self.assertIn("no model request", problems[0])


if __name__ == "__main__":
    unittest.main()
