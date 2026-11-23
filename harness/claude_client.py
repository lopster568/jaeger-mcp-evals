# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""The Claude Code CLI as an agent client (run.client claude): its argv, its pre-flight and the guards
only it supports (the sandbox probe, the model pin, the per-trial budget cap, plan-versus-key billing).
bench.py runs `claude -p` itself and reads its stdout as stream.jsonl, the event shape every client writes.
"""
import http.server
import json
import os
import pathlib
import re
import subprocess
import tempfile
import threading

import grade


def claude_argv(a, prompt, system, schema, mcp_config):
    """The Claude Code CLI call; bench.py passes the texts its leak scan read (rendered_texts)."""
    return ["claude", "-p", prompt,
            "--output-format", "stream-json", "--verbose",
            "--mcp-config", mcp_config, "--strict-mcp-config",
            "--setting-sources", "", "--restricted", "--tools", "",
            "--allowedTools", "mcp__jaeger__*",
            "--system-prompt", system,
            "--json-schema", schema,
            "--model", a.model, "--effort", a.effort, "--max-turns", str(a.max_turns), "--max-budget-usd", str(a.max_budget_usd)]


def redact_argv(argv, prompt, system, schema=None):
    """Replace the texts claude_argv inlines (sha256 in a record, a file name on screen); the schema only when given."""
    argv[2] = prompt
    argv[argv.index("--system-prompt") + 1] = system
    if schema:
        argv[argv.index("--json-schema") + 1] = schema


# The claude flags the harness passes that `claude --help` lists (--max-turns and --allowedTools are hidden there).
CLAUDE_FLAGS = ("--restricted", "--setting-sources", "--strict-mcp-config", "--json-schema", "--effort",
                "--tools", "--mcp-config", "--system-prompt", "--max-budget-usd")


def claude_missing_flags():
    """(version, flags the installed claude does not list), or None when claude cannot be run."""
    try:
        h = subprocess.run(["claude", "--help"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60).stdout
        v = subprocess.run(["claude", "--version"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return ((v.split() or ["unknown version"])[0], [f for f in CLAUDE_FLAGS if f not in h])


def is_full_model_id(m):
    return bool(re.fullmatch(r"claude-[a-z]+(?:-[a-z0-9]+)*", m) and re.search(r"\d", m))


def preflight(a, cfg, log):
    """The installed claude accepts the harness's flags, and billing is set as the experiment asks: None, or (message, fix)."""
    log.detail("== pre-flight: claude client (claude flags) ==")
    found = claude_missing_flags()
    if found is None:
        return ("claude is not on PATH",
                "install the Claude Code CLI and log in (run `claude` once), or pick another run.client")
    if found[1]:
        return ("ABORT - Claude Code CLI %s does not accept %s; nothing was touched" % (found[0], " ".join(found[1])),
                "run `claude update`")
    log.check(True, "claude CLI %s accepts the flags the harness passes" % found[0])
    if not is_full_model_id(a.model):
        log.sub("WARNING: run.model %r is an alias; the model the stream reports is recorded per trial, not pinned" % a.model)
    if a.bill_api_key:
        if not cfg.get("ANTHROPIC_API_KEY"):
            return ("run.bill_api_key is true but ANTHROPIC_API_KEY is empty in fixture.env; nothing was touched",
                    "set ANTHROPIC_API_KEY in fixture.env, or set run.bill_api_key to false in the experiment file")
        grade.API_KEY = cfg["ANTHROPIC_API_KEY"]  # environment only: never argv, never a record
        grade.BILLING_FAILED = False
        log.check(True, "agent and grader bill ANTHROPIC_API_KEY" if grade.CLIENT == "claude" else
                  "agent bills ANTHROPIC_API_KEY (the %s grader bills its provider's key)" % grade.CLIENT)
    elif cfg.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"):
        log.sub("ANTHROPIC_API_KEY is set; the claude client runs on the logged-in plan and ignores it")
    return None


def env(a, cfg):
    """No API key in the environment, so claude bills the logged-in plan, unless run.bill_api_key."""
    e = grade.child_env()
    if a.bill_api_key:
        e.update({"ANTHROPIC_API_KEY": cfg.get("ANTHROPIC_API_KEY", "")})  # environment only: never argv, never a record
    return e


MEMORY_MARKERS = ("CLAUDE.md", "# CLAUDE", "auto-memory", "MEMORY.md")
PROBE_TIMEOUT_S = 90


def sandbox_probe(argv, env, expected, timeout=PROBE_TIMEOUT_S, need_key=False):
    """Run a trial's CLI argv against a server on 127.0.0.1 that keeps the first POST body in memory
    and answers 400, so no model is called. Returns (problems, tool names sent). The body is never
    written anywhere: with a claude.ai login it carries the account's email. Of the headers only two
    booleans are kept (x-api-key and authorization present); need_key fails the probe without x-api-key."""
    first, hdrs = [], {}

    class Probe(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if not first:
                first.append(body)
                hdrs.update(key=self.headers.get("x-api-key") is not None, auth=self.headers.get("authorization") is not None)
            reply = b'{"type":"error","error":{"type":"invalid_request_error","message":"sandbox probe"}}'
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Probe)
    threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True).start()
    said = ""
    try:
        with tempfile.TemporaryDirectory() as work:
            try:
                r = subprocess.run(argv + ["--no-session-persistence"], cwd=work, stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, errors="replace", timeout=timeout,
                                   env=dict(env, ANTHROPIC_BASE_URL="http://127.0.0.1:%d" % srv.server_address[1]))
                lines = [l.strip() for l in (r.stderr + r.stdout).splitlines() if l.strip()]
                said = lines[-1][:200] if lines else ""  # only the CLI's last line is quoted, never the rest
            except subprocess.TimeoutExpired:
                pass  # subprocess.run has killed it; judge whatever request arrived
            except OSError as e:
                return ["could not start %s: %s" % (argv[0], e)], []
    finally:
        srv.shutdown()
        srv.server_close()
    try:
        req = json.loads(first[0])
        sent = req.get("tools") or []
        names = [str(t.get("name")) for t in sent]
    except (IndexError, ValueError, AttributeError):
        return ["no model request with a JSON body reached the probe within %ds%s" % (timeout, "; claude said: " + said if said else "")], []
    problems = [] if sorted(names) == sorted(expected) else ["tools sent %s, expected %s" % (sorted(names), sorted(expected))]
    problems += ["server-side tool %s (type %s)" % (t.get("name"), t["type"]) for t in sent if "type" in t]
    text = json.dumps([req.get("system"), req.get("messages")], ensure_ascii=False)
    if need_key and not hdrs.get("key"):
        problems.append("billing: no x-api-key header reached the probe (authorization header present: %s)" % hdrs.get("auth"))
    problems += ["memory marker %r in the request" % m for m in MEMORY_MARKERS if m in text]
    return problems, names


def probe_fix(version, unexpected):
    """The way out of a failed sandbox probe; version is the `claude --version` output."""
    if unexpected:
        return ("claude CLI %s sends a tool the harness does not expect (%s). If it is new in this CLI version, find the "
                "setting that turns it off and add it to grade.CLIENT_ENV, or run a CLI version the probe accepts"
                % (((version or "").split() or ["unknown version"])[0], ", ".join(sorted(unexpected))))
    return "read the sandbox probe lines above; the CLI's request failed the check named there"


def api_key_source(stream_path):
    """The stream init event's apiKeySource ("ANTHROPIC_API_KEY" when the key billed the trial), else None."""
    for line in pathlib.Path(stream_path).read_text(encoding="utf-8", errors="replace").splitlines() if os.path.isfile(stream_path) else []:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("type") == "system" and e.get("subtype") == "init":
            return e.get("apiKeySource")
    return None
