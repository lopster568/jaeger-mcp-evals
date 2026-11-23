#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Runs one trial through Goose (`goose run`) in the image the Jaeger AI Assistant demo pins, and
writes stream.jsonl in the Claude CLI's stream-json shape, so score.py reads it unchanged.
Started by `bench.py run` with run.client goose; Goose's own events are kept in goose-events.jsonl
and its stderr goes to the trial's stderr.txt.

The container starts through goose/goose-without-extensions.sh, vendored unchanged from Jaeger main
f9046f3dad (scripts/ai-sidecar/goose), so Goose's bundled extensions are off as in the demo. The
recipe (<out-dir>/goose/recipe.yaml, JSON text, which Goose's YAML reader accepts) carries the
prompt, the system prompt as `instructions`, the Jaeger MCP server and the verdict schema. Goose
renders a recipe as a template, so text holding "{{", "{%" or "{#" is refused. The provider key is
passed by name only (`-e NAME`): docker reads its value from this process's environment.
Event shapes are from Goose v1.57.0 recordings (stub model, 2026-11-21).

Exit codes: 0 a result event was written for a model-driven stop; 1 Goose could not run, the run
failed, Goose could not start the jaeger extension, or the trial was stopped by a signal.
"""
import argparse
import ipaddress
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone

import config

IMAGE = "ghcr.io/aaif-goose/goose:v1.57.0@sha256:06485bd5275090903b7ef5313059d792dd88759d9c6e3365d300aba37454ca58"
WRAPPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "goose", "goose-without-extensions.sh")
KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openrouter": "OPENROUTER_API_KEY"}
TEMPLATE_MARKERS = ("{{", "{%", "{#")
FINAL_TOOL = "recipe__final_output"
# Goose v1.57.0 texts: ops_maxturns.rs MAX_TURNS_MESSAGE, ops_compaction.rs, session/output.rs.
MAX_TURNS_MESSAGE = ("I've reached the maximum number of actions I can do without user input. "
                     "Would you like me to continue?")
COMPACTION_NOTICE = "Exceeded auto-compact threshold"
CONNECT_FAILED = "Failed to start extension 'jaeger'"


def build_recipe(prompt, instructions, schema, url, param=None):
    """The recipe text; url None gives no extension (an arm with tools off). param: the prompt is the file parameter
    of that name, supplied with `--params NAME=<path>`: Goose substitutes it once (template_recipe.rs render, v1.57.0)
    and never renders it again, so the text may hold template markers; the file holds it JSON-escaped."""
    recipe = {"version": "1.0.0", "title": "Jaeger root cause",
              "description": "One scenario prompt against Jaeger's MCP tools, answered through the verdict schema.",
              "instructions": instructions.strip(), "prompt": "{{ %s }}" % param if param else prompt.strip(),
              "extensions": [{"type": "streamable_http", "name": "jaeger", "uri": url, "timeout": 300}] if url else [],
              "response": {"json_schema": schema}}
    if param:
        recipe["parameters"] = [{"key": param, "input_type": "file", "requirement": "required", "description": "the text to judge"}]
    text = json.dumps(recipe, indent=2, ensure_ascii=False) + "\n"  # JSON escapes for non-ASCII are not valid YAML
    found = [m for m in TEMPLATE_MARKERS if m in (json.dumps(instructions, ensure_ascii=False) if param else text)]
    if found:
        raise ValueError("recipe text holds template markers %s; Goose would render them" % " ".join(found))
    return text


def is_loopback(host):
    try:
        return host == "localhost" or ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def container_url(url, desktop):
    """On Docker Desktop the container's host network is the VM's, so a loopback host becomes host.docker.internal."""
    u = urllib.parse.urlsplit(url)
    if not (desktop and is_loopback(u.hostname)):
        return url
    return u._replace(netloc="host.docker.internal" + (":%d" % u.port if u.port else "")).geturl()


def docker_desktop():
    try:
        r = subprocess.run(["docker", "info", "--format", "{{.OperatingSystem}}"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "Docker Desktop" in r.stdout


def container_name(out_dir):
    """goose-<batch>-<trial>, in the characters docker allows (arm names are free text)."""
    out_dir = os.path.abspath(out_dir)
    raw = "%s-%s" % (os.path.basename(os.path.dirname(out_dir)), os.path.basename(out_dir))
    return "goose-" + re.sub(r"[^a-zA-Z0-9_.-]", "-", raw)


def docker_argv(name, desktop, work, provider, model, effort, max_turns, extra=()):
    """work is mounted at /work; docker -v reads a relative path as a volume name, so it is made absolute here.
    effort "" sets no GOOSE_THINKING_EFFORT; extra goes after the run flags."""
    env = ["-e", "GOOSE_PROVIDER=" + provider, "-e", "GOOSE_MODEL=" + model]
    if effort:
        env += ["-e", "GOOSE_THINKING_EFFORT=" + effort]
    return (["docker", "run", "--rm", "--name", name] + ([] if desktop else ["--network", "host"]) +
            ["-w", "/work", "-v", "%s:/work:ro" % os.path.abspath(work), "-v", "%s:/goose-without-extensions.sh:ro" % WRAPPER] + env +
            ["-e", KEYS[provider], "--entrypoint", "sh", IMAGE, "/goose-without-extensions.sh",
             "run", "--recipe", "/work/recipe.yaml", "--max-turns", str(max_turns), "--no-session", "-q",
             "--output-format", "stream-json"] + list(extra))


def remove_container(name):
    """A timed-out or interrupted container would keep calling the model and Jaeger."""
    try:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _iso(created):
    try:
        return datetime.fromtimestamp(created, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except (TypeError, ValueError, OverflowError):
        return None


def _event(kind, content, stamp):
    e = {"type": kind, "message": {"role": kind, "content": content}}
    if stamp:
        e["timestamp"] = stamp
    return e


def to_stream(goose_events, stderr_text, model, client_version, with_tools=True, duration_ms=0, returncode=0):
    """Goose's stream-json events as stream.jsonl events in the Claude CLI's shape. Pure."""
    out, text_by_id, finals = [], {}, {}
    structured, last_text, calls, complete, error = None, "", 0, None, None
    for e in goose_events:
        t = e.get("type")
        if t == "complete":
            complete = e
        elif t == "error":
            error = str(e.get("error") or "")
        if t != "message":
            continue
        m = e.get("message") or {}
        stamp = _iso(m.get("created"))
        for b in m.get("content") or []:
            kind = b.get("type")
            if kind == "text" and m.get("role") == "assistant":
                ev = text_by_id.get(m.get("id"))
                if ev is None:  # text arrives as deltas sharing one message id
                    ev = text_by_id[m.get("id")] = _event("assistant", [{"type": "text", "text": ""}], stamp)
                    out.append(ev)
                ev["message"]["content"][0]["text"] += b.get("text") or ""
                last_text = ev["message"]["content"][0]["text"]
            elif kind == "toolRequest":
                call = b.get("toolCall") or {}
                if call.get("status") != "success":  # arguments Goose could not parse: no tool name to report
                    continue
                v = call.get("value") or {}
                name = str(v.get("name") or "")
                if name == FINAL_TOOL:
                    finals[b.get("id")] = v.get("arguments")
                name = ("StructuredOutput" if name == FINAL_TOOL else
                        "mcp__jaeger__" + name[len("jaeger__"):] if name.startswith("jaeger__") else name)
                out.append(_event("assistant", [{"type": "tool_use", "id": b.get("id"), "name": name,
                                                 "input": v.get("arguments") or {}}], stamp))
                calls += 1
            elif kind == "toolResponse":
                r = b.get("toolResult") or {}
                v = r.get("value") or {}
                ok = r.get("status") == "success" and not v.get("isError")
                text = ("".join(c.get("text", "") for c in v.get("content") or [] if isinstance(c, dict))
                        if r.get("status") == "success" else str(r.get("error") or ""))
                if ok and b.get("id") in finals:  # only an answer Goose validated against the schema
                    structured = finals[b.get("id")]
                out.append(_event("user", [{"type": "tool_result", "tool_use_id": b.get("id"), "content": text,
                                            "is_error": not ok}], stamp))
            elif kind == "systemNotification" and str(b.get("msg") or "").startswith(COMPACTION_NOTICE):
                out.append({"type": "system", "subtype": "compact_boundary", "message": b.get("msg")})

    connect_failed = with_tools and CONNECT_FAILED in (stderr_text or "")
    err = None
    if error is not None:
        subtype, err = "error_during_execution", error
    elif connect_failed:
        subtype, err = "error_during_execution", "Goose could not start the jaeger extension (see stderr.txt)"
    elif complete is None or returncode is None:  # stopped or crashed before Goose exited: never a success
        subtype = "error_during_execution"
        err = "goose stopped before it finished" if returncode is None else "goose exited %s without a complete event" % returncode
        said = next((l.strip() for l in reversed((stderr_text or "").splitlines()) if l.strip()), "")
        if returncode is not None and said:  # the provider's refusal is often only on stderr
            err += ": " + said[:200]
    elif structured is None and last_text.strip() == MAX_TURNS_MESSAGE:
        subtype = "error_max_turns"
    else:
        subtype = "success"
    c = complete or {}
    n = lambda k: c.get(k) or 0
    # Goose's input_tokens includes cache reads and writes; score.py and otlp.py add those again.
    usage = {"input_tokens": n("input_tokens") - n("cache_read_input_tokens") - n("cache_write_input_tokens"),
             "cache_read_input_tokens": n("cache_read_input_tokens"),
             "cache_creation_input_tokens": n("cache_write_input_tokens"), "output_tokens": n("output_tokens")}
    init = {"type": "system", "subtype": "init", "model": model, "client_version": client_version, "goose_image": IMAGE,
            "mcp_servers": [{"name": "jaeger"}] if with_tools and not connect_failed else []}
    result = {"type": "result", "subtype": subtype, "is_error": subtype != "success", "result": last_text,
              "structured_output": structured, "num_turns": calls + 1,  # the CLI's convention: tool calls plus one
              "num_tool_calls": calls, "duration_ms": duration_ms, "total_cost_usd": c.get("cost_usd"),
              "usage": usage, "model": model, "session_id": None}
    if err:
        result["error"] = err
    return [init] + out + [result]


def read_events(path):
    events = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                events.append(e)
    return events


def _stop(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    for k in ("model", "effort", "provider", "prompt-file", "system-prompt-file", "schema-file", "mcp-config", "out-dir"):
        ap.add_argument("--" + k, required=True)
    ap.add_argument("--max-turns", type=int, default=30)
    ap.add_argument("--client-version", default=None, help="goose --version in IMAGE, from bench.py's pre-flight")
    a = ap.parse_args(argv)
    if a.provider not in KEYS:
        ap.error("--provider must be one of %s" % ", ".join(KEYS))
    for sig in (signal.SIGTERM, signal.SIGHUP):  # bench.py stops a cell with SIGTERM: the container must still go
        signal.signal(sig, _stop)
    url = config.mcp_config_url(a.mcp_config, "jaeger")
    desktop = docker_desktop()
    read = lambda p: open(p, encoding="utf-8").read()
    try:
        recipe = build_recipe(read(a.prompt_file), read(a.system_prompt_file), json.loads(read(a.schema_file)),
                              container_url(url, desktop) if url else None)
    except ValueError as e:
        print("goose_client: %s" % e, file=sys.stderr)
        return 1
    work = os.path.abspath(os.path.join(a.out_dir, "goose"))  # docker -v reads a relative path as a volume name
    os.makedirs(work, exist_ok=True)
    os.chmod(work, 0o755)  # the image's user reads the mount
    with open(os.path.join(work, "recipe.yaml"), "w", encoding="utf-8") as f:
        f.write(recipe)
    os.chmod(os.path.join(work, "recipe.yaml"), 0o644)
    name = container_name(a.out_dir)
    events_path = os.path.join(a.out_dir, "goose-events.jsonl")
    t0, rc, stopped = time.time(), None, False
    with open(events_path, "w", encoding="utf-8") as raw, tempfile.TemporaryFile("w+", encoding="utf-8") as err:
        try:
            rc = subprocess.run(docker_argv(name, desktop, work, a.provider, a.model, a.effort, a.max_turns),
                                stdin=subprocess.DEVNULL, stdout=raw, stderr=err).returncode
        except KeyboardInterrupt:
            stopped = True
        except OSError as e:
            err.write("goose_client: could not start docker: %s\n" % e)
        finally:
            for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):  # a second signal must not skip the removal
                signal.signal(s, signal.SIG_IGN)
            remove_container(name)
        err.seek(0)
        stderr_text = err.read()
    sys.stderr.write(stderr_text)
    stream = to_stream(read_events(events_path), stderr_text, a.model, a.client_version, with_tools=url is not None,
                       duration_ms=int((time.time() - t0) * 1000), returncode=rc)
    with open(os.path.join(a.out_dir, "stream.jsonl"), "w", encoding="utf-8") as f:
        f.writelines(json.dumps(e, ensure_ascii=False) + "\n" for e in stream)
    if stream[-1].get("error"):
        print("goose_client: " + stream[-1]["error"], file=sys.stderr)
    return 1 if stopped or stream[-1]["subtype"] == "error_during_execution" else 0


if __name__ == "__main__":
    sys.exit(main())
