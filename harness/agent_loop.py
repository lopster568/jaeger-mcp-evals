#!/usr/bin/env python3
"""Owned agent loop: runs one benchmark trial against the Jaeger MCP server
through the Messages API with a pinned model, and writes stream.jsonl in the
Claude Code CLI's stream-json shape, so score.py reads it unchanged.

Started by `bench.py run` with run.client api (docs/USAGE.md, Clients). Besides
stream.jsonl and exit.txt it writes agent_loop.json (its settings, under
`agent_loop`), reasoning.jsonl, tools.json, mcp.jsonl and
output-schema-sent.json into --out-dir.

Exit codes: 0 a result event was written for a model-driven stop (success,
max turns, max_tokens, refusal, budget), which is a scorable trial; 1 the
trial could not run (MCP or provider unreachable, crash); 2 bad arguments
(alias model id, unpriced model, missing file), nothing is written in that case.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone

from mcp_client import MCPClient, MCPError
from providers.anthropic_provider import THINKING

LOOP_VERSION = "0.1.0"
TOOL_PREFIX = "mcp__jaeger__"
DEFAULT_MAX_TURNS = 30
MCP_SERVER = "jaeger"
# CLI runs of 2026-09-20 report maxOutputTokens 64000 for claude-sonnet-5 in
# the result event's modelUsage.
MAX_TOKENS = 64000
DEFAULT_MAX_BUDGET_USD = 2.0  # bench.py passes its own --max-budget-usd
EFFORTS = ("low", "medium", "high", "xhigh", "max")

# USD per million tokens. Base rates from the claude-api skill docs bundled
# with Claude Code 2.1.281: python/claude-api/README.md "Choose the Right
# Model" (claude-opus-5 $5/$25, claude-sonnet-5 $2/$10, claude-haiku-4-5
# $1/$5) and shared/model-migration.md ("$2/$10 per MTok" for Sonnet 5).
# Cache multipliers from shared/prompt-caching.md "Economics": reads about
# 0.1x input, writes 1.25x (5-minute TTL) and 2x (1-hour TTL). Cross-check:
# these rates reproduced the CLI's own total_cost_usd for a 2026-09-20 CLI
# run to the last digit.
PRICE_TABLE = {
    "source": "claude-api skill 2.1.281: python/claude-api/README.md, shared/model-migration.md, shared/prompt-caching.md",
    "unit": "USD per 1M tokens",
    "cache_multipliers": {"write_5m": 1.25, "write_1h": 2.0, "read": 0.1},
    "models": {
        "claude-sonnet-5": {"input": 2.0, "output": 10.0},
        "claude-opus-5": {"input": 5.0, "output": 25.0},
        "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
    },
}

KNOWN_ALIASES = {"sonnet", "opus", "haiku", "fable", "mythos", "default", "best", "opusplan"}
MODEL_ID_RE = re.compile(r"^claude-[a-z]+(?:-[a-z0-9]+)*$")


class ArgError(Exception):
    pass


def check_model_id(model):
    """Exact ids only. The CLI resolved aliases such as "sonnet" server-side
    of our record; the owned loop pins the string it sends."""
    m = (model or "").strip()
    if (m.lower() in KNOWN_ALIASES or not MODEL_ID_RE.match(m) or not re.search(r"\d", m)):
        raise ArgError(
            "model must be an exact model id such as claude-sonnet-5, got %r. "
            "Aliases (sonnet, opus, ...) and suffixes such as [1m] are not accepted: "
            "the Claude Code CLI resolved aliases itself, the owned loop sends the id verbatim "
            "and records it in agent_loop.json." % model)
    return m


def sha256_file(path):
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def sha256_json(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def iso_ms():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Schema validation (the subset of JSON Schema that verdict-schema.json uses)
# ---------------------------------------------------------------------------

_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool,
    "null": type(None),
}


def validate(instance, schema, path="$"):
    errors = []
    t = schema.get("type")
    if t == "integer":
        ok = isinstance(instance, int) and not isinstance(instance, bool)
    elif t == "number":
        ok = isinstance(instance, (int, float)) and not isinstance(instance, bool)
    elif t in _TYPES:
        ok = isinstance(instance, _TYPES[t])
    else:
        ok = True
    if not ok:
        return ["%s: expected %s, got %s" % (path, t, type(instance).__name__)]
    if "enum" in schema and instance not in schema["enum"]:
        errors.append("%s: %r not in enum" % (path, instance))
    if t == "object":
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in instance:
                errors.append("%s: missing required %r" % (path, k))
        if schema.get("additionalProperties") is False:
            for k in instance:
                if k not in props:
                    errors.append("%s: unexpected property %r" % (path, k))
        for k, sub in props.items():
            if k in instance:
                errors.extend(validate(instance[k], sub, "%s.%s" % (path, k)))
    if t == "array" and "items" in schema:
        for i, item in enumerate(instance):
            errors.extend(validate(item, schema["items"], "%s[%d]" % (path, i)))
    return errors


# ---------------------------------------------------------------------------
# Usage and cost
# ---------------------------------------------------------------------------

USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


def add_usage(total, u):
    for k in USAGE_KEYS:
        total[k] = total.get(k, 0) + int(u.get(k) or 0)
    cc = u.get("cache_creation") or {}
    tcc = total.setdefault("cache_creation", {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0})
    tcc["ephemeral_5m_input_tokens"] += int(cc.get("ephemeral_5m_input_tokens") or 0)
    tcc["ephemeral_1h_input_tokens"] += int(cc.get("ephemeral_1h_input_tokens") or 0)


def turn_cost(model, u):
    """USD for one response's usage, or None if the model is not in PRICE_TABLE."""
    p = PRICE_TABLE["models"].get(model)
    if p is None:
        return None
    m = PRICE_TABLE["cache_multipliers"]
    per = 1e-6
    cc_total = int(u.get("cache_creation_input_tokens") or 0)
    cc = u.get("cache_creation") or {}
    w1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
    w5m = cc_total - w1h  # no breakdown means the default 5-minute TTL
    return (int(u.get("input_tokens") or 0) * p["input"] * per
            + w5m * p["input"] * m["write_5m"] * per
            + w1h * p["input"] * m["write_1h"] * per
            + int(u.get("cache_read_input_tokens") or 0) * p["input"] * m["read"] * per
            + int(u.get("output_tokens") or 0) * p["output"] * per)


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

def parse_args(argv):
    ap = argparse.ArgumentParser(description="Owned agent loop for the Jaeger MCP benchmark.")
    ap.add_argument("--version", action="version", version="agent_loop " + LOOP_VERSION)
    ap.add_argument("--model", required=True, help="exact model id, e.g. claude-sonnet-5")
    ap.add_argument("--scenario")
    ap.add_argument("--arm")
    ap.add_argument("--prompt-file")
    ap.add_argument("--system-prompt-file")
    ap.add_argument("--schema-file")
    ap.add_argument("--scenario-file")
    ap.add_argument("--mcp-config")
    ap.add_argument("--out-dir")
    ap.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    ap.add_argument("--max-budget-usd", type=float, default=DEFAULT_MAX_BUDGET_USD)
    ap.add_argument("--effort", default="high", choices=EFFORTS)
    ap.add_argument("--provider", default="api", choices=("api", "openai"),
                    help="openai: any chat completions endpoint, OPENAI_API_KEY and OPENAI_BASE_URL from the environment")
    ap.add_argument("--probe-model", action="store_true",
                    help="one tiny call; exit 3 if the model the endpoint reports differs from --model")
    ap.add_argument("--validate-only", action="store_true",
                    help="check the model id and arguments, write nothing, exit 0 or 2")
    return ap.parse_args(argv)


def validate_args(args):
    if args.provider == "openai":
        # The model id passes through unchanged; no price applies; effort goes out as reasoning_effort.
        if not args.model.strip():
            raise ArgError("--model is required with --provider openai")
    else:
        validate_anthropic_args(args)
    if args.max_turns < 1:
        raise ArgError("--max-turns must be >= 1")
    if args.validate_only or args.probe_model:
        return
    for name in ("scenario", "arm", "prompt_file", "system_prompt_file", "schema_file", "mcp_config", "out_dir"):
        if not getattr(args, name):
            raise ArgError("--%s is required" % name.replace("_", "-"))
    for name in ("prompt_file", "system_prompt_file", "schema_file", "mcp_config"):
        if not os.path.isfile(getattr(args, name)):
            raise ArgError("no such file for --%s: %s" % (name.replace("_", "-"), getattr(args, name)))
    if args.scenario_file and not os.path.isfile(args.scenario_file):
        raise ArgError("no such file for --scenario-file: %s" % args.scenario_file)


def validate_anthropic_args(args):
    args.model = check_model_id(args.model)
    if args.model not in PRICE_TABLE["models"]:
        raise ArgError(
            "no price for %s in PRICE_TABLE, so neither total_cost_usd nor the --max-budget-usd "
            "ceiling could be computed. Priced models: %s. Add the model's rates (from the "
            "claude-api skill docs) to PRICE_TABLE before running it."
            % (args.model, ", ".join(sorted(PRICE_TABLE["models"]))))


def mcp_url(config_path, server=MCP_SERVER):
    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)
    servers = cfg.get("mcpServers") or {}
    if not servers:
        return None  # the notools arm: no MCP server, no tools
    entry = servers.get(server)
    if not entry or not entry.get("url"):
        raise ArgError("mcp config %s has no mcpServers.%s.url" % (config_path, server))
    if entry.get("type") not in (None, "http", "streamable-http"):
        raise ArgError("mcp server %s has type %r; only streamable HTTP is supported" % (server, entry.get("type")))
    return entry["url"]


def default_provider_factory(args, output_schema):
    # The verdict schema's "$schema" keyword is metadata, not a constraint; it is not sent.
    schema = {k: v for k, v in output_schema.items() if k != "$schema"}
    if args.provider == "openai":
        from providers.openai_provider import OpenAIProvider
        return OpenAIProvider(model=args.model, max_tokens=MAX_TOKENS, output_schema=schema,
                              api_key=os.environ.get("OPENAI_API_KEY", ""), base_url=os.environ.get("OPENAI_BASE_URL", ""),
                              effort=args.effort)
    from providers.anthropic_provider import AnthropicProvider
    return AnthropicProvider(model=args.model, max_tokens=MAX_TOKENS, effort=args.effort, output_schema=schema,
                             api_key=os.environ.get("ANTHROPIC_API_KEY", ""))


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

def block_text(content, kind):
    if kind == "thinking":
        return "".join(b.get("thinking") or "" for b in content if b.get("type") == "thinking")
    return "".join(b.get("text") or "" for b in content if b.get("type") == "text")


def extract_json(last, schema):
    """openai: the final message's content, code fences stripped, is structured_output
    if it parses as JSON and validates; otherwise null, with the errors, as on anthropic."""
    if last is None or any(b.get("type") == "tool_use" for b in last.content):
        return None, ["final message has tool calls or no response"]
    text = block_text(last.content, "text").strip()
    fenced = re.fullmatch(r"```[a-zA-Z]*\s*(.*?)\s*```", text, re.DOTALL)
    try:
        obj = json.loads(fenced.group(1) if fenced else text)
    except ValueError as e:
        return None, ["final text is not JSON: %s" % e]
    errs = validate(obj, schema)
    return (None, errs) if errs else (obj, None)


class TrialState:
    """Counters shared between run_trial and main, so that main can still
    write a terminal result event if an exception escapes run_trial."""

    def __init__(self):
        self.session_id = str(uuid.uuid4())
        self.t_start = time.time()
        self.api_ms = 0
        self.usage_total = {}
        self.cost_total = 0.0
        self.cost_known = True
        self.num_turns = 0
        self.num_tool_calls = 0
        self.result_written = False


def run_trial(args, rec, meta, provider_factory, st=None, mcp_client_factory=MCPClient):
    st = st or TrialState()
    al = meta["agent_loop"]
    last = None
    subtype = None
    stop_details = None
    error_text = None

    with open(args.prompt_file, encoding="utf-8") as f:
        prompt_text = f.read()
    with open(args.system_prompt_file, encoding="utf-8") as f:
        system_text = f.read()
    with open(args.schema_file, encoding="utf-8") as f:
        schema = json.load(f)

    url = mcp_url(args.mcp_config)
    mcp = mcp_client_factory(url, record=rec.mcp_raw, client_version=LOOP_VERSION) if url else None
    mcp_status, tools_raw, init_result = "connected", [], None
    if mcp:
        try:
            init_result = mcp.initialize()
            tools_raw = mcp.list_tools()
        except MCPError as e:
            mcp_status = "failed"
            error_text = str(e)

    rec.write_json("tools.json", {
        "server": MCP_SERVER,
        "server_url": url,
        "listed_utc": utc_now(),
        "protocol_version": getattr(mcp, "protocol_version", None),
        "server_info": getattr(mcp, "server_info", None),
        "initialize_result": init_result,
        "tools": tools_raw,
    })
    al["tools_json_sha256"] = sha256_file(rec.path("tools.json"))

    api_tools = []
    for t in tools_raw:
        name = TOOL_PREFIX + t["name"]
        api_tools.append({
            "name": name,
            "description": t.get("description", ""),
            "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}},
        })
    tool_names = {TOOL_PREFIX + t["name"]: t["name"] for t in tools_raw}
    al["api_tool_names"] = [t["name"] for t in api_tools]

    rec.event({
        "type": "system", "subtype": "init", "session_id": st.session_id,
        "model": args.model,
        "tools": [t["name"] for t in api_tools],
        "mcp_servers": [{"name": MCP_SERVER, "status": mcp_status, "url": url,
                          "protocol_version": mcp.protocol_version, "server_info": mcp.server_info,
                         "initialized_notification_error": getattr(mcp, "initialized_notification_error", None)}] if mcp else [],
        "agent_loop": "owned", "loop_version": LOOP_VERSION,
        "uuid": str(uuid.uuid4()), "timestamp": iso_ms(),
    })

    if mcp_status != "connected":
        subtype = "error_during_execution"
    else:
        provider = provider_factory(args, schema)
        al["api_version"] = provider.api_version()
        sent = provider.output_schema_sent()
        if sent is not None:
            rec.write_json("output-schema-sent.json", sent)
            al["output_schema_sent_sha256"] = sha256_json(sent)
        messages = [{"role": "user", "content": prompt_text}]

        while True:
            if st.num_turns >= args.max_turns:
                subtype = "error_max_turns"
                break
            if st.cost_known and st.cost_total > args.max_budget_usd:
                subtype = "error_max_budget_usd"
                break
            st.num_turns += 1
            t0 = time.time()
            try:
                resp = provider.create(messages, api_tools, system_text)
            except Exception as e:  # API error, network
                st.api_ms += int((time.time() - t0) * 1000)
                subtype = "error_during_execution"
                error_text = "%s: %s" % (type(e).__name__, e)
                st.num_turns -= 1
                break
            st.api_ms += int((time.time() - t0) * 1000)
            last = resp
            if resp.request_id:
                al["request_ids"].append(resp.request_id)
            if resp.model and resp.model not in al["response_models"]:
                al["response_models"].append(resp.model)
            add_usage(st.usage_total, resp.usage or {})
            c = None if args.provider == "openai" else turn_cost(args.model, resp.usage or {})
            if c is None:
                st.cost_known = False
            else:
                st.cost_total += c

            rec.event({
                "type": "assistant",
                "message": {
                    "model": resp.model, "id": resp.message_id, "type": "message", "role": "assistant",
                    "content": resp.content, "stop_reason": resp.stop_reason,
                    "stop_details": resp.stop_details, "usage": resp.usage,
                },
                "parent_tool_use_id": None, "session_id": st.session_id,
                "uuid": str(uuid.uuid4()), "timestamp": iso_ms(), "request_id": resp.request_id,
            })
            tool_uses = [b for b in resp.content if b.get("type") == "tool_use"]
            rec.reasoning({
                "turn": st.num_turns,
                "thinking": block_text(resp.content, "thinking"),
                "thinking_blocks": sum(1 for b in resp.content if b.get("type") == "thinking"),
                "redacted_thinking_blocks": sum(1 for b in resp.content if b.get("type") == "redacted_thinking"),
                "text": block_text(resp.content, "text"),
                "tool_calls": [{"name": b.get("name"), "input": b.get("input")} for b in tool_uses],
                "stop_reason": resp.stop_reason,
                "usage": resp.usage,
                "request_id": resp.request_id,
            })
            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason == "tool_use":
                results, raw_results = [], []
                st.num_tool_calls += len(tool_uses)
                for b in tool_uses:
                    bare = tool_names.get(b.get("name"))
                    if bare is None:
                        r = {"is_error": True, "text": "unknown tool: %s" % b.get("name"), "raw": None}
                    else:
                        try:
                            r = mcp.call_tool(bare, b.get("input") or {})
                        except Exception as e:  # one bad tool response must not end the trial
                            r = {"is_error": True, "text": "tool call raised %s: %s" % (type(e).__name__, e),
                                 "raw": {"client_exception": repr(e)}}
                    results.append({"type": "tool_result", "tool_use_id": b.get("id"),
                                    "content": r["text"], "is_error": bool(r["is_error"])})
                    raw_results.append({"tool_use_id": b.get("id"), "mcp_result": r["raw"]})
                rec.event({
                    "type": "user",
                    "message": {"role": "user", "content": results},
                    "parent_tool_use_id": None, "session_id": st.session_id,
                    "uuid": str(uuid.uuid4()), "timestamp": iso_ms(),
                    "tool_use_results": raw_results,
                })
                messages.append({"role": "user", "content": results})
                continue
            if resp.stop_reason == "pause_turn":
                continue
            if resp.stop_reason in ("end_turn", "stop_sequence"):
                subtype = "success"
            elif resp.stop_reason == "max_tokens":
                subtype = "error_max_tokens"
            elif resp.stop_reason == "refusal":
                subtype = "error_refusal"
                stop_details = resp.stop_details
            else:
                subtype = "error_stop_%s" % resp.stop_reason
                stop_details = resp.stop_details
            break
        if args.provider == "openai":
            al["response_format_supported"] = provider.response_format_supported

    final_text = block_text(last.content, "text") if last is not None else ""
    structured, structured_errors = None, None
    if args.provider == "openai":
        structured, structured_errors = extract_json(last, schema)
    elif final_text.strip():
        try:
            obj = json.loads(final_text)
        except ValueError as e:
            structured_errors = ["final text is not JSON: %s" % e]
        else:
            errs = validate(obj, schema)
            if errs:
                structured_errors = errs
            else:
                structured = obj
    else:
        structured_errors = ["no final text"]

    result = result_event(st, subtype, args.model, al["request_ids"], result=final_text, structured=structured,
                          structured_errors=structured_errors,
                          stop_reason=last.stop_reason if last is not None else None,
                          stop_details=stop_details if stop_details is not None else (last.stop_details if last is not None else None),
                          error=error_text)
    if error_text:
        print("agent_loop: " + error_text, file=sys.stderr)
    rec.event(result)
    st.result_written = True
    al["result_subtype"] = subtype
    # A model-driven stop is a scorable trial; only a trial that could not run fails the cell.
    return 1 if subtype == "error_during_execution" else 0


def result_event(st, subtype, model, request_ids, result="", structured=None, structured_errors=None,
                 stop_reason=None, stop_details=None, error=None):
    """The terminal stream-json result event, for run_trial and write_crash_result alike."""
    ev = {
        "type": "result",
        "subtype": subtype,
        "is_error": subtype != "success",
        "result": result,
        "structured_output": structured,
        "structured_output_errors": structured_errors,
        # num_turns here = API calls. The CLI's num_turns is tool_use blocks
        # (StructuredOutput included) + 1 on the 2026-09 CLI runs, so the two
        # are not comparable; compare score.py's tool_calls instead.
        "num_turns": st.num_turns,
        "num_tool_calls": st.num_tool_calls,
        "duration_ms": int((time.time() - st.t_start) * 1000),
        "duration_api_ms": st.api_ms,
        "stop_reason": stop_reason,
        "stop_details": stop_details,
        "total_cost_usd": st.cost_total if st.cost_known else None,
        "usage": st.usage_total,
        "model": model,
        "session_id": st.session_id,
        "request_ids": list(request_ids),
        "uuid": str(uuid.uuid4()),
    }
    if error:
        ev["error"] = error
    return ev


def build_meta(args):
    meta = {
        "scenario": args.scenario,
        "arm": args.arm,
        "model_requested": args.model,
        "started_utc": utc_now(),
        "prompt_sha256": sha256_file(args.prompt_file),
        "system_prompt_sha256": sha256_file(args.system_prompt_file),
    }
    if args.scenario_file:
        meta["scenario_sha256"] = sha256_file(args.scenario_file)
    meta["verdict_schema_sha256"] = sha256_file(args.schema_file)
    meta["agent_loop"] = {
        "loop": "owned",
        "loop_version": LOOP_VERSION,
        "provider": args.provider,
        "api_version": None,
        "model": args.model,
        "response_models": [],
        "thinking": THINKING,
        "effort": args.effort,
        "effort_applied": True,
        "temperature": "not_sent",
        "top_p": "not_sent",
        "top_k": "not_sent",
        "max_turns": args.max_turns,
        "max_tokens": MAX_TOKENS,
        "max_budget_usd": args.max_budget_usd,
        "tool_choice": "auto",
        "cache_control": {"type": "ephemeral"},
        "structured_output": "output_config.format json_schema (verdict-schema.json without $schema), "
                             "re-validated client-side against verdict-schema.json",
        "tool_name_prefix": TOOL_PREFIX,
        "history_policy": "loop truncates nothing; no compaction; every turn resends the full conversation",
        "price_table_usd_per_mtok": PRICE_TABLE,
        "request_ids": [],
    }
    if args.provider == "openai":
        meta["agent_loop"].update(
            api_base_url="set" if os.environ.get("OPENAI_BASE_URL") else "", thinking="reasoning_effort",
            cache_control=None, price_table_usd_per_mtok=None, response_format_supported=True,
            structured_output="response_format json_schema (strict false) until the endpoint rejects it; "
                              "final content parsed as JSON, code fences stripped; schema errors recorded, not dropped")
    return meta


def write_crash_result(rec, meta, st, exc):
    """Terminal result event for an exception that escaped run_trial."""
    subtype = "error_during_execution"
    rec.event(result_event(st, subtype, meta.get("model_requested"), meta["agent_loop"]["request_ids"],
                           structured_errors=["trial aborted by an exception"],
                           error="%s: %s" % (type(exc).__name__, exc)))
    st.result_written = True
    meta["agent_loop"]["result_subtype"] = subtype


class RunRecorder:
    """Writes the run directory: stream.jsonl, reasoning.jsonl, mcp.jsonl, JSON files, exit.txt."""

    def __init__(self, out_dir):
        self.out_dir = out_dir
        os.makedirs(out_dir, exist_ok=True)
        self._stream = open(self.path("stream.jsonl"), "a", encoding="utf-8")
        self._reasoning = open(self.path("reasoning.jsonl"), "a", encoding="utf-8")
        self._mcp = open(self.path("mcp.jsonl"), "a", encoding="utf-8")

    def path(self, name):
        return os.path.join(self.out_dir, name)

    def write_json(self, name, obj):
        tmp = self.path(name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)
            f.write("\n")
        os.replace(tmp, self.path(name))

    @staticmethod
    def _line(fh, obj):
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        fh.flush()

    def event(self, obj):
        self._line(self._stream, obj)

    def reasoning(self, obj):
        self._line(self._reasoning, obj)

    def mcp_raw(self, obj):
        self._line(self._mcp, obj)

    def write_exit(self, code):
        with open(self.path("exit.txt"), "w", encoding="utf-8") as f:
            f.write("exit=%d\n" % code)

    def close(self):
        for fh in (self._stream, self._reasoning, self._mcp):
            try:
                fh.close()
            except Exception:
                pass


def probe_model(args, provider_factory):
    """One call as cheap as it goes (short prompt, 256 max_tokens, no schema, no reasoning on openai)."""
    p = provider_factory(args, {})
    p.max_tokens = 256
    if args.provider == "openai":
        p.effort, p._schema = None, None
    r = p.create([{"role": "user", "content": "Reply with the word ok."}], [], "Connectivity check.")
    print("probe: endpoint reports model %s (requested %s), request_id %s" % (r.model, args.model, r.request_id))
    return 0 if r.model == args.model else 3


def main(argv=None, provider_factory=None, mcp_client_factory=None):
    try:
        args = parse_args(argv if argv is not None else sys.argv[1:])
        validate_args(args)
        if args.validate_only:
            return 0
        if args.probe_model:
            return probe_model(args, provider_factory or default_provider_factory)
        meta = build_meta(args)
    except ArgError as e:
        print("agent_loop: " + str(e), file=sys.stderr)
        return 2
    except SystemExit as e:  # argparse
        return 2 if e.code else 0

    rec = RunRecorder(args.out_dir)
    st = TrialState()
    code = 1
    try:
        shutil.copyfile(args.prompt_file, rec.path("prompt.txt"))
        shutil.copyfile(args.system_prompt_file, rec.path("system-prompt.txt"))
        rec.write_json("agent_loop.json", meta)
        code = run_trial(args, rec, meta, provider_factory or default_provider_factory,
                         st=st, mcp_client_factory=mcp_client_factory or MCPClient)
    except BaseException as exc:
        traceback.print_exc()
        code = 1
        if not st.result_written:
            try:
                write_crash_result(rec, meta, st, exc)
            except Exception:
                traceback.print_exc()
        raise
    finally:
        meta["agent_loop"]["exit_code"] = code
        meta["ended_utc"] = utc_now()
        try:
            rec.write_json("agent_loop.json", meta)
        finally:
            rec.write_exit(code)
            rec.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
