#!/usr/bin/env python3
"""Grade an agent's free-text mechanism against a scenario's mechanism_truth.

One pinned model (GRADER_MODEL) with one pinned prompt (grader-prompt.txt, by sha256) labels
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

HARNESS = os.path.dirname(os.path.abspath(__file__))
GRADER_MODEL = "claude-fable-5-1"
PROMPT = os.path.join(HARNESS, "grader-prompt.txt")
CLAUDE = "claude"
LABELS = ("correct", "incorrect", "unclear")
SCHEMA = {"type": "object", "properties": {"label": {"type": "string", "enum": list(LABELS)}, "reason": {"type": "string"}},
          "required": ["label", "reason"], "additionalProperties": False}
DROP_ENV = ("CLAUDECODE", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL")
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


def argv(truth, text, mcp_config):
    return [CLAUDE, "-p", user_prompt(truth, text), "--output-format", "json",
            "--mcp-config", mcp_config, "--strict-mcp-config", "--setting-sources", "", "--restricted", "--tools", "",
            "--no-session-persistence", "--system-prompt", prompt_text().rstrip("\n"),
            "--json-schema", json.dumps(SCHEMA), "--model", GRADER_MODEL, "--max-turns", "3"] + (["--verbose"] if API_KEY else [])


def _billing_error(msg):
    global BILLING_FAILED
    BILLING_FAILED = True
    return RuntimeError("grader: billing: " + msg)


def call_cli(truth, text):
    """One grader call; {"label", "reason"}. Raises RuntimeError on anything but a valid label."""
    if BILLING_FAILED:
        raise RuntimeError("grader: billing: an earlier call failed the apiKeySource check; no further grader calls")
    env = {k: v for k, v in os.environ.items() if k not in DROP_ENV}
    if API_KEY:
        env["ANTHROPIC_API_KEY"] = API_KEY
    with tempfile.TemporaryDirectory() as work:
        mcp = os.path.join(work, "mcp.json")
        with open(mcp, "w", encoding="utf-8") as f:
            f.write('{"mcpServers":{}}')
        p = subprocess.run(argv(truth, text, mcp), cwd=work, env=env, stdin=subprocess.DEVNULL,
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
        return {"label": ans["label"], "reason": str(ans["reason"])}
    except (ValueError, KeyError, TypeError, IndexError, StopIteration) as e:
        raise RuntimeError("grader: no valid label (exit %s): %r" % (p.returncode, e))


def grade(batch_dir, truth, text, call=True):
    """The cached label for (truth, text), else one call_cli (call=True) appended to the cache, else None.
    Returns {"label", "reason", "model", "prompt_sha256", "cached"}."""
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
