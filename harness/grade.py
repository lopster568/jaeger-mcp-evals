#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Grade an agent's free-text mechanism against a scenario's mechanism_truth.

One pinned model with one pinned prompt (grader-prompt.txt, by sha256) labels the pair correct,
incorrect or unclear, through Goose in the pinned image (GRADER_CLIENT goose; GRADER_PROVIDER and
GRADER_MODEL, falling back to GOOSE_PROVIDER and GOOSE_MODEL, whose default in fixture.env.example is
nvidia/nemotron-3.5-lightning:free) or through the Claude Code CLI (GRADER_CLIENT claude, read from "cli" in old records;
GRADER_MODEL default claude-fable-5-1, command GRADER_BIN default claude). GRADER_CLIENT empty, the default: the grader
follows the agent, claude for a claude agent, else goose. Both are sandboxed like the agent: no tools, no MCP servers, no session,
a temp cwd, the provider key by name only. Every label is cached in <batch>/grades.jsonl, keyed by
sha256 of the grader's identity, prompt sha256, truth and text, so re-scoring reads the cache and
never calls the model again. A re-score looks up the grader the batch's manifest recorded
(batch_grader), not the current setting, so a changed default never leaves an old batch ungraded.
"""
import hashlib
import json
import os
import subprocess
import tempfile

import config
import goose_client

HARNESS = os.path.dirname(os.path.abspath(__file__))
_CFG = config.load()

VALIDATED = ("claude", "claude-fable-5-1")  # the one grader checked against human labels (docs/TUNING.md, Mechanism grader)


def settings(cfg, agent=None):
    """(client, provider, model) from fixture.env's settings; the goose model falls back to GOOSE_MODEL (empty only
    when that is). With the agent's client the grader follows it when GRADER_CLIENT is empty (config.grader_client)."""
    client = config.grader_client(cfg, agent) if agent else config.client_name(cfg.get("GRADER_CLIENT") or "goose")
    return (client, cfg.get("GRADER_PROVIDER") or cfg.get("GOOSE_PROVIDER") or "",
            cfg.get("GRADER_MODEL") or (VALIDATED[1] if client == "claude" else cfg.get("GOOSE_MODEL") or ""))


CLIENT, PROVIDER, GRADER_MODEL = settings(_CFG)  # see fixture.env.example
WARNING = "The grader decides every mechanism score: use the agent's model or a stronger reasoning model."
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


class NoLabel(RuntimeError):
    """The grader answered but gave no valid label: the one failure grade() asks again for."""


def prompt_text():
    with open(PROMPT, encoding="utf-8") as f:
        return f.read()


def prompt_sha():
    return hashlib.sha256(prompt_text().encode("utf-8")).hexdigest()


def current():
    """The grader identity of the current setting."""
    return {"client": CLIENT, "provider": None if CLIENT == "claude" else PROVIDER, "model": GRADER_MODEL}


def batch_grader(batch_dir):
    """The grader identity the batch's manifest recorded; a manifest without a client is a claude batch (written before
    GRADER_CLIENT; "cli" is read as claude). None when the batch has no manifest or its manifest names no grader model."""
    try:
        with open(os.path.join(batch_dir, "manifest.json"), encoding="utf-8") as f:
            g = json.load(f).get("grader") or {}
    except (OSError, ValueError):
        return None
    if not g.get("model"):
        return None
    return {"client": config.recorded_client(g.get("client")), "provider": g.get("provider"), "model": g.get("model")}


def cache_key(truth, text, who=None):
    """claude (once cli) keys are unchanged since before GRADER_CLIENT; a goose key also holds client and provider."""
    who = who or current()
    head = [who["model"]] if config.client_name(who["client"]) == "claude" else [who["client"], who["provider"], who["model"]]
    return hashlib.sha256(json.dumps(head + [prompt_sha(), truth, text]).encode("utf-8")).hexdigest()


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
        raise NoLabel("grader: no valid label (exit %s): %r" % (p.returncode, e))


def call_goose(truth, text):
    """One grader call through goose_client's container, no extensions. The untrusted answer is not recipe text: Goose
    renders a recipe as a template, so it travels as a file parameter, substituted once (JSON-escaped into the
    recipe's prompt string), never rendered again. Raises RuntimeError on anything but a valid label."""
    key = goose_client.KEYS.get(PROVIDER)
    if not key:
        raise RuntimeError("grader: provider %r is not one of %s" % (PROVIDER, ", ".join(goose_client.KEYS)))
    env = child_env(False)
    env[key] = config.load().get(key, "")  # by name to docker (-e NAME): never argv, never a record
    answer = json.dumps(user_prompt(truth, text), ensure_ascii=False)[1:-1]
    for c in "\u2028\u2029\x85":  # YAML line breaks JSON leaves raw
        answer = answer.replace(c, "\\u%04x" % ord(c))
    name = "goose-grader-" + hashlib.sha256((truth + text).encode("utf-8")).hexdigest()[:12]
    with tempfile.TemporaryDirectory() as tmp:
        work, events = os.path.join(tmp, "goose"), os.path.join(tmp, "events.jsonl")
        os.mkdir(work)
        os.chmod(work, 0o755)  # mkdir's mode is masked by the umask; the image's user reads the mount
        for fname, body in (("recipe.yaml", goose_client.build_recipe("", prompt_text(), SCHEMA, None, param="answer")),
                            ("answer.txt", answer)):
            with open(os.path.join(work, fname), "w", encoding="utf-8") as f:
                f.write(body)
            os.chmod(os.path.join(work, fname), 0o644)
        argv = goose_client.docker_argv(name, goose_client.docker_desktop(), work, PROVIDER, GRADER_MODEL, "", 3,
                                        ["--params", "answer=/work/answer.txt"])
        try:
            with open(events, "w", encoding="utf-8") as raw, tempfile.TemporaryFile("w+", encoding="utf-8") as err:
                rc = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, stdout=raw, stderr=err, timeout=TIMEOUT_S).returncode
                err.seek(0)
                stderr = err.read()
        except (OSError, subprocess.TimeoutExpired) as e:
            raise RuntimeError("grader: goose did not run: %r" % e)
        finally:
            goose_client.remove_container(name)
        result = goose_client.to_stream(goose_client.read_events(events), stderr, GRADER_MODEL, None, with_tools=False,
                                        returncode=rc)[-1]
    try:
        ans = result["structured_output"]
        if ans["label"] not in LABELS:
            raise ValueError(ans["label"])
        return {"label": ans["label"], "reason": str(ans["reason"]), "cost_usd": result.get("total_cost_usd")}
    except (ValueError, KeyError, TypeError):
        said = result.get("error") or str(result.get("result") or "").strip()[:200]
        raise NoLabel("grader: the grader returned no structured answer (goose %s %s, exit %s)%s"
                      % (PROVIDER, GRADER_MODEL, rc, ": " + said if said else ""))


def grade(batch_dir, truth, text, call=True):
    """The cached label for (truth, text), else one grader call (call=True) appended to the cache, else None.
    call=True is bench.py run, which writes the current identity to the manifest first; call=False looks up the grader
    the batch's manifest recorded. A call that returns no label is made once more; a label is never asked again.
    Returns {"label", "reason", "cost_usd", "model", "prompt_sha256", "attempts", "cached"}; cost_usd is None on rows
    cached before 2026-11-17, attempts absent on rows cached before 2026-11-23."""
    who = current() if call else batch_grader(batch_dir) or current()
    key, path = cache_key(truth, text, who), os.path.join(batch_dir, "grades.jsonl")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                row = json.loads(line) if line.strip() else {}
                if row.get("key") == key:
                    return dict(row, cached=True)
    if not call:
        return None
    ask = call_cli if CLIENT == "claude" else call_goose
    try:
        got, attempts = ask(truth, text), 1
    except NoLabel:
        got, attempts = ask(truth, text), 2
    row = dict(got, key=key, client=CLIENT, provider=who["provider"], model=GRADER_MODEL, prompt_sha256=prompt_sha(),
               attempts=attempts)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    return dict(row, cached=False)
