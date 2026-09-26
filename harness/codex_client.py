#!/usr/bin/env python3
"""Runs one trial through the OpenAI Codex CLI (`codex exec`) and writes stream.jsonl
in the loop's shape (init, assistant tool_use, user tool_result, result with
structured_output), so score.py reads it unchanged. Started by
`bench.py run` with run.client codex; codex's own events are kept in codex-events.jsonl.

Flags, from `codex exec --help` 0.153.2 unless noted: --json (JSONL events),
--output-schema, -m, -s read-only, --ephemeral, --skip-git-repo-check,
--ignore-user-config, --ignore-rules; `-c mcp_servers.jaeger.url=...` is the
streamable HTTP form (`codex mcp add --help`, confirmed with `codex mcp get --json`);
`-c approval_policy="never"` and `-c web_search="disabled"` are typed config keys (a bad
value fails to load); `--disable <feature>` names are from `codex features list`.
Not from help, unverified: `-c model_reasoning_effort`. codex exec has no system
prompt flag, so the system prompt is folded ahead of the prompt, and no turn cap, so
this wrapper stops codex after --max-turns tool calls.

Event mapping, from codex 0.153.2 help text and the event names in its binary
(thread.started, turn.completed, turn.failed, item.completed with item types
mcp_tool_call and agent_message, error); field shapes verify on first live run.

Exit codes: 0 a result event was written for a model-driven stop; 1 codex could
not run or the turn failed.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import config
from agent_loop import extract_json, mcp_url

# Features that give the model a tool other than MCP; `--disable unified_exec` is
# accepted but `codex features list` still shows it enabled, so it is left out.
DISABLE = ("shell_tool", "apps", "plugins", "multi_agent", "image_generation", "browser_use",
           "computer_use", "in_app_browser", "view_image", "sleep_tool", "goals", "tool_suggest")


def codex_argv(codex_bin, model, effort, schema_file, url):
    # ARGV ORDER: measured with codex-cli 0.153.2, any `-c` placed after `exec` makes codex
    # discard every `-c` placed before `exec`. Wrappers (e.g. fcc-codex) build
    # [codex, <their -c model_provider=...>, *our args], so our `-c` pairs must all come
    # before `exec` or they erase the wrapper's provider override and codex falls back to
    # api.openai.com. Every other flag stays after exec as before.
    c_pairs = ["-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
               "-c", 'model_reasoning_effort="%s"' % effort]
    if url:
        c_pairs += ["-c", 'mcp_servers.jaeger.url="%s"' % url]
    argv = [codex_bin] + c_pairs + ["exec", "--json", "--ephemeral", "--skip-git-repo-check",
            "--ignore-user-config", "--ignore-rules", "--color", "never", "-s", "read-only",
            "-m", model, "--output-schema", schema_file]
    for f in DISABLE:
        argv += ["--disable", f]
    return argv + ["-"]  # the prompt arrives on stdin


def version(codex_bin):
    try:
        return subprocess.run([codex_bin, "--version"], capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL).stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def result_text(r):
    return "".join(c.get("text", "") for c in (r or {}).get("content") or [] if isinstance(c, dict))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    for k in ("model", "effort", "prompt-file", "system-prompt-file", "schema-file", "mcp-config", "out-dir"):
        ap.add_argument("--" + k, required=True)
    ap.add_argument("--max-turns", type=int, default=30)
    ap.add_argument("--codex-bin", default="codex", help="command that runs the Codex CLI, e.g. a proxy wrapper")
    a = ap.parse_args(argv)
    url = mcp_url(a.mcp_config, "jaeger")
    argv = codex_argv(a.codex_bin, a.model, a.effort, a.schema_file, url)
    with open(a.system_prompt_file, encoding="utf-8") as f:
        prompt = f.read().rstrip("\n") + "\n\n"
    with open(a.prompt_file, encoding="utf-8") as f:
        prompt += f.read()
    with open(a.schema_file, encoding="utf-8") as f:
        schema = json.load(f)

    t0, calls, text, usage, sid, subtype, err = time.time(), 0, "", {}, None, None, None
    with open(os.path.join(a.out_dir, "stream.jsonl"), "w", encoding="utf-8") as out, \
            open(os.path.join(a.out_dir, "codex-events.jsonl"), "w", encoding="utf-8") as raw:
        emit = lambda e: out.write(json.dumps(e, ensure_ascii=False) + "\n")
        # Recorded without the fixture host or any absolute path: this line gets published.
        endpoint = config.host_free(url) if url else None
        emit({"type": "system", "subtype": "init", "model": a.model, "client_version": version(a.codex_bin),
              "mcp_servers": [{"name": "jaeger", "url": endpoint}] if url else [],
              "codex_argv": codex_argv(os.path.basename(a.codex_bin), a.model, a.effort,
                                       config.repo_relpath(a.schema_file), endpoint),
              "codex_bin": os.path.basename(a.codex_bin)})
        try:
            p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        except OSError as e:
            p, subtype, err = None, "error_during_execution", "could not start codex: %s" % e
        if p:
            p.stdin.write(prompt)
            p.stdin.close()
            for line in p.stdout:
                raw.write(line)
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                t, item = e.get("type"), e.get("item") or {}
                if t == "thread.started":
                    sid = e.get("thread_id")
                elif t == "turn.completed":
                    subtype = subtype or "success"
                    for k, v in (e.get("usage") or {}).items():
                        usage[k] = usage.get(k, 0) + v
                elif t == "turn.failed":
                    err = (e.get("error") or {}).get("message") or line.strip()
                elif t == "error":
                    msg = e.get("message") or (e.get("error") or {}).get("message") or line.strip()
                    err = err or msg
                    emit({"type": "system", "subtype": "codex_error", "message": msg})
                elif t == "item.completed" and item.get("type") == "agent_message":
                    text = item.get("text") or ""
                    emit({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
                elif t == "item.completed" and item.get("type") == "error":
                    # warnings such as "Model metadata for X not found" or "Falling back from
                    # WebSockets"; not a tool call, not a turn (score.py ignores non-init system events).
                    emit({"type": "system", "subtype": "codex_error", "message": item.get("message") or item.get("text") or ""})
                elif t == "item.completed" and item.get("type") not in ("reasoning", None):
                    # mcp_tool_call becomes mcp__<server>__<tool>; any other tool item keeps its type as
                    # the name, so score.py lists it under non_jaeger_tool_calls.
                    mcp = item["type"] == "mcp_tool_call"
                    name = "mcp__%s__%s" % (item.get("server"), item.get("tool")) if mcp else item["type"]
                    emit({"type": "assistant", "message": {"role": "assistant", "content": [
                        {"type": "tool_use", "id": item.get("id"), "name": name, "input": item.get("arguments") if mcp else item}]}})
                    fail = item.get("error")
                    msg = str(fail.get("message", "") if isinstance(fail, dict) else fail or "")
                    emit({"type": "user", "message": {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": item.get("id"), "content": result_text(item.get("result")) or msg,
                         "is_error": bool(fail) or item.get("status") == "failed"}]}})
                    calls += 1
                    if calls >= a.max_turns:
                        subtype = "error_max_turns"
                        p.terminate()
                        break
            p.wait()
            if subtype is None:
                subtype = "error_during_execution"
                err = err or "codex exited %d without turn.completed" % p.returncode
        structured, errors = extract_json(SimpleNamespace(content=[{"type": "text", "text": text}]), schema)
        # score.py sums input_tokens and cache_read_input_tokens; codex's input_tokens includes the cached ones.
        u = {"input_tokens": usage.get("input_tokens", 0) - usage.get("cached_input_tokens", 0),
             "cache_read_input_tokens": usage.get("cached_input_tokens", 0), "output_tokens": usage.get("output_tokens", 0)}
        result = {"type": "result", "subtype": subtype, "is_error": subtype != "success", "result": text,
                  "structured_output": structured, "structured_output_errors": errors,
                  "num_turns": calls + 1,  # the CLI's convention: tool calls plus one
                  "num_tool_calls": calls, "duration_ms": int((time.time() - t0) * 1000),
                  "total_cost_usd": None, "usage": u, "model": a.model, "session_id": sid}
        if err:
            result["error"] = err
            print("codex_client: " + err, file=sys.stderr)
        emit(result)
    return 1 if subtype == "error_during_execution" else 0


if __name__ == "__main__":
    sys.exit(main())
