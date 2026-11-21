#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Grade an agent's free-text mechanism against a scenario's mechanism_truth.

One pinned model (setting GRADER_MODEL, default claude-fable-5-1; command GRADER_BIN, default claude) with one pinned prompt (grader-prompt.txt, by sha256) labels
the pair correct, incorrect or unclear through the Claude Code CLI, sandboxed like the agent:
no tools, no MCP servers, no settings, no session, a temp cwd, no API keys in the environment.
Every label is cached in <batch>/grades.jsonl, keyed by sha256 of (model, prompt sha256, truth,
text), so re-scoring reads the cache and never calls the model again.
"""
import hashlib
import json
import os
import subprocess
import tempfile

import config

HARNESS = os.path.dirname(os.path.abspath(__file__))
_CFG = config.load()
GRADER_MODEL = _CFG.get("GRADER_MODEL") or "claude-fable-5-1"  # settings, see fixture.env.example
PROMPT = os.path.join(HARNESS, "grader-prompt.txt")
CLAUDE = _CFG.get("GRADER_BIN") or "claude"
LABELS = ("correct", "incorrect", "unclear")
SCHEMA = {"type": "object", "properties": {"label": {"type": "string", "enum": list(LABELS)}, "reason": {"type": "string"}},
          "required": ["label", "reason"], "additionalProperties": False}
# CLAUDE_CODE_PLUGIN_DIRS: the user's own plugins load from it even with --setting-sources "".
DROP_ENV = ("CLAUDECODE", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL", "CLAUDE_CODE_PLUGIN_DIRS")
# Set on every Claude CLI call the harness makes (trials, sandbox probe, grader): no tool-search placeholder tool, no self-update.
CLIENT_ENV = {"ENABLE_TOOL_SEARCH": "false", "DISABLE_AUTOUPDATER": "1"}


def child_env(claude=True):
    """os.environ for a child process: DROP_ENV removed, CLIENT_ENV added when the child is the Claude CLI."""
    return dict({k: v for k, v in os.environ.items() if k not in DROP_ENV}, **(CLIENT_ENV if claude else {}))


# Set by bench.py for an experiment with run.bill_api_key: the grader bills this key instead of the
# logged-in plan, and a call whose init event reports another apiKeySource fails. Environment only.
API_KEY = None
BILLING_FAILED = False  # set by the first billing error: no later grader call may reach the CLI
TIMEOUT_S = 300


def prompt_text():
    with open(PROMPT, encoding="utf-8") as f:
        return f.read()


def prompt_sha():
    return hashlib.sha256(prompt_text().encode("utf-8")).hexdigest()


def cache_key(truth, text):
    return hashlib.sha256(json.dumps([GRADER_MODEL, prompt_sha(), truth, text]).encode("utf-8")).hexdigest()


def user_prompt(truth, text):
    return "True mechanism: %s\n\nAgent's explanation:\n\n%s\n" % (truth, "\n".join("> " + l for l in text.splitlines()))


def argv(truth, text, mcp_config, piped=False):
    """piped: the prompt goes on stdin (see call_cli), for prompts too long for one argument."""
    return [CLAUDE, "-p"] + ([] if piped else [user_prompt(truth, text)]) + ["--output-format", "json",
            "--mcp-config", mcp_config, "--strict-mcp-config", "--setting-sources", "", "--restricted", "--tools", "",
            "--no-session-persistence", "--system-prompt", prompt_text().rstrip("\n"),
            "--json-schema", json.dumps(SCHEMA), "--model", GRADER_MODEL, "--max-turns", "3"] + (["--verbose"] if API_KEY else [])


def _billing_error(msg):
    global BILLING_FAILED
    BILLING_FAILED = True
    return RuntimeError("grader: billing: " + msg)


ARG_MAX_BYTES = 100_000  # below the 131,072-byte per-argument limit


def call_cli(truth, text):
    """One grader call; {"label", "reason"}. Raises RuntimeError on anything but a valid label."""
    if BILLING_FAILED:
        raise RuntimeError("grader: billing: an earlier call failed the apiKeySource check; no further grader calls")
    env = child_env()
    if API_KEY:
        env["ANTHROPIC_API_KEY"] = API_KEY
    with tempfile.TemporaryDirectory() as work:
        mcp = os.path.join(work, "mcp.json")
        with open(mcp, "w", encoding="utf-8") as f:
            f.write('{"mcpServers":{}}')
        # Linux caps one argument at 128 KiB (MAX_ARG_STRLEN): a longer prompt goes to claude -p on stdin.
        user = user_prompt(truth, text)
        piped = len(user.encode("utf-8")) > ARG_MAX_BYTES
        p = subprocess.run(argv(truth, text, mcp, piped), cwd=work, env=env,
                           input=user if piped else None, stdin=None if piped else subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=TIMEOUT_S)
    try:
        out = json.loads(p.stdout)
        if isinstance(out, list):  # --verbose prints every event; the result is last
            init = next((e for e in out if isinstance(e, dict) and e.get("subtype") == "init"), {}) if API_KEY else {}
            if API_KEY and init.get("apiKeySource") != "ANTHROPIC_API_KEY":
                raise _billing_error("apiKeySource %r, not ANTHROPIC_API_KEY" % init.get("apiKeySource"))
            out = next(e for e in reversed(out) if isinstance(e, dict) and e.get("type") == "result")
        elif API_KEY:
            raise _billing_error("no init event to confirm apiKeySource")
        ans = out["structured_output"]
        if ans["label"] not in LABELS:
            raise ValueError(ans["label"])
        return {"label": ans["label"], "reason": str(ans["reason"]), "cost_usd": out.get("total_cost_usd")}
    except (ValueError, KeyError, TypeError, IndexError, StopIteration) as e:
        raise RuntimeError("grader: no valid label (exit %s): %r" % (p.returncode, e))


def grade(batch_dir, truth, text, call=True):
    """The cached label for (truth, text), else one call_cli (call=True) appended to the cache, else None.
    Returns {"label", "reason", "cost_usd", "model", "prompt_sha256", "cached"}; cost_usd is None on rows cached before 2026-11-17."""
    key, path = cache_key(truth, text), os.path.join(batch_dir, "grades.jsonl")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                row = json.loads(line) if line.strip() else {}
                if row.get("key") == key:
                    return dict(row, cached=True)
    if not call:
        return None
    row = dict(call_cli(truth, text), key=key, model=GRADER_MODEL, prompt_sha256=prompt_sha())
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    return dict(row, cached=False)
