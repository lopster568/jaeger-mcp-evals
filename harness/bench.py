#!/usr/bin/env python3
"""bench.py: run benchmark batches against the fixture and check their records.

  bench.py run <experiment.json> [--dry-run] [--first-only]
                                  run the batch the file describes (every run setting lives in it)
  bench.py verify                 re-score every stream.jsonl under RUNS_DIR; fail if records/INDEX.md is stale
  bench.py band <batch_dir> [--arm A]
  bench.py leak <file>...
  bench.py readiness <scenario>
  bench.py oracle <scenario>      scripted MCP calls must reach signal_regex (needs the fault active)
  bench.py soak <scenario> [samples=30] [interval_s=60]
  bench.py index                  write records/INDEX.md
  bench.py pack <experiment>      dist/<experiment>-trajectories.tar.gz of its batches, after a leak scan
  bench.py power <n_per_arm>      Fisher's exact test significance table
  bench.py export <batch_dir> [--endpoint URL]
                                  POST the batch's trajectories to Phoenix (<URL>/v1/traces), after a leak scan

The experiment file (harness/experiments/<name>.json, docs/USAGE.md) is the complete
configuration of a batch. fixture.env (harness/config.py) holds only where the fixture
runs and the API keys. Records follow docs/RECORD.md (schema v4).

run exit codes: 0 done (or dry run / first-only), 1 bad experiment file, pre-flight or
setup failure (nothing ran), 4 aborted after 3 consecutive cell failures, 5 the flag was not
confirmed back at its default after the restore (check the fixture by hand; wins
over every other code), 6 interrupted (SIGINT, SIGTERM or SIGHUP) or fewer cells
ran than planned.
band exit codes: 0 certified, 2 n < 10 (rank only), 3 pass rate 0 or 1 (read the
trajectories). leak and readiness: 0 pass, 1 fail, 2 bad input. oracle: 0 pass, 1 fail.
soak: 0 pass, 5 fail. pack: 0 written, 1 refused (a scan hit or a missing batch).
export: 0 sent, 1 refused (a scan hit or an unreadable batch) or Phoenix error; nothing is written either way.
"""
import argparse
import glob
import gzip
import hashlib
import http.server
import io
import json
import math
import os
import platform
import random
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from time import sleep

import config
import fixture_leak
import grade
import judge
import otlp
import score
import tools
from mcp_client import MCPClient, MCPError

HARNESS = os.path.dirname(os.path.abspath(__file__))
ROOT = config.ROOT
SYSTEM_PROMPT = os.path.join(HARNESS, "system-prompt.txt")
VERDICT_SCHEMA = os.path.join(HARNESS, "verdict-schema.json")
AGENT_LOOP = os.path.join(HARNESS, "agent_loop.py")
CODEX_CLIENT = os.path.join(HARNESS, "codex_client.py")
RECORDS = os.path.join(ROOT, "records")
DIST = os.path.join(ROOT, "dist")
# api: the owned loop (harness/agent_loop.py), which refuses aliases; cli: the Claude Code CLI;
# codex: the OpenAI Codex CLI.
CLIENTS = ("api", "cli", "codex")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new"]
TRACE_POLL_MAX, TRACE_POLL_SLEEP, TRACE_MIN_COUNT = 48, 10, 3
BASELINE_LOOKBACK_S = 600
HISTORY_POLICY = {"cli": {"harness_truncation": "none", "cli_autocompact": "default"},
                  "api": {"harness_truncation": "none", "cli_autocompact": "n/a"},
                  "codex": {"harness_truncation": "none", "cli_autocompact": "unknown"}}
NOT_RECORDED = "not recorded"
# Runs on the fixture with the flag file, flag and variant as argv; the script
# itself arrives on stdin, so no value is ever spliced into Python source.
FLIP_SCRIPT = """import json, sys
p, flag, value = sys.argv[1:4]
with open(p) as f:
    d = json.load(f)
d['flags'][flag]['defaultVariant'] = value
with open(p, 'w') as f:
    json.dump(d, f, indent=2)
    f.write(chr(10))
"""
SOAK_REMOTE = ('l=$(cut -d" " -f1 /proc/loadavg); u=$(docker ps --format "{{.Status}}" | grep -c -E "unhealthy|Restarting"); '
               'c=$(docker ps -q | wc -l); s=$(vmstat 1 2 | tail -1 | awk "{print \\$14}"); echo "$l $u $c $s"')


def sha256_file(path):
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


repo_relpath = config.repo_relpath


def recorded_argv(argv, bdir, trial):
    """The argv as meta.json records it: no absolute path. Inside the trial dir, trial-relative;
    elsewhere under the batch dir, batch-relative; else repo-relative (basename outside the repo)."""
    out = []
    for x in argv:
        if isinstance(x, str) and os.path.isabs(x):
            base = next((d for d in (trial, bdir) if x == d or x.startswith(d + os.sep)), None)
            x = os.path.relpath(x, base) if base else repo_relpath(x)
        out.append(x)
    return out


def utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_text(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, doc):
    """Atomic: a reader or a crash never sees half a file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def read_jsonl(path):
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def runs_root(cfg):
    return os.path.join(ROOT, cfg["RUNS_DIR"])


# ---- fixture access -----------------------------------------------------------

def fixture_where(cfg):
    user = cfg.get("FIXTURE_SSH_USER")
    return "ssh %s@%s" % (user, cfg["FIXTURE_HOST"]) if user else "local"


def fixture_sh(cfg, cmd, stdin_text=None, timeout=120):
    """Run cmd in FIXTURE_DEMO_DIR (absolute, or relative to the home directory): with bash
    on this machine, or over ssh when FIXTURE_SSH_USER is set."""
    io = {"input": stdin_text} if stdin_text is not None else {"stdin": subprocess.DEVNULL}
    d = cfg["FIXTURE_DEMO_DIR"]
    if cfg.get("FIXTURE_SSH_USER"):  # the ssh login shell starts in the home directory
        target = "%s@%s" % (cfg["FIXTURE_SSH_USER"], cfg["FIXTURE_HOST"])
        argv, cwd = ["ssh", *SSH_OPTS, target, "cd %s && %s" % (shlex.quote(d.removeprefix("~/")), cmd)], None
    else:
        argv, cwd = ["bash", "-c", cmd], os.path.join(os.path.expanduser("~"), os.path.expanduser(d))
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, **io)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess([], 255, "", str(e))


def http_json(url, body=None, timeout=20):
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def ofrep_url(cfg, flag):
    return "http://%s:%s/ofrep/v1/evaluate/flags/%s" % (cfg["FIXTURE_HOST"], cfg["OFREP_PORT"], flag)


def ofrep_variant(cfg, flag):
    """The variant OFREP reports for flag, or "" on any failure."""
    try:
        d = http_json(ofrep_url(cfg, flag), b'{"context":{}}', 8)
    except Exception:
        return ""
    v = d.get("variant")
    if v is None:
        v = d.get("value")
    return "" if v is None else str(v)


def traces(cfg, service, start_us, end_us, limit, timeout):
    q = urllib.parse.urlencode({"service": service, "start": start_us, "end": end_us, "limit": limit})
    return http_json(config.jaeger_base(cfg) + "/api/traces?" + q, timeout=timeout).get("data") or []


def now_us():
    return int(time.time() * 1e6)


# ---- fixture stability (per-trial snapshots, host thrash, restart gate) --------

def container_snapshot(cfg):
    """{container_name: (restart_count, oom_killed, started_at)} for every container docker ps
    -aq lists, from one ssh or local call; {} if the call fails."""
    r = fixture_sh(cfg, "docker inspect -f '{{.Name}} {{.RestartCount}} {{.State.OOMKilled}} {{.State.StartedAt}}' $(docker ps -aq)")
    if r.returncode != 0:
        return {}
    out = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) == 4:
            name, restarts, oom, started = parts
            out[name.lstrip("/")] = (restarts, oom, started)
    return out


def fixture_diff(before, after, expected):
    """Containers whose snapshot differs between before and after (appeared, disappeared, or a
    changed RestartCount/OOMKilled/StartedAt), and fixture_ok: false iff any of them is not in
    expected (a scenario's expected_restarts, container names). Fails closed: an empty before or
    after (container_snapshot could not read the fixture) is never treated as "no changes"."""
    if not before or not after:
        return ["snapshot_failed"], False
    changed = [{"container": n, "before": before.get(n), "after": after.get(n)}
               for n in sorted(set(before) | set(after)) if before.get(n) != after.get(n)]
    return changed, all(c["container"] in expected for c in changed)


def wait_for_restart(cfg, name, restart0, poll_max, out=lambda *a: None):
    """Poll name's RestartCount (container_snapshot()[name][0]) every TRACE_POLL_SLEEP until it
    rises above restart0, up to poll_max times. Returns the poll number it rose on, or None if
    it never did (name missing from a poll's snapshot counts as no change yet)."""
    for i in range(1, poll_max + 1):
        snap = container_snapshot(cfg).get(name)
        cur, oom = (int(snap[0]), snap[1]) if snap else (None, None)
        out(i, cur, oom)
        if cur is not None and cur > restart0:
            return i
        if i < poll_max:
            sleep(TRACE_POLL_SLEEP)
    return None


def restart_before_run(cfg, names, service, poll_max, out=lambda *a: None):
    """Hygiene restart that keeps itself out of the trace store: docker restart names, poll
    container_snapshot until each StartedAt is newer than before, and only then docker restart
    jaeger and poll its query API until it answers (any HTTP reply counts). Returns
    {"containers": {name: utc}, "jaeger": utc}; raises RuntimeError if a step does not finish
    within poll_max polls of TRACE_POLL_SLEEP."""
    before = container_snapshot(cfg)
    if not all(n in before for n in names):
        raise RuntimeError("restart_before_run: %s not all in the container snapshot" % names)
    rec = {"containers": {}}
    for n in names:
        if fixture_sh(cfg, "docker restart %s" % shlex.quote(n)).returncode != 0:
            raise RuntimeError("docker restart %s failed" % n)
        rec["containers"][n] = utc()
    for i in range(1, poll_max + 1):
        snap = container_snapshot(cfg)
        pending = [n for n in names if n not in snap or snap[n][2] <= before[n][2]]
        out(i, pending)
        if not pending:
            break
        if i == poll_max:
            raise RuntimeError("%s did not come back running after %d polls" % (pending, poll_max))
        sleep(TRACE_POLL_SLEEP)
    if fixture_sh(cfg, "docker restart jaeger").returncode != 0:
        raise RuntimeError("docker restart jaeger failed")
    rec["jaeger"] = utc()
    for i in range(1, poll_max + 1):
        try:
            traces(cfg, service, now_us() - 60 * 10**6, now_us(), 1, 10)
            return rec
        except urllib.error.HTTPError:
            return rec
        except Exception:
            out(i, ["jaeger"])
            sleep(TRACE_POLL_SLEEP)
    raise RuntimeError("jaeger query API did not answer after %d polls" % poll_max)


def vmstat_bi_mean(text):
    """Mean of vmstat's 'bi' column (procps' fixed field 9, index 8; no header lookup) over the
    non-first sample (the first sample is the average since boot, not live), skipping vmstat's
    own two header lines and any line without a digit there. None with fewer than two samples."""
    lines = [l for l in text.splitlines() if l.strip()][2:]
    bi = [int(parts[8]) for parts in (l.split() for l in lines) if len(parts) > 8 and parts[8].isdigit()]
    return statistics.mean(bi[1:]) if len(bi) > 1 else None


# ---- gates --------------------------------------------------------------------

def leak(files, out=print):
    for f in files:
        if not os.path.isfile(f):
            out("leak: no such file: %s" % f)
            return 2
    return leak_texts([(f, read_text(f)) for f in files], out)


# The stock search_traces description says "error flag" (a trace's error status), which names no fault.
TOOLS_ALLOW = re.compile(r"\berror flag\b", re.I)


def leak_texts(named, out=print):
    """leak() over (name, text) pairs: 0 clean, 1 a leak word found, 2 no word list."""
    words = score.leak_words(HARNESS)
    if not words:
        out("leak: missing or empty %s" % os.path.join(HARNESS, "leak-words.txt"))
        return 2
    hits = []
    for name, text in named:
        for n, line in enumerate(text.splitlines(), 1):
            hits += [(name, n, w) for w in words if score.leak_present(line, [w])]
    for f, n, w in hits:
        out("%s:%d:%s" % (f, n, w))
    if hits:
        out("leak: FAIL - leak word(s) found (file:line:word above)")
        return 1
    out("leak: PASS - no leak words found in: %s" % " ".join(name for name, _ in named))
    return 0


def oracle_shape_ok(calls):
    """True iff calls is a non-empty list of {tool: str, arguments: dict}."""
    return isinstance(calls, list) and bool(calls) and all(
        isinstance(c, dict) and isinstance(c.get("tool"), str) and isinstance(c.get("arguments"), dict) for c in calls)


def readiness(scenario, out=print, harness=HARNESS):
    sf = os.path.join(harness, "scenarios", scenario + ".json")
    if not os.path.isfile(sf):
        out("readiness %s: FAIL - scenario file not found: %s" % (scenario, sf))
        return 1
    try:
        d = load_json(sf)
    except Exception as e:
        out("readiness %s: scenario json parses - FAIL (%s)" % (scenario, e))
        out("readiness %s: FAIL" % scenario)
        return 1
    ev = d.get("evidence") if isinstance(d.get("evidence"), dict) else {}
    gt_path, base_path = (os.path.normpath(os.path.join(harness, "scenarios", ev[k])) if ev.get(k) else ""
                          for k in ("ground_truth_trace", "baseline_absence"))
    gt = d.get("ground_truth") if isinstance(d.get("ground_truth"), dict) else {}
    det, sig = d.get("deterministic"), d.get("signal_regex", "")
    gt_exists, base_exists = (bool(p) and os.path.isfile(p) for p in (gt_path, base_path))

    def safe(fn):
        try:
            return fn()
        except Exception:
            return False

    # (name, result or None when skipped, detail)
    checks = [
        ("scenario json parses", True, ""),
        ("version is a positive integer", type(d.get("version")) is int and d["version"] > 0, "version=%r" % (d.get("version"),)),
        ("deterministic is yes/no/bucketed", det in ("yes", "no", "bucketed"), "deterministic=%r" % (det,)),
        ("ground_truth.service set", bool(gt.get("service")), "service=%r" % (gt.get("service"),)),
        ("ground_truth.operation set", bool(gt.get("operation")), "operation=%r" % (gt.get("operation"),)),
        ("mechanism_truth set", isinstance(d.get("mechanism_truth"), str) and bool(d["mechanism_truth"]), ""),
        ("signal_regex present", bool(sig), ""),
        ("oracle is a non-empty list of {tool, arguments}", oracle_shape_ok(d.get("oracle")), "oracle=%r" % (d.get("oracle"),)),
        ("ground_truth_trace file exists", gt_exists, gt_path or "evidence.ground_truth_trace not set"),
        ("ground_truth_trace matches signal_regex",
         safe(lambda: re.search(sig, read_text(gt_path), re.I) is not None) if gt_exists and sig else None, sig),
        ("baseline_absence file exists", base_exists, base_path or "evidence.baseline_absence not set"),
        ('baseline_absence contains "signal_present": false',
         safe(lambda: load_json(base_path).get("signal_present") is False) if base_exists else None, ""),
    ]
    for name, ok, msg in checks:
        out("readiness %s: %s - %s" % (scenario, name, "SKIPPED" if ok is None else
                                       ("OK" if ok else "FAIL") + (" (%s)" % msg if msg else "")))
    ok = all(c[1] is not False for c in checks)
    out("readiness %s: %s" % (scenario, "PASS" if ok else "FAIL"))
    return 0 if ok else 1


def oracle(scenario, cfg, out=print):
    """Run the scenario's scripted MCP calls in order; PASS once their concatenated text
    output matches signal_regex. An argument value "$trace_id" runs that call once per
    trace_id seen in earlier output, in order, until the signal is reached; "$span_id" runs it
    once per 16-hex span id the earlier calls for that trace printed. Needs the
    fault active: at the default variant the signal is absent by design."""
    try:
        d = load_json(os.path.join(HARNESS, "scenarios", scenario + ".json"))
        calls, sig = d["oracle"], re.compile(d["signal_regex"], re.I)
        if not oracle_shape_ok(calls):
            raise ValueError("oracle must be a non-empty list of {tool, arguments}")
    except (OSError, ValueError, KeyError, TypeError, re.error) as e:
        out("oracle %s: FAIL - cannot read oracle and signal_regex from the scenario file: %r" % (scenario, e))
        return 1
    text, n = "", 0
    per_trace = {}  # trace id -> output of the calls made for it; "$span_id" expands from it
    try:
        s = MCPClient(config.mcp_url(cfg), timeout=20.0, client_name="jaeger-mcp-evals-harness-capture-tools", client_version="1")
        s.initialize()
        for c in calls:
            spec = json.dumps(c["arguments"])
            ids = list(dict.fromkeys(re.findall(r'"trace_id":\s*"([0-9a-fA-F]+)"', text))) if "$trace_id" in spec else [None]
            for tid in ids:
                s1 = spec.replace("$trace_id", tid) if tid else spec
                # "$span_id": one call per 16-hex span id an earlier call for this trace printed, in order
                sids = list(dict.fromkeys(re.findall(r"\b[0-9a-f]{16}\b", per_trace.get(tid, "")))) if "$span_id" in s1 else [None]
                for sid in sids:
                    args = json.loads(s1.replace("$span_id", sid) if sid else s1)
                    n += 1
                    r = s.call_tool(c["tool"], args)
                    if r["is_error"] and not (r["raw"] or {}).get("isError"):  # transport or JSON-RPC error: stop here
                        raise MCPError(r["text"])
                    text += r["text"] + "\n"
                    if tid:
                        per_trace[tid] = per_trace.get(tid, "") + r["text"] + "\n"
                    if sig.search(text):
                        out("oracle %s: PASS - /%s/ reached at call %d: %s %s" % (scenario, sig.pattern, n, c["tool"], json.dumps(args)))
                        return 0
    except Exception as e:
        out("oracle %s: FAIL - %s" % (scenario, e))
        return 1
    out("oracle %s: FAIL - the output of %d call(s) never matched /%s/" % (scenario, n, sig.pattern))
    return 1


def fisher_p(a, b, n):
    """Two-sided Fisher's exact p-value for two arms of n trials each,
    a PASS in baseline and b PASS in variant (2x2 table, row margins n/n)."""
    total = a + b
    lo, hi = max(0, total - n), min(n, total)
    prob = lambda k: math.comb(n, k) * math.comb(n, total - k) / math.comb(2 * n, total)
    obs = prob(a)
    return sum(prob(k) for k in range(lo, hi + 1) if prob(k) <= obs * 1.0000001)


def power(n, out=print):
    """For n trials per arm, the smallest PASS-count gap between baseline and
    variant that is significant at two-sided p < 0.05 by Fisher's exact test,
    per baseline count 0..n. Same arithmetic for accuracy PASS counts and for
    tool-use counts (both are just counts out of n)."""
    out("power n=%d (accuracy PASS counts and tool-use counts: same arithmetic)" % n)
    out("baseline_passes\tmin_variant_passes_significant")
    for a in range(n + 1):
        found = None
        for d in range(1, n + 1):
            for b in sorted({x for x in (a - d, a + d) if 0 <= x <= n}):
                if fisher_p(a, b, n) < 0.05:
                    found = b
                    break
            if found is not None:
                break
        out("%d\t%s" % (a, found if found is not None else "none"))
    return 0


def counted(cells, arm):
    """The cells that count for an arm, in band and in INDEX alike: those with an out_dir."""
    return [c for c in cells if c.get("arm") == arm and c.get("out_dir")]


def band_code(n, passes):
    if n < 10:
        return 2
    if passes in (0, n):
        return 3
    return 0


def wilson(k, n, z=1.96):
    """Wilson score interval for k successes out of n > 0."""
    p, z2 = k / n, z * z
    mid, half = p + z2 / (2 * n), z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return max(0.0, (mid - half) / (1 + z2 / n)), min(1.0, (mid + half) / (1 + z2 / n))


def stops(summaries):
    """Tally of stop values other than a normal completion ("success"); a stream with
    no result event counts as no_result. "-" when every run completed normally."""
    t = {}
    for s in summaries:
        if s.get("stop") != "success":
            k = s.get("stop") or "no_result"
            t[k] = t.get(k, 0) + 1
    return ",".join("%s:%d" % kv for kv in sorted(t.items())) or "-"


def arm_row(arm, scored, n):
    """One arm's numbers from (trial_dir, summary) pairs; n counts every counted cell,
    scored or not (the unscored ones are ERROR)."""
    ss = [s for _, s in scored]
    med = lambda k: (lambda xs: round(statistics.median(xs), 2) if xs else None)(
        [s.get(k) for s in ss if isinstance(s.get(k), (int, float))])
    tool_names = sorted({t for s in ss for t in (s.get("call_sequence") or [])} - {"read_skill"})
    r = {k: sum(s.get("verdict") == k for s in ss) for k in ("PASS", "PARTIAL", "FAIL", "ABSTAIN", "INVALID", "LEAK")}
    r.update(arm=arm, n=n, ERROR=n - len(ss), stops=stops(ss), calls=med("tool_calls"), steps=med("steps_to_evidence"),
             chars=med("tool_output_chars"), call_errors=sum(s.get("call_errors") or 0 for s in ss),
             rs_att=sum(bool(s.get("read_skill_attempted")) for s in ss),
             rs_ok=sum(bool(s.get("read_skill_succeeded")) for s in ss),
             tools=judge.arm_metrics([dict(s, arm=arm) for s in ss], arm, tool_names)["tool_used"],
             compacted=[(d, s["compaction_events"]) for d, s in scored if (s.get("compaction_events") or 0) > 0])
    return r


def results(scenario, rows, out, detail=lambda m: None):
    """The results table, one row per arm; detail() gets the full key=value lines.
    Returns the worst band code: 0 certified, 2 rank only, 3 rate 0 or 1."""
    code, warn = 0, []
    for r in rows:
        n = r["n"]
        detail("%s: n=%d PASS=%d PARTIAL=%d FAIL=%d ABSTAIN=%d ERROR=%d INVALID=%d LEAK=%d stops=%s median_tool_calls=%s "
               "median_steps_to_evidence=%s median_tool_output_chars=%s total_call_errors=%d read_skill_attempted=%d/%d "
               "read_skill_succeeded=%d/%d" % (r["arm"], n, r["PASS"], r["PARTIAL"], r["FAIL"], r["ABSTAIN"], r["ERROR"], r["INVALID"], r["LEAK"],
                                               r["stops"], r["calls"], r["steps"], r["chars"], r["call_errors"],
                                               r["rs_att"], n, r["rs_ok"], n))
        detail("band %s/%s: n=%d passes=%d pass_rate=%.2f%s ERROR=%d INVALID=%d LEAK=%d stops=%s read_skill_attempted=%d/%d "
               "read_skill_succeeded=%d/%d tools=%s" % (
                   scenario, r["arm"], n, r["PASS"], r["PASS"] / n if n else 0.0,
                   " ci95=[%.2f,%.2f]" % wilson(r["PASS"], n) if n else "", r["ERROR"], r["INVALID"], r["LEAK"], r["stops"], r["rs_att"], n,
                   r["rs_ok"], n, ",".join("%s:%d" % kv for kv in r["tools"].items()) or "-"))
        warn += ["WARNING: %s: %s compaction event(s) in stream.jsonl" % c for c in r["compacted"]]
        if r["INVALID"]:
            warn.append("WARNING: %s: %d/%d runs INVALID (sandbox check failed, see sandbox_violations); never a pass"
                        % (r["arm"], r["INVALID"], n))
        if r["LEAK"]:
            warn.append("WARNING: %s: %d/%d runs LEAK (the answer names a flag, see leak_hits); never a pass"
                        % (r["arm"], r["LEAK"], n))
        if r["compacted"]:
            # A warning, not a failure: the rate stands, but a compacted run's
            # context numbers are not comparable with an uncompacted one's.
            warn.append("WARNING: %d/%d counted runs contain a compaction event; their context numbers are not comparable"
                        % (len(r["compacted"]), n))
        code = max(code, band_code(n, r["PASS"]))
    ns = sorted({r["n"] for r in rows})
    note = "n=%s per arm" % ", ".join(map(str, ns))
    if ns and ns[0] < 10:
        note += "; " + paint(YELLOW, "below certification threshold (10); rank only")
    out("\n" + paint(BOLD_CYAN, "Results") + "  (" + note + ")")
    num = lambda x: "-" if x is None else format(int(x) if x == int(x) else x, ",")
    head = ("arm", "pass", "partial", "fail", "abstain", "err", "invalid", "leak", "pass rate", "95% CI", "calls", "output chars")
    cells = [(r["arm"], "%d/%d" % (r["PASS"], r["n"]), str(r["PARTIAL"]), str(r["FAIL"]), str(r["ABSTAIN"]), str(r["ERROR"]),
              str(r["INVALID"]), str(r["LEAK"]),
              "%.2f" % (r["PASS"] / r["n"]) if r["n"] else "-",
              "[%.2f, %.2f]" % wilson(r["PASS"], r["n"]) if r["n"] else "-", num(r["calls"]), num(r["chars"])) for r in rows]
    w = [max(map(len, col)) for col in zip(head, *cells)]
    pad = lambda xs: [x.ljust(w[0]) if i == 0 else x.rjust(w[i]) for i, x in enumerate(xs)]
    out("  " + paint(DIM, "  ".join(pad(head))))
    for r, xs in zip(rows, cells):
        xs = pad(xs)
        if r["n"]:
            rate = r["PASS"] / r["n"]
            xs[8] = paint(GREEN if rate == 1 else BOLD_RED if rate == 0 else YELLOW, xs[8])
        out("  " + "  ".join(xs))
        out("    tools used: " + (", ".join("%s %d" % kv for kv in r["tools"].items()) or "none"))
        extra = (["steps to evidence %s (median)" % num(r["steps"])] if r["steps"] is not None else []) + \
                (["stops " + r["stops"]] if r["stops"] != "-" else []) + \
                (["read_skill attempted %d/%d, succeeded %d/%d" % (r["rs_att"], r["n"], r["rs_ok"], r["n"])] if r["rs_att"] else []) + \
                (["call errors %d" % r["call_errors"]] if r["call_errors"] else [])
        if extra:
            out("    " + paint(DIM, " \u00b7 ".join(extra)))
    if code == 3:
        warn.append("read the logs: 0%/100% usually means a leaky prompt or an unfair assertion")
    for m in warn:
        out(paint(YELLOW, m))
    return code


def band(batch_dir, arm=None, out=None, detail=lambda m: None):
    out = out or printer(sys.stdout)
    cells_file = os.path.join(batch_dir, "cells.jsonl")
    if not os.path.isfile(cells_file):
        out("band: no such file: %s" % cells_file)
        return 2
    cells = read_jsonl(cells_file)
    scenario = load_json(os.path.join(batch_dir, "manifest.json"))["scenario"]
    rows = []
    for a in [arm] if arm else sorted({c.get("arm") for c in cells if c.get("arm")}):
        dirs = [os.path.join(batch_dir, c["out_dir"]) for c in counted(cells, a)]
        scored = []
        for d in dirs:
            try:
                scored.append((d, score.score(d)[0]))
            except Exception as e:
                print("band %s/%s: WARN could not score %s: %s" % (scenario, a, d, e), file=sys.stderr)
        rows.append(arm_row(a, scored, len(dirs)))
    return results(scenario, rows, out, detail)


# ---- batch --------------------------------------------------------------------

class Log:
    """The run's one screen stream (stderr), teed without colour into batch.log once the batch
    dir exists (earlier lines are replayed). detail() lines go to the file only; fail() and
    abort() put the current step's detail on screen too, so a failure is always explained."""

    def __init__(self):
        self.stream = sys.stderr
        self.tty, self.color = self.stream.isatty(), use_color(self.stream)
        self.lines, self.path, self.step, self.live_on = [], None, [], False

    def file(self, msg):
        msg = ANSI.sub("", msg)
        self.lines.append(msg)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(msg + "\n")

    def screen(self, msg, end="\n"):
        clear = "\r\x1b[K" if self.live_on else ""
        self.live_on = end == ""
        self.stream.write(clear + (msg if self.color else ANSI.sub("", msg)) + end)
        self.stream.flush()

    def __call__(self, msg):
        self.screen(msg)
        self.file(msg)

    def detail(self, msg):
        self.step.append(msg)
        self.file(msg)

    def live(self, msg):
        """A line redrawn in place; TTY only, never in the file."""
        if self.tty:
            self.screen(msg, end="")

    def section(self, title, tail=""):
        self.step = []
        self("\n" + paint(BOLD_CYAN, title) + tail)

    def check(self, ok, text):
        """An ok line, or a FAIL line followed by the step's detail."""
        self("  " + (paint(GREEN, "ok  ") if ok else paint(BOLD_RED, "FAIL")) + "  " + text)
        if ok:
            self.step = []
        else:
            self.abort(None)
        return ok

    def abort(self, msg):
        """The current step's detail on screen, then msg in red."""
        for m in self.step:
            self.screen("        " + m)
        self.step = []
        if msg:
            self(paint(BOLD_RED, msg))

    def open(self, path):
        self.path = path
        with open(path, "w", encoding="utf-8") as f:
            f.write("".join(line + "\n" for line in self.lines))


ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
BOLD_CYAN, GREEN, BOLD_RED, YELLOW, MAGENTA, CYAN, DIM = "1;36", "32", "1;31", "33", "35", "36", "2"
VERDICT_COLOR = {"PASS": GREEN, "PARTIAL": YELLOW, "FAIL": BOLD_RED, "ERROR": BOLD_RED, "INVALID": BOLD_RED, "LEAK": BOLD_RED,
                 "ABSTAIN": MAGENTA, "UNGRADED": CYAN}


def use_color(stream):
    if os.environ.get("FORCE_COLOR", "") not in ("", "0"):
        return True
    return not os.environ.get("NO_COLOR") and stream.isatty()


def paint(code, text):
    """ANSI always; each writer strips it when its stream gets no colour."""
    return "\x1b[%sm%s\x1b[0m" % (code, text)


def printer(stream):
    color = use_color(stream)
    return lambda msg: print(msg if color else ANSI.sub("", msg), file=stream, flush=True)


def shown(path):
    """A path for the screen: relative when that is shorter and stays near here."""
    rel = os.path.relpath(path)
    return rel if len(rel) < len(path) and not rel.startswith(os.path.join("..", "..")) else path


def dur(s):
    return "%dm%02ds" % divmod(int(s), 60) if s >= 60 else "%ds" % int(s)


def write_mcp_config(path, mcp_url):
    """The cell's mcp.json: the real, host-including URL (never a record file), or no server."""
    servers = {"jaeger": {"type": "http", "url": mcp_url}} if mcp_url else {}
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"mcpServers": servers}, separators=(",", ":")))


MEMORY_MARKERS = ("CLAUDE.md", "# CLAUDE", "auto-memory", "MEMORY.md")
PROBE_TIMEOUT_S = 90


def sandbox_probe(argv, env, expected, timeout=PROBE_TIMEOUT_S):
    """Run a trial's CLI argv against a server on 127.0.0.1 that keeps the first POST body in memory
    and answers 400, so no model is called. Returns (problems, tool names sent). The body is never
    written anywhere: with a claude.ai login it carries the account's email."""
    first = []

    class Probe(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if not first:
                first.append(body)
            reply = b'{"type":"error","error":{"type":"invalid_request_error","message":"sandbox probe"}}'
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Probe)
    threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as work:
            try:
                subprocess.run(argv + ["--no-session-persistence"], cwd=work, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout,
                               env=dict(env, ANTHROPIC_BASE_URL="http://127.0.0.1:%d" % srv.server_address[1]))
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
        return ["no model request with a JSON body reached the probe within %ds" % timeout], []
    problems = [] if sorted(names) == sorted(expected) else ["tools sent %s, expected %s" % (sorted(names), sorted(expected))]
    problems += ["server-side tool %s (type %s)" % (t.get("name"), t["type"]) for t in sent if "type" in t]
    text = json.dumps([req.get("system"), req.get("messages")], ensure_ascii=False)
    problems += ["memory marker %r in the request" % m for m in MEMORY_MARKERS if m in text]
    return problems, names


def claude_argv(a, prompt_file, mcp_config):
    """The Claude Code CLI call. Text args are passed like bash's "$(cat f)", which strips
    trailing newlines."""
    text = lambda p: read_text(p).rstrip("\n")
    return ["claude", "-p", text(prompt_file),
            "--output-format", "stream-json", "--verbose",
            "--mcp-config", mcp_config, "--strict-mcp-config",
            "--setting-sources", "", "--restricted", "--tools", "",
            "--allowedTools", "mcp__jaeger__*",
            "--system-prompt", text(SYSTEM_PROMPT),
            "--json-schema", text(VERDICT_SCHEMA),
            "--model", a.model, "--effort", a.effort, "--max-turns", str(a.max_turns), "--max-budget-usd", a.max_budget_usd]


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


def client_argv(a, arm, prompt_file, mcp_config, trial):
    if a.client == "cli":
        return claude_argv(a, prompt_file, mcp_config)
    if a.client == "codex":
        return [sys.executable, CODEX_CLIENT, "--model", a.model, "--effort", a.effort, "--max-turns", str(a.max_turns),
                "--prompt-file", prompt_file, "--system-prompt-file", SYSTEM_PROMPT, "--schema-file", VERDICT_SCHEMA,
                "--mcp-config", mcp_config, "--out-dir", trial, "--codex-bin", a.codex_bin]
    return [sys.executable, AGENT_LOOP, "--scenario", a.scenario, "--arm", arm, "--model", a.model,
            "--prompt-file", prompt_file, "--system-prompt-file", SYSTEM_PROMPT, "--schema-file", VERDICT_SCHEMA,
            "--scenario-file", os.path.join(HARNESS, "scenarios", a.scenario + ".json"), "--mcp-config", mcp_config,
            "--out-dir", trial, "--effort", a.effort, "--max-turns", str(a.max_turns), "--max-budget-usd", a.max_budget_usd
            ] + loop_provider(a)


def probe_argv(a):
    return [sys.executable, AGENT_LOOP, "--probe-model", "--model", a.model, "--effort", a.effort] + loop_provider(a)


def api_secrets(a, cfg):
    return {k: cfg.get(k, "") for k in (SECRET_KEYS[1:] if a.provider == "openai" else SECRET_KEYS[:1])}


def is_full_model_id(m):
    return bool(re.fullmatch(r"claude-[a-z]+(?:-[a-z0-9]+)*", m) and re.search(r"\d", m))


def loop_provider(a):
    return ["--provider", "openai"] if a.provider == "openai" else []


def observe(stream_path):
    init, compactions = {}, 0
    for line in read_text(stream_path).splitlines(keepends=True) if os.path.isfile(stream_path) else []:
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        if not init and e.get("type") == "system" and e.get("subtype") == "init":
            init = e
        compactions += score.is_compaction_event(e, line)
    return {"client_version": init.get("claude_code_version") or init.get("loop_version") or init.get("client_version"),
            "model": init.get("model"),
            "tools": init.get("tools"), "skills": init.get("skills"), "agents": init.get("agents"),
            "compaction_events": compactions}


def jaeger_commit(cfg, image):
    for p in sorted(glob.glob(os.path.join(HARNESS, "experiments", "images", "*.json"))):
        rec = load_json(p)
        if rec.get("tag") == image and rec.get("branch_commit"):
            return rec["branch_commit"]
    known = dict(kv.split("=", 1) for kv in cfg.get("KNOWN_COMMITS", "").split() if "=" in kv)
    return known.get(image) or known.get(image.rsplit(":", 1)[-1]) or "unresolved"


STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def append_line(path, rec):
    """One cells.jsonl line, written whole: stop signals wait until it is flushed."""
    signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
            f.flush()
    finally:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, STOP_SIGNALS)


EXPERIMENT_KEYS = ("name", "version", "scenario", "hypothesis", "run", "arms", "baseline_arm", "thresholds", "status")
RUN_KEYS = ("client", "provider", "model", "effort", "max_turns", "max_budget_usd", "n_per_arm", "seed")
ARM_KEYS = ("prompt", "image", "tools")


def load_experiment(path):
    """The experiment file, validated strictly; ValueError names the first problem."""
    try:
        exp = load_json(path)
    except (OSError, ValueError) as e:
        raise ValueError("cannot read %s: %s" % (path, e))

    def keys(doc, where, required, optional=()):
        if not isinstance(doc, dict):
            raise ValueError("%s must be an object" % where)
        unknown = sorted(set(doc) - set(required) - set(optional))
        missing = [k for k in required if k not in doc]
        if unknown or missing:
            raise ValueError("%s: %s" % (where, "; ".join(
                (["unknown key(s): " + ", ".join(unknown)] if unknown else []) +
                (["missing key(s): " + ", ".join(missing)] if missing else []))))

    def check(ok, msg):
        if not ok:
            raise ValueError(msg)

    is_int = lambda v: type(v) is int
    keys(exp, "experiment", EXPERIMENT_KEYS)
    check(exp["version"] == 2, "version must be 2")
    for k in ("name", "scenario", "hypothesis", "status"):
        check(isinstance(exp[k], str) and exp[k], "%s must be a non-empty string" % k)
    r = exp["run"]
    keys(r, "run", RUN_KEYS, optional=("client_version",))
    check(r["client"] in CLIENTS, "run.client must be one of %s" % ", ".join(CLIENTS))
    if r["client"] == "api":
        check(r["provider"] in ("anthropic", "openai"), "run.provider must be anthropic or openai for client api")
    else:
        check(r["provider"] is None, "run.provider applies only to client api; set it to null")
    check(isinstance(r["model"], str) and r["model"], "run.model must be a non-empty string")
    check(r["effort"] in EFFORTS, "run.effort must be one of %s" % ", ".join(EFFORTS))
    check(is_int(r["max_turns"]) and r["max_turns"] > 0, "run.max_turns must be a positive integer")
    check(type(r["max_budget_usd"]) in (int, float) and r["max_budget_usd"] > 0, "run.max_budget_usd must be a positive number")
    check(is_int(r["n_per_arm"]) and r["n_per_arm"] > 0, "run.n_per_arm must be a positive integer")
    check(r["seed"] is None or is_int(r["seed"]), "run.seed must be an integer or null")
    if "client_version" in r:
        check(isinstance(r["client_version"], str) and r["client_version"], "run.client_version must be a non-empty string")
    check(isinstance(exp["arms"], dict) and exp["arms"], "arms must be a non-empty object")
    for name, arm in exp["arms"].items():
        keys(arm, "arms.%s" % name, ARM_KEYS)
        for k in ("prompt", "image"):
            check(isinstance(arm[k], str) and arm[k], "arms.%s.%s must be a non-empty string" % (name, k))
        check(type(arm["tools"]) is bool, "arms.%s.tools must be true or false" % name)
    check(exp["baseline_arm"] is None or exp["baseline_arm"] in exp["arms"], "baseline_arm must name an arm or be null")
    check(isinstance(exp["thresholds"], dict), "thresholds must be an object")
    return exp


def run(a, cfg):
    log = Log()

    def die(msg):
        log.abort("bench: " + msg)
        return 1

    try:
        exp = load_experiment(a.experiment_file)
    except ValueError as e:
        return die("experiment file: %s" % e)
    exp_sha = sha256_file(a.experiment_file)
    if "DRAFT" in exp["status"]:
        return die("experiment status is still DRAFT; set it to CONFIRMED before running it")
    # The file's run block is the only source of these settings.
    vars(a).update(exp["run"], scenario=exp["scenario"], experiment=exp["name"])
    a.max_budget_usd = str(a.max_budget_usd)
    budget = float(a.max_budget_usd)
    a.codex_bin = cfg.get("CODEX_BIN") or "codex"
    sf = os.path.join(HARNESS, "scenarios", a.scenario + ".json")
    if not os.path.isfile(sf):
        return die("no such scenario file: %s" % sf)
    scen = load_json(sf)
    flag, act = scen.get("flag"), scen.get("activation") or {}
    gt_service = (scen.get("ground_truth") or {}).get("service", "")
    signal_re = scen.get("signal_regex") or ""
    # A slow-building fault can set signal_wait_s; the default is 48 polls of 10 s.
    poll_max = max(1, int(scen.get("signal_wait_s", TRACE_POLL_MAX * TRACE_POLL_SLEEP)) // TRACE_POLL_SLEEP)
    expected_restarts = scen.get("expected_restarts") or []
    signal_after_restart = scen.get("signal_after_restart")
    if not flag:
        return die("scenario %s: no 'flag' field" % a.scenario)
    if act.get("field") != "defaultVariant":
        return die("scenario %s uses activation.field=%r; only 'defaultVariant' activation is implemented "
                   "(targeting-based activation is not)" % (a.scenario, act.get("field")))
    if not act.get("value"):
        return die("scenario %s: activation.value is empty" % a.scenario)

    prompt_for = {n: arm["prompt"] for n, arm in exp["arms"].items()}
    image_for = {n: arm["image"] for n, arm in exp["arms"].items()}
    tools_for = {n: arm["tools"] for n, arm in exp["arms"].items()}
    prompt_file = {arm: os.path.join(HARNESS, "prompts", p + ".txt") for arm, p in prompt_for.items()}
    for arm, p in prompt_file.items():
        if not os.path.isfile(p):
            return die("arm %r has no prompt file %s" % (arm, p))

    log.detail("bench: experiment=%s scenario=%s flag=%s activation=defaultVariant:%s client=%s model=%s effort=%s n_per_arm=%d"
               % (a.experiment, a.scenario, flag, act["value"], a.client, a.model, a.effort, a.n_per_arm))
    log(paint("1", " \u00b7 ".join((a.experiment, a.scenario, " ".join(filter(None, (a.client, a.provider, a.model, a.effort))),
                                     "%d per arm" % a.n_per_arm))))
    log.section("Pre-flight")

    # ---- pre-flight (read-only) ----
    pre = {"leak": None, "readiness": None, "containers": None, "baseline_traces": None,
           "fixture_leak_baseline": None, "fixture_leak_under_fault": None, "oracle": None, "client": None,
           "client_version": None, "sandbox_probe": None, "leak_rendered": None, "thrash": None,
           "restarted_before_run": None}
    if a.client == "api" and a.provider == "openai":
        log.detail("== pre-flight: api client, provider openai (OPENAI_API_KEY, OPENAI_BASE_URL, model) ==")
        if not (cfg.get("OPENAI_API_KEY") and cfg.get("OPENAI_BASE_URL")):
            return die("set OPENAI_API_KEY and OPENAI_BASE_URL in fixture.env for provider openai")
    elif a.client == "api":
        log.detail("== pre-flight: api client (ANTHROPIC_API_KEY, model and effort) ==")
        if not cfg.get("ANTHROPIC_API_KEY"):
            return die("set ANTHROPIC_API_KEY in fixture.env (or set run.client to cli to use the Claude Code CLI)")
    elif a.client == "cli":
        log.detail("== pre-flight: cli client (claude flags) ==")
        found = claude_missing_flags()
        if found is None:
            return die("claude is not on PATH; install the Claude Code CLI and log in (run `claude` once), or pick another run.client")
        if found[1]:
            return die("ABORT - Claude Code CLI %s does not accept %s; update with `claude update`; nothing was touched"
                       % (found[0], " ".join(found[1])))
        log.check(True, "claude accepts the flags the harness passes")
        if not is_full_model_id(a.model):
            log("        WARNING: run.model %r is an alias; the model the stream reports is recorded per trial, not pinned" % a.model)
        if cfg.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"):
            log("        ANTHROPIC_API_KEY is set; the cli client runs on the logged-in plan and ignores it")
    elif a.client == "codex":
        log.detail("== pre-flight: codex client (%s on PATH) ==" % a.codex_bin)
        if not shutil.which(a.codex_bin):
            return die("%s is not on PATH; install the OpenAI Codex CLI (or the wrapper named by CODEX_BIN) and "
                       "log in, or pick another run.client" % a.codex_bin)
        log.check(True, "%s on PATH" % a.codex_bin)
    if a.client == "api":
        r = subprocess.run([sys.executable, AGENT_LOOP, "--validate-only", "--model", a.model, "--effort", a.effort,
                            "--max-turns", str(a.max_turns)] + loop_provider(a),
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            return die("ABORT - the api client refuses these arguments: %s" % r.stderr.strip())
        pre["client"] = "PASS"
        log.check(True, "api client (%s) accepts %s at effort %s" % (a.provider, a.model, a.effort))
        log.detail("pre-flight: model pin probe: one call, prompt 'Reply with the word ok.', max_tokens 256, no reasoning")
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE",) + SECRET_KEYS}
        r = subprocess.run(probe_argv(a), capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300,
                           env=dict(env, **api_secrets(a, cfg)))
        seen = re.search(r"endpoint reports model (\S+)", r.stdout)
        if r.returncode == 3 and seen:
            return die("ABORT - model pin: run.model is %s but the endpoint answered as %s; nothing was touched"
                       % (a.model, seen.group(1)))
        if r.returncode != 0:
            return die("ABORT - the model probe failed: %s" % (r.stderr.strip() or r.stdout.strip()))
        log.check(True, "endpoint answers as %s" % a.model)
    try:
        client_pre = subprocess.run({"cli": ["claude", "--version"], "codex": [a.codex_bin, "--version"]}.get(
                                        a.client, [sys.executable, AGENT_LOOP, "--version"]),
                                    capture_output=True, text=True,
                                    stdin=subprocess.DEVNULL, timeout=60).stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        client_pre = None
    want = exp["run"].get("client_version")
    if want:
        # "2.1.283 (Claude Code)", "codex-cli 0.153.2", "agent_loop 1": the first token starting with a digit
        got = next((t for t in (client_pre or "").split() if t[:1].isdigit()), None)
        pre["client_version"] = "PASS" if got == want else "FAIL"
        if not log.check(got == want, "client version %s" % want):
            return die("ABORT - run.client_version is %s but the %s client reports %s; nothing was touched"
                       % (want, a.client, got or client_pre or "no version"))
    try:
        grader_cli = subprocess.run([grade.CLAUDE, "--version"], capture_output=True, text=True,
                                    stdin=subprocess.DEVNULL, timeout=60).stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        grader_cli = None
    if not log.check(bool(grader_cli), "mechanism grader %s via %s" % (grade.GRADER_MODEL, grader_cli or "no Claude Code CLI")):
        return die("ABORT - the mechanism grader needs the Claude Code CLI (%s --version failed); nothing was touched" % grade.CLAUDE)
    log.detail("== pre-flight: readiness ==")
    pre["readiness"] = "PASS" if readiness(a.scenario, out=log.detail) == 0 else "FAIL"
    log.check(pre["readiness"] == "PASS", "scenario %s ready" % a.scenario)
    log.detail("== pre-flight: leak scan (system prompt + arm prompts) ==")
    pre["leak"] = "PASS" if leak([SYSTEM_PROMPT] + sorted(set(prompt_file.values())), out=log.detail) == 0 else "FAIL"
    log.check(pre["leak"] == "PASS", "prompts leak-free")
    where = "%s: %s" % (fixture_where(cfg), cfg["FIXTURE_DEMO_DIR"])
    log.detail("== pre-flight: fixture reachable (%s) ==" % where)
    fx_ok = fixture_sh(cfg, "true", timeout=30).returncode == 0
    log.detail("pre-flight: fixture reachable" if fx_ok else "pre-flight: FAIL - fixture unreachable or no demo dir: %s" % where)
    log.check(fx_ok, "fixture reachable " + paint(DIM, "(%s)" % where))
    if not (fx_ok and pre["leak"] == "PASS" and pre["readiness"] == "PASS"):
        return die("ABORT - pre-flight failed (see above), nothing was touched")

    # Observed, not typed: what the fixture and this machine are running right now.
    r = fixture_sh(cfg, "docker inspect jaeger --format '{{.Config.Image}} {{.Image}}'")
    parts = r.stdout.split()
    if r.returncode != 0 or len(parts) != 2:
        return die("ABORT - could not read the running jaeger image (docker inspect jaeger): %s" % r.stderr.strip())
    image, image_id = parts
    log.detail("pre-flight: fixture runs jaeger image %s (%s)" % (image, image_id))
    # One batch runs only the arms pinned to the running image; the others need the fixture switched first.
    arms = [n for n in prompt_for if image_for[n] == image]
    if not arms:
        return die("ABORT - the fixture runs %s, which no arm of experiment %s expects (%s); point the fixture at "
                   "an arm's image first" % (image, a.experiment, ", ".join("%s=%s" % kv for kv in image_for.items())))
    log.detail("pre-flight: experiment arms for this image: %s" % " ".join(arms))
    log.check(True, "jaeger image %s %s" % (image, paint(DIM, image_id[:19])))
    others = ["%s needs %s" % (n, image_for[n]) for n in prompt_for if n not in arms]
    log("        arms here: %s%s" % (" ".join(arms), paint(DIM, "  (%s)" % "; ".join(others)) if others else ""))
    any_tools = any(tools_for[arm] for arm in arms)
    r = fixture_sh(cfg, "git rev-parse HEAD")
    otel_demo_ref = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "unresolved"
    r = fixture_sh(cfg, "sha256sum overlay/* .env.override")
    overlay_sha = hashlib.sha256(r.stdout.encode()).hexdigest() if r.returncode == 0 else None
    # Same overlay without the *.bak files make setup writes and without the image line, so two arms on
    # different images hash equal (judge.py's cross-experiment check). POSIX sh only: ssh login shells vary.
    r = fixture_sh(cfg, "{ sha256sum $(ls overlay/* | grep -v '\\.bak$'); "
                        "grep -v '^JAEGERTRACING_IMAGE=' .env.override | sha256sum; }")
    overlay_sha_sans_image = hashlib.sha256(r.stdout.encode()).hexdigest() if r.returncode == 0 else None
    git = lambda *args: subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True).stdout.strip()
    harness_sha, harness_dirty = git("rev-parse", "HEAD"), bool(git("status", "--porcelain", "--", "harness", "fixture"))

    # Tool list the model will see, and the pre-registered description check.
    # mcp_url (host included) is for the live connection only; mcp_endpoint (host-free)
    # is what gets recorded, since FIXTURE_HOST must never land in records/.
    mcp_url = config.mcp_url(cfg) if any_tools else None
    mcp_endpoint = config.mcp_endpoint(cfg) if any_tools else None
    tool_list = tools_text = tools_sha = None
    desc = {}
    for arm in arms:
        f = None
        base = exp["baseline_arm"]
        if base and arm != base and tools_for[arm] and image_for[arm] != image_for.get(base):
            # A variant image's record (experiments/images/*.json, matched by tag) may say which
            # descriptions it serves: a file under descriptions/, or null for the stock wording
            # (recorded only). Without that key, desc-change.json, what build-variant.sh bakes in.
            rec = next((r for r in map(load_json, glob.glob(os.path.join(HARNESS, "experiments", "images", "*.json")))
                        if r.get("tag") == image_for[arm]), {})
            name = rec.get("descriptions", "desc-change.json")
            f = os.path.join(HARNESS, "experiments", "descriptions", name) if name else None
            f = f if f and os.path.isfile(f) else None
        desc[arm] = {"file": repo_relpath(f) if f else None, "sha256": sha256_file(f) if f else None,
                     "check": "compared" if f else "recorded_only" if tools_for[arm] else "no_tools", "path": f}
    if not any_tools:
        log.detail("pre-flight: no arm here has tools - no MCP server configured; tools/list capture, description check and oracle skipped")
        log("  " + paint(DIM, "skip  no arm here has tools: tools/list, description check and oracle"))
    else:
        log.detail("== pre-flight: capture MCP tools/list (%s) ==" % mcp_url)
        try:
            tool_list = tools.list_tools(mcp_url, 20.0)
        except MCPError as e:
            return die("ABORT - could not capture tools/list: %s" % e)
        tools_text = tools.tools_text(tool_list)
        tools_sha = hashlib.sha256(tools_text.encode("utf-8")).hexdigest()
        log.detail("pre-flight: %d tools captured, sha256 %s" % (len(tool_list), tools_sha))
        log.check(True, "%d MCP tools served %s" % (len(tool_list), paint(DIM, "(sha256 %s)" % tools_sha[:12])))
        for arm in arms:
            if not desc[arm]["path"]:
                continue
            log.detail("pre-flight: arm %s: served descriptions against %s" % (arm, desc[arm]["file"]))
            if tools.report(tool_list, tools.expected_descriptions(load_json(desc[arm]["path"])), log.detail):
                return die("ABORT - the fixture is not serving the pre-registered tool descriptions for arm %r" % arm)
            log.check(True, "arm %s: descriptions match %s" % (arm, os.path.basename(desc[arm]["file"])))

    log.detail("== pre-flight: leak scan of the rendered prompts, system prompt and served tools/list ==")
    rendered = []
    for arm in arms:
        argv = claude_argv(a, prompt_file[arm], "-")
        rendered.append(("rendered prompt %s" % arm, argv[2]))
    rendered.append(("rendered system prompt", argv[argv.index("--system-prompt") + 1]))
    if tools_text:
        rendered.append(("served tools/list", TOOLS_ALLOW.sub("", tools_text)))
    pre["leak_rendered"] = "PASS" if leak_texts(rendered, out=log.detail) == 0 else "FAIL"
    if not log.check(pre["leak_rendered"] == "PASS", "rendered prompts and served tool descriptions leak-free"):
        return die("ABORT - a leak word in what the agent would see (see above); the flag was not touched")

    if a.client == "cli":
        # Free and read-only, so a dry run does it too: the exact trial argv, answered by a local 400.
        log.detail("== pre-flight: sandbox probe (each arm's trial argv against a local server; no model call) ==")
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE",) + SECRET_KEYS}
        probe = {"result": "PASS", "tools": {}}
        for arm in arms:
            expected = [score.VERDICT_TOOL] + ([score.JAEGER_PREFIX + t["name"] for t in tool_list] if tools_for[arm] else [])
            with tempfile.TemporaryDirectory() as tmp:
                write_mcp_config(os.path.join(tmp, "mcp.json"), mcp_url if tools_for[arm] else None)
                problems, probe["tools"][arm] = sandbox_probe(claude_argv(a, prompt_file[arm], os.path.join(tmp, "mcp.json")),
                                                              env, expected)
            for m in problems:
                log.detail("sandbox probe %s: FAIL - %s" % (arm, m))
            probe["result"] = "FAIL" if problems else probe["result"]
        pre["sandbox_probe"] = probe
        if not log.check(probe["result"] == "PASS", "sandbox probe: the CLI sends only its verdict tool and the Jaeger tools"):
            return die("ABORT - the sandbox probe failed (see above); the flag was not touched")

    log.detail("== pre-flight: container count (need >= %s) ==" % cfg["MIN_CONTAINERS"])
    r = fixture_sh(cfg, "docker ps -q | wc -l")
    try:
        pre["containers"] = int(r.stdout.strip())
    except ValueError:
        return die("ABORT - could not read container count")
    if pre["containers"] < int(cfg["MIN_CONTAINERS"]):
        return die("ABORT - only %d running containers (need >= %s)" % (pre["containers"], cfg["MIN_CONTAINERS"]))
    log.detail("pre-flight: container count OK (%d)" % pre["containers"])
    log.check(True, "%d containers running (need >= %s)" % (pre["containers"], cfg["MIN_CONTAINERS"]))

    log.detail("== pre-flight: host thrash (vmstat 5 3, cat /proc/loadavg) ==")
    r = fixture_sh(cfg, "vmstat 5 3; echo @@loadavg@@; cat /proc/loadavg", timeout=30)
    vmstat_out, _, loadavg_out = r.stdout.partition("@@loadavg@@")  # not "---": vmstat's header contains it
    bi_mean = vmstat_bi_mean(vmstat_out)
    pre["thrash"] = {"bi_mean_non_first": bi_mean, "loadavg": loadavg_out.strip() or None}
    log.detail("pre-flight: vmstat bi mean(non-first)=%s loadavg=%s" % (bi_mean, pre["thrash"]["loadavg"]))
    if not log.check(bi_mean is not None and bi_mean <= 50000, "host not thrashing (vmstat bi mean %s <= 50000)" % bi_mean):
        return die("ABORT - host thrashing or vmstat unreadable: bi mean %s (limit 50000, non-first samples)" % bi_mean)

    pristine, flagfile = shlex.quote(cfg["FIXTURE_PRISTINE_FLAG_FILE"]), shlex.quote(cfg["FIXTURE_FLAG_FILE"])
    log.detail("== pre-flight: flag %r currently at default per OFREP ==" % flag)
    r = fixture_sh(cfg, "cat %s" % pristine)
    try:
        pristine_doc = json.loads(r.stdout)
        default_variant = pristine_doc["flags"][flag]["defaultVariant"]
    except (ValueError, KeyError, TypeError):
        return die("ABORT - could not read defaultVariant for %r from the fixture's pristine flag file" % flag)
    current = ofrep_variant(cfg, flag)
    if current != default_variant:
        return die("ABORT - OFREP reports %r=%r, default is %r" % (flag, current, default_variant))
    log.detail("pre-flight: flag %r at default (%r) - OK" % (flag, default_variant))
    log.check(True, "flag %s at default (%s)" % (flag, default_variant))

    if scen.get("restart_before_run"):
        log.detail("== pre-flight: restart %s, then jaeger (so jaeger never records the restart) ==" % scen["restart_before_run"])
        try:
            pre["restarted_before_run"] = restart_before_run(cfg, scen["restart_before_run"], gt_service, TRACE_POLL_MAX,
                                                             out=lambda i, p: log.detail("  poll %d: waiting on %s" % (i, p)))
        except RuntimeError as e:
            return die("ABORT - %s; nothing was flipped" % e)
        log.check(True, "restarted %s, then jaeger" % ", ".join(scen["restart_before_run"]))
    log.detail("== pre-flight: baseline traffic (service=%s needs >= 3 traces in the last %ds) ==" % (gt_service, BASELINE_LOOKBACK_S))
    # After a restart the store starts empty, so wait for traffic within the TRACE_POLL_MAX budget.
    for i in range(TRACE_POLL_MAX if pre["restarted_before_run"] else 1):
        try:
            pre["baseline_traces"] = len(traces(cfg, gt_service, now_us() - BASELINE_LOOKBACK_S * 10**6, now_us(), 20, 10))
        except Exception:
            pre["baseline_traces"] = 0
        if pre["baseline_traces"] >= 3:
            break
        if pre["restarted_before_run"]:
            log.detail("  poll %d: %d baseline traces so far" % (i + 1, pre["baseline_traces"]))
            sleep(TRACE_POLL_SLEEP)
    if pre["baseline_traces"] < 3:
        return die("ABORT - only %d baseline traces for service=%s; the fixture is up but not completing requests "
                   "through it. Check upstream services before flipping any flag." % (pre["baseline_traces"], gt_service))
    log.detail("pre-flight: baseline traffic OK (%d traces)" % pre["baseline_traces"])
    log.check(True, "%s traffic: %d traces in the last %d min" % (gt_service, pre["baseline_traces"], BASELINE_LOOKBACK_S // 60))

    log.detail("== pre-flight: fixture leak scan (what the fixture serves must not name a flag) ==")
    rc = fixture_leak.scan_live(list(pristine_doc["flags"]), 300, out=log.detail)
    pre["fixture_leak_baseline"] = "PASS" if rc == 0 else "FAIL"
    if not log.check(rc == 0, "fixture telemetry names no flag"):
        return die("ABORT - the fixture serves telemetry that names a flag (or Jaeger was unreadable); apply fixture/apply-deflag.sh first")
    log.detail("bench: pre-flight PASS")

    # ---- manifest ----
    batch_id = "batch-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bdir = os.path.join(runs_root(cfg), a.scenario, batch_id)
    cells_file = os.path.join(bdir, "cells.jsonl")
    if not a.dry_run:
        os.makedirs(bdir)
        log.open(os.path.join(bdir, "batch.log"))
        open(cells_file, "w").close()
        if tools_text:
            with open(os.path.join(bdir, "tools.json"), "w", encoding="utf-8") as f:
                f.write(tools_text)
        write_json(os.path.join(bdir, "preflight.json"), pre)

    seed = a.seed if a.seed is not None else random.SystemRandom().randint(0, 2**31 - 1)
    order = [{"arm": arm, "trial_index": i} for arm in arms for i in range(a.n_per_arm)]
    random.Random(seed).shuffle(order)
    for i, c in enumerate(order):
        c["order_index"] = i
    hashes = {name: sha256_file(os.path.join(HARNESS, name)) for name in
              ("bench.py", "tools.py", "fixture_leak.py", "score.py", "system-prompt.txt", "verdict-schema.json",
               "agent_loop.py", "mcp_client.py", "codex_client.py", "leak-words.txt", "judge.py", "grade.py", "grader-prompt.txt",
               "providers/__init__.py", "providers/base.py",
               "providers/anthropic_provider.py", "providers/openai_provider.py")}
    hashes["scenario_file"] = sha256_file(sf)
    for arm in arms:
        hashes["prompts/%s.txt" % prompt_for[arm]] = sha256_file(prompt_file[arm])
    sut = {"jaeger_image": image, "jaeger_image_digest": image_id, "jaeger_commit": jaeger_commit(cfg, image),
           "otel_demo_ref": otel_demo_ref, "agent_client": client_pre,
           "harness_git_sha": harness_sha + ("-dirty" if harness_dirty else ""),
           "python_version": platform.python_version(), "pinned_at_utc": utc()}
    common = {
        "schema_version": score.SCHEMA_VERSION, "client": a.client, "provider": a.provider, "batch_id": batch_id, "seed": seed,
        "scenario": a.scenario, "scenario_sha256": hashes["scenario_file"], "scenario_version": scen.get("version"),
        "system_prompt_sha256": hashes["system-prompt.txt"], "verdict_schema_sha256": hashes["verdict-schema.json"],
        "model_requested": a.model, "effort": a.effort, "max_turns": a.max_turns, "max_budget_usd": budget,
        "history_policy": HISTORY_POLICY[a.client], "client_version_pre": client_pre,
        "mcp_endpoint": mcp_endpoint, "tools_list_sha256": tools_sha, "tools_count": len(tool_list) if tool_list else None,
        "jaeger_image": image, "jaeger_image_id": image_id, "jaeger_commit": sut["jaeger_commit"],
        "otel_demo_ref": otel_demo_ref, "fixture_overlay_sha256": overlay_sha,
        "fixture_overlay_sha256_sans_image": overlay_sha_sans_image,
        "fault": {"flag": flag, "activation_field": "defaultVariant", "activation_value": act["value"]},
        "flag_names": sorted(pristine_doc["flags"]), "preflight": pre,
        "experiment": {"name": a.experiment, "file": repo_relpath(a.experiment_file), "sha256": exp_sha},
        "harness_git_sha": harness_sha, "harness_dirty": harness_dirty,
        "score_py_sha256": hashes["score.py"],
    }
    manifest = {"scenario": a.scenario, "batch_id": batch_id, "n_per_arm": a.n_per_arm, "arms": arms, "seed": seed,
                "order": order, "file_hashes_sha256": hashes, "system_under_test": sut, "tools_list_sha256": tools_sha,
                "tools_list_path": "tools.json" if tools_text else None,
                "grader": {"model": grade.GRADER_MODEL, "prompt_sha256": hashes["grader-prompt.txt"], "cli_version": grader_cli}}
    # ponytail: arm_pin and tools_description_check keep their one-arm shape (judge.py reads
    # arm_pin); a batch where several experiment arms share one image records them per trial only.
    if len(arms) == 1:
        manifest["arm_pin"] = {"arm": arms[0], "expected_image": image_for[arms[0]], "sut_jaeger_image_at_run": image}
        manifest["tools_description_check"] = {"mode": desc[arms[0]]["check"], "descriptions_file": desc[arms[0]]["file"],
                                               "descriptions_sha256": desc[arms[0]]["sha256"]}
    manifest.update(common)
    manifest["experiment"] = dict(common["experiment"], content=exp)
    if not a.dry_run:
        write_json(os.path.join(bdir, "manifest.json"), manifest)
    log.detail("bench: manifest %s: %s (seed=%d, %d arms x %d = %d cells, experiment=%s sha256=%s)" % (
        "not written (dry run)" if a.dry_run else "written", os.path.join(bdir, "manifest.json"), seed, len(arms),
        a.n_per_arm, len(order), a.experiment, exp_sha))

    plan = order[:1] if a.first_only else order
    flip_cmd = "python3 - %s %s %s" % (flagfile, shlex.quote(flag), shlex.quote(act["value"]))
    restore_cmd = "cp %s %s" % (pristine, flagfile)

    if a.dry_run:
        log.section("DRY RUN: planned cell order", paint(DIM, "  (seed=%d%s)" % (seed, ", first only" if a.first_only else "")))
        if a.seed is None:
            log("  " + paint(YELLOW, "a real run draws a new seed, so its order will differ; set run.seed to %d to run this order" % seed))
        for c in plan:
            log("  order_index=%d arm=%s trial_index=%d prompt=%s %s" % (c["order_index"], c["arm"], c["trial_index"],
                prompt_for[c["arm"]], paint(DIM, "-> %d-%s" % (c["order_index"], c["arm"]))))
        argv = client_argv(a, plan[0]["arm"], prompt_file[plan[0]["arm"]], "<trial>/mcp.json", "<trial>")
        if a.client == "cli":  # the texts themselves would fill the screen
            argv[2] = "<%s>" % os.path.basename(prompt_file[plan[0]["arm"]])
            for k, f in (("--system-prompt", SYSTEM_PROMPT), ("--json-schema", VERDICT_SCHEMA)):
                argv[argv.index(k) + 1] = "<%s>" % os.path.basename(f)
        log.section("DRY RUN: fixture commands", paint(DIM, "  (none executed)"))
        for step, text in (
                ("1", "activate fault: %s: %s  %s" % (fixture_where(cfg), flip_cmd,
                                                    paint(DIM, "(FLIP_SCRIPT on stdin sets flags.%s.defaultVariant)" % flag))),
                ("2", "confirm via OFREP: POST %s" % ofrep_url(cfg, flag)),
                ("3", "poll up to %d x %ds for >= %d %s traces matching /%s/ since the flip: GET %s/api/traces?service=%s"
                 % (poll_max, TRACE_POLL_SLEEP, TRACE_MIN_COUNT, gt_service, signal_re, config.jaeger_base(cfg), gt_service)),
                ("3b", "leak scan under the fault, then %s" % ("bench.py oracle %s" % a.scenario if any_tools else "no oracle (no arm has tools)")),
                ("4", "each cell, e.g.: %s" % shlex.join(argv)),
                ("5", "restore the pristine flag file: %s: %s" % (fixture_where(cfg), restore_cmd)),
                ("6", "confirm the restore via OFREP (same call as 2)")):
            log("  %-3s %s" % (step, text))
        log.section("DRY RUN: files", paint(DIM, "  (none written)"))
        log("  %s/" % shown(bdir))
        log("    batch.log preflight.json manifest.json tools.json cells.jsonl restore.json scores.jsonl")
        log("    <order_index>-<arm>/{prompt.txt,system-prompt.txt,mcp.json,meta.json,stream.jsonl,stderr.txt,exit.txt}")
        log("  copied to %s/: %s" % (shown(os.path.join(RECORDS, a.experiment, batch_id)), " ".join(RECORD_FILES)))
        log("\nbench: DRY RUN complete, nothing on the fixture or under RUNS_DIR was written")
        return 0

    # ---- fault, cells, restore ----
    fault = dict(common["fault"], flip_utc=None, ofrep_confirmed=False, signal_traces_seen=0,
                restart_seen_utc=None, restart_seen_after_s=None)
    st = {"ran": 0, "aborted": False}

    def fault_and_cells():
        """Returns 1 on an abort before any cell ran, else None."""
        log.section("Fault")
        log.detail("== activating fault: %s.defaultVariant = %s ==" % (flag, act["value"]))
        r = fixture_sh(cfg, flip_cmd, stdin_text=FLIP_SCRIPT)
        if r.returncode != 0:
            return die("ABORT - failed to write the flag file on the fixture: %s" % r.stderr.strip())
        for _ in range(5):
            sleep(2)
            if ofrep_variant(cfg, flag) == act["value"]:
                fault["ofrep_confirmed"] = True
                break
        if not fault["ofrep_confirmed"]:
            return die("ABORT - OFREP never showed %r=%r after the flip" % (flag, act["value"]))
        log.detail("bench: OFREP confirms %r=%r" % (flag, act["value"]))
        log.check(True, "%s -> %s, confirmed by OFREP" % (flag, act["value"]))
        t_flip = time.monotonic()
        # Count only traces that started after this flip; an earlier aborted attempt
        # leaves matching traces behind that would satisfy the wait instantly.
        flip_us, fault["flip_utc"] = now_us(), utc()
        sig = re.compile(signal_re, re.I)
        for i in range(1, poll_max + 1):
            try:
                fault["signal_traces_seen"] = sum(1 for t in traces(cfg, gt_service, flip_us, now_us(), 50, 30) if sig.search(json.dumps(t)))
                log.detail("bench: poll %d/%d: %d matching traces" % (i, poll_max, fault["signal_traces_seen"]))
            except Exception as e:
                log.detail("bench: poll %d/%d: Jaeger query failed: %s" % (i, poll_max, e))
            if fault["signal_traces_seen"] >= TRACE_MIN_COUNT:
                break
            if i < poll_max:
                sleep(TRACE_POLL_SLEEP)
        if fault["signal_traces_seen"] < TRACE_MIN_COUNT:
            return die("ABORT - never saw %d matching traces after %d polls" % (TRACE_MIN_COUNT, poll_max))
        log.detail("bench: fault confirmed live")
        log.check(True, "%d traces match the signal after %s" % (fault["signal_traces_seen"], dur(time.monotonic() - t_flip)))
        if signal_after_restart:
            log.detail("== waiting for %s to restart (signal_after_restart) ==" % signal_after_restart)
            snap0 = container_snapshot(cfg).get(signal_after_restart)
            if snap0 is None:
                return die("ABORT - could not read %s's restart count for signal_after_restart" % signal_after_restart)
            restart0 = int(snap0[0])
            polls = wait_for_restart(cfg, signal_after_restart, restart0, poll_max, out=lambda i, cur, oom: log.detail(
                "bench: restart poll %d/%d: %s RestartCount=%s (was %d) OOMKilled=%s" % (i, poll_max, signal_after_restart, cur, restart0, oom)))
            if polls is None:
                return die("ABORT - %s never restarted within %d polls after the flip (signal_after_restart)" % (signal_after_restart, poll_max))
            fault["restart_seen_utc"] = utc()
            fault["restart_seen_after_s"] = round(time.monotonic() - t_flip, 1)
            log.check(True, "%s restarted %s after the flip" % (signal_after_restart, dur(fault["restart_seen_after_s"])))
        # The worst leaks only exist while a fault is active (an event saying variant=on).
        log.detail("== leak scan under fault ==")
        rc = fixture_leak.scan_live(list(pristine_doc["flags"]), 240, out=log.detail)
        pre["fixture_leak_under_fault"] = "PASS" if rc == 0 else "FAIL"
        log.check(rc == 0, "telemetry under the fault names no flag")
        if rc == 0 and any_tools:
            # Needs the fault live, so it runs here and not in the read-only pre-flight.
            log.detail("== oracle under fault (scripted MCP calls must reach the signal) ==")
            ok = oracle(a.scenario, cfg, out=log.detail) == 0
            pre["oracle"] = "PASS" if ok else "FAIL"
            log.check(ok, "oracle reached the signal " + paint(DIM, "(" + log.step[-1].split(" reached ", 1)[-1] + ")")
                      if ok else "oracle never reached the signal")
        write_json(os.path.join(bdir, "preflight.json"), pre)
        write_json(os.path.join(bdir, "manifest.json"), manifest)  # manifest.preflight is the same dict
        if rc != 0:
            return die("ABORT - the fixture names a flag while the fault is active; no trials run")
        if pre["oracle"] == "FAIL":
            return die("ABORT - the oracle's scripted MCP calls never reached the signal under the fault; no trials run")

        fails = 0
        secrets = api_secrets(a, cfg)
        labels = ["[%*d/%d] %s #%d" % (len(str(len(plan))), i + 1, len(plan), c["arm"], c["trial_index"]) for i, c in enumerate(plan)]
        w = max(map(len, labels))
        log.section("Trials".ljust(w + 4), paint(DIM, "verdict  calls    time"))
        for c, label in zip(plan, labels):
            log.detail("== cell order_index=%d arm=%s trial_index=%d ==" % (c["order_index"], c["arm"], c["trial_index"]))
            label, t0 = "  " + label.ljust(w) + "  ", time.monotonic()
            tick = lambda: log.live(label + paint(CYAN, "running...".ljust(16) + dur(time.monotonic() - t0).rjust(6)))
            tick()
            try:
                rec = run_cell(a, bdir, c, prompt_for, prompt_file, desc, common, fault, sut, mcp_url, cfg,
                               expected_restarts, secrets, tick=tick)
            except KeyboardInterrupt:
                # Every trial dir on disk gets a cells row, so band and INDEX see what verify sees.
                trial = os.path.join(bdir, "%d-%s" % (c["order_index"], c["arm"]))
                if os.path.isdir(trial):
                    append_line(cells_file, {"order_index": c["order_index"], "arm": c["arm"], "trial_index": c["trial_index"],
                                             "model": a.model, "out_dir": os.path.basename(trial), "trial_exit_code": None,
                                             "wall_time_s": None, "started_utc": None, "ended_utc": None,
                                             "failed": True, "interrupted": True})
                raise
            append_line(cells_file, rec)
            st["ran"] += 1
            seen = a.client == "cli" and is_full_model_id(a.model) and observe(os.path.join(bdir, rec["out_dir"], "stream.jsonl"))["model"]
            if seen and seen != a.model:
                log(paint(BOLD_RED, "bench: ABORTING remaining cells - model pin: run.model is %s but the stream reports %s"
                          % (a.model, seen)))
                st["aborted"] = True
                return None
            fails = fails + 1 if rec["failed"] else 0
            verdict, calls, note = "ERROR", "-", "exit %s" % rec["trial_exit_code"]
            try:
                s = score.score(os.path.join(bdir, rec["out_dir"]))[0]  # grader runs after the restore
                calls = str(s.get("tool_calls"))
                verdict, note = (verdict, "%s, scored %s" % (note, s.get("verdict"))) if rec["failed"] else (s.get("verdict") or "ERROR", "")
            except Exception:
                note = note if rec["failed"] else "unscorable"
            log(label + paint(VERDICT_COLOR.get(verdict, "0"), verdict.ljust(7)) + "  " + calls.rjust(5) + "  "
                + paint(DIM, dur(rec["wall_time_s"]).rjust(6)) + ("  " + paint(DIM, note) if note else ""))
            if rec["failed"]:
                log.detail("bench: cell FAILED (exit=%s), consecutive_fails=%d" % (rec["trial_exit_code"], fails))
            if fails >= 3:
                log(paint(BOLD_RED, "bench: ABORTING remaining cells - 3 consecutive failures"))
                st["aborted"] = True
                return None
            if a.first_only:
                log(paint(DIM, "bench: --first-only: stopping after one cell"))
                return None
        return None

    early, interrupted = None, False
    old = {s: signal.signal(s, _interrupt) for s in STOP_SIGNALS}
    try:
        early = fault_and_cells()
    except KeyboardInterrupt:
        log(paint(YELLOW, "bench: interrupted; no further cells run"))
        interrupted = True
    finally:
        # A second signal must not abort the restore: ignore them until it is done.
        for s_ in STOP_SIGNALS:
            signal.signal(s_, signal.SIG_IGN)
        restored = restore(cfg, bdir, restore_cmd, flag, default_variant, log)
        for s_, h in old.items():
            signal.signal(s_, h)
    if early is not None:
        keep_records(bdir, a.experiment, log)
        return early if restored else 5

    # ---- score, summary, band ----
    log.detail("== scoring cells (one %s grader call per new mechanism answer, cached in grades.jsonl) ==" % grade.GRADER_MODEL)
    rows, scored = [], []
    for rec in read_jsonl(cells_file):
        t = os.path.join(bdir, rec["out_dir"])
        try:
            s = score.score(t, call_grader=True)[0]
            scored.append((t, s))
        except Exception as e:
            # str(e) can carry the absolute trial path (e.g. a FileNotFoundError from open());
            # scrub it back to the relative out_dir name before it reaches scores.jsonl.
            s = {"dir": rec["out_dir"], "error": str(e).replace(t, rec["out_dir"])}
        s["arm"] = rec["arm"]
        rows.append(s)
    with open(os.path.join(bdir, "scores.jsonl"), "w", encoding="utf-8") as f:
        f.writelines(json.dumps(s) + "\n" for s in rows)
    cells = read_jsonl(cells_file)
    results(a.scenario, [arm_row(arm, [(t, s) for t, s in scored if s["arm"] == arm], len(counted(cells, arm)))
                         for arm in arms], log, log.detail)
    keep_records(bdir, a.experiment, log)
    if not restored:
        log(paint(BOLD_RED, "bench: the flag was NOT confirmed back at its default - check the fixture before anything else"))
        return 5
    if st["aborted"]:
        log(paint(BOLD_RED, "bench: batch ABORTED EARLY (3 consecutive cell failures) - see %s" % shown(cells_file)))
        return 4
    if interrupted or (not a.first_only and st["ran"] < len(plan)):
        log(paint(YELLOW, "bench: ran %d of %d planned cells; batch INCOMPLETE" % (st["ran"], len(plan))))
        return 6
    log.detail("bench: batch complete: %s" % bdir)
    return 0


META_KEYS = (
    "schema_version", "run_id", "client", "provider", "batch_id", "order_index", "trial_index", "seed", "scenario", "scenario_sha256",
    "arm", "prompt_name", "prompt_sha256", "system_prompt_sha256", "verdict_schema_sha256", "model_requested",
    "effort", "max_turns", "max_budget_usd", "client_argv", "history_policy", "client_version_pre", "mcp_endpoint",
    "mcp_config_sha256", "tools_list_sha256", "tools_count", "tool_descriptions_file", "tool_descriptions_sha256",
    "tool_descriptions_check", "jaeger_image", "jaeger_image_id", "jaeger_commit", "otel_demo_ref",
    "fixture_overlay_sha256", "fault", "flag_names", "preflight", "experiment", "harness_git_sha", "harness_dirty",
    "score_py_sha256", "started_utc", "ended_utc", "wall_time_s", "exit_code", "observed", "agent_loop",
    "system_under_test", "fixture_changes", "fixture_ok")


SECRET_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL")


def run_cell(a, bdir, c, prompt_for, prompt_file, desc, common, fault, sut, mcp_url, cfg, expected_restarts,
            secrets=None, tick=lambda: None):
    arm, oi = c["arm"], c["order_index"]
    name = "%d-%s" % (oi, arm)
    trial = os.path.join(bdir, name)
    os.makedirs(trial)
    shutil.copy(prompt_file[arm], os.path.join(trial, "prompt.txt"))
    shutil.copy(SYSTEM_PROMPT, os.path.join(trial, "system-prompt.txt"))
    if desc[arm]["check"] == "no_tools":  # this arm gets no MCP server, whatever its batch-mates have
        common = dict(common, mcp_endpoint=None, tools_list_sha256=None, tools_count=None)
        mcp_url = None
    mcp_path = os.path.join(trial, "mcp.json")
    write_mcp_config(mcp_path, mcp_url)
    argv = client_argv(a, arm, prompt_file[arm], mcp_path, trial)
    prompt_sha = sha256_file(prompt_file[arm])
    shown = recorded_argv(argv, bdir, trial)
    if a.client == "cli":  # the api argv carries file paths, not the texts
        shown[2] = "sha256:" + prompt_sha
        shown[shown.index("--system-prompt") + 1] = "sha256:" + common["system_prompt_sha256"]
    merged = dict(common, run_id="%s/%s" % (common["batch_id"], name), order_index=oi, trial_index=c["trial_index"],
                  arm=arm, prompt_name=prompt_for[arm], prompt_sha256=prompt_sha, client_argv=shown,
                  mcp_config_sha256=sha256_file(mcp_path), tool_descriptions_file=desc[arm]["file"],
                  tool_descriptions_sha256=desc[arm]["sha256"], tool_descriptions_check=desc[arm]["check"],
                  fault=fault, started_utc=utc(), ended_utc=None, wall_time_s=None, exit_code=None,
                  observed=None, agent_loop=None, system_under_test=sut, fixture_changes=None, fixture_ok=None)
    meta = {k: merged[k] for k in META_KEYS}
    write_json(os.path.join(trial, "meta.json"), meta)
    # cli: no API key in the environment, so claude bills the logged-in plan.
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE",) + SECRET_KEYS}
    if a.client == "api":
        env.update(secrets or {})  # environment only: never argv, never a record
    stream = os.path.join(trial, "stream.jsonl")
    before = container_snapshot(cfg)
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory() as work, open(stream, "w") as so, open(os.path.join(trial, "stderr.txt"), "w") as se:
        try:
            # The loop writes stream.jsonl itself (--out-dir), so its stdout goes with stderr.
            p = subprocess.Popen(argv, cwd=work, env=env, stdin=subprocess.DEVNULL, stdout=so if a.client == "cli" else se,
                                 stderr=se)
        except OSError as e:
            se.write("bench: could not start %s: %s\n" % (argv[0], e))
            rc = 127
        else:
            try:
                while True:
                    try:
                        rc = p.wait(timeout=1)
                        break
                    except subprocess.TimeoutExpired:
                        tick()
            except BaseException:  # an interrupt must not leave the agent running
                p.kill()
                p.wait()
                raise
    with open(os.path.join(trial, "exit.txt"), "w") as f:
        f.write("exit=%d\n" % rc)
    loop_meta = os.path.join(trial, "agent_loop.json")
    fixture_changes, fixture_ok = fixture_diff(before, container_snapshot(cfg), expected_restarts)
    meta.update(ended_utc=utc(), wall_time_s=round(time.monotonic() - t0, 3), exit_code=rc, observed=observe(stream),
                agent_loop=load_json(loop_meta).get("agent_loop") if os.path.isfile(loop_meta) else None,
                fixture_changes=fixture_changes, fixture_ok=fixture_ok)
    write_json(os.path.join(trial, "meta.json"), meta)
    return {"order_index": oi, "arm": arm, "trial_index": c["trial_index"], "model": a.model,
            "out_dir": name, "trial_exit_code": rc, "wall_time_s": meta["wall_time_s"],
            "started_utc": meta["started_utc"], "ended_utc": meta["ended_utc"], "failed": rc != 0}


def restore(cfg, bdir, restore_cmd, flag, default_variant, log):
    log.section("Restore")
    log.detail("bench: restoring the pristine flag file on the fixture")
    cp_ok = fixture_sh(cfg, restore_cmd).returncode == 0
    if cp_ok:
        log.detail("bench: restore cp OK")
    else:
        log(paint(BOLD_RED, "bench: RESTORE FAILED - cp of the pristine flag file did not succeed - MANUAL INTERVENTION NEEDED"))
    sleep(2)
    after = ofrep_variant(cfg, flag)
    confirmed = after == default_variant
    if confirmed:
        log.detail("bench: OFREP confirms %r back at default (%r)" % (flag, after))
        log.check(True, "flag %s back at default (%s)" % (flag, after))
    else:
        log(paint(YELLOW, "bench: WARNING - OFREP reports %r=%r, expected default %r - MANUAL CHECK NEEDED" % (flag, after, default_variant)))
    write_json(os.path.join(bdir, "restore.json"), {"restored_utc": utc(), "cp_ok": cp_ok, "default_variant": default_variant,
                                                   "variant_after_restore": after, "default_confirmed": confirmed})
    return confirmed


RECORD_FILES = ("manifest.json", "preflight.json", "cells.jsonl", "scores.jsonl", "restore.json")


def keep_records(bdir, experiment, log):
    """Copy the batch-level records into the repository; trajectories stay under RUNS_DIR."""
    dest = os.path.join(RECORDS, experiment, os.path.basename(bdir))
    os.makedirs(dest, exist_ok=True)
    kept = [f for f in RECORD_FILES if os.path.isfile(os.path.join(bdir, f))]
    for f in kept:
        shutil.copy(os.path.join(bdir, f), dest)
    log.detail("bench: records kept in %s: %s" % (os.path.relpath(dest, ROOT), " ".join(kept)))
    write_index(RECORDS, out=log.detail)
    log("")
    log(paint(BOLD_CYAN, "Batch  ") + " " + shown(bdir) + "   " + paint(DIM, "(full log: batch.log)"))
    log(paint(BOLD_CYAN, "Records") + " " + shown(dest))


# ---- soak, verify, index -------------------------------------------------------

def soak(cfg, scenario, samples, interval):
    sf = os.path.join(HARNESS, "scenarios", scenario + ".json")
    if not os.path.isfile(sf):
        print("soak: no such scenario file: %s" % sf, file=sys.stderr)
        return 2
    svc = load_json(sf)["ground_truth"]["service"]
    d = os.path.join(runs_root(cfg), scenario)
    os.makedirs(d, exist_ok=True)
    logp = os.path.join(d, "soak-%s.log" % datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))

    def out(msg):
        print(msg)
        with open(logp, "a", encoding="utf-8") as f:
            f.write(msg + "\n")

    out("soak start %s scenario=%s service=%s samples=%d interval=%ds; pass = load1<8, 0 unhealthy, >=3 %s traces per 180s"
        % (utc(), scenario, svc, samples, interval, svc))
    bad = 0
    for i in range(1, samples + 1):
        try:
            calls = len(traces(cfg, svc, now_us() - 180 * 10**6, now_us(), 100, 20))
        except Exception:
            calls = -1
        r = fixture_sh(cfg, SOAK_REMOTE, timeout=60)
        fields = r.stdout.split() if r.returncode == 0 else []
        load, unhealthy, containers, sy = fields if len(fields) == 4 else ["NA"] * 4
        try:
            ok = float(load) < 8 and unhealthy == "0" and calls >= 3
        except ValueError:
            ok = False
        out("sample %d %s load1=%s unhealthy=%s containers=%s sy=%s %s_calls_180s=%d ok=%d"
            % (i, datetime.now(timezone.utc).strftime("%H:%M:%S"), load, unhealthy, containers, sy, svc, calls, ok))
        bad = 0 if ok else bad + 1
        if bad >= 2:
            out("SOAK FAIL at sample %d (two consecutive bad samples)" % i)
            return 5
        if i < samples:
            sleep(interval)
    out("SOAK PASS %s" % utc())
    return 0


def scenario_at_batch_sha(d):
    """The scenario JSON as it was at the batch's recorded harness_git_sha (a -dirty suffix is
    stripped: the committed file is the nearest record), so verify scores a batch against the
    truth it ran. None (score.py loads the current file) if git cannot resolve it; says so."""
    mp = os.path.join(d, "meta.json")
    meta = load_json(mp) if os.path.isfile(mp) else {}
    sha, name = str(meta.get("harness_git_sha") or ""), meta.get("scenario")
    if not sha:  # legacy batch: nothing recorded to resolve
        return None
    if sha.endswith("-dirty"):
        sha = sha[:-len("-dirty")]
        print("verify: %s: harness_git_sha is -dirty, using the committed %s" % (d, sha))
    r = subprocess.run(["git", "-C", ROOT, "show", "%s:harness/scenarios/%s.json" % (sha, name)], capture_output=True, text=True)
    if r.returncode == 0:
        return json.loads(r.stdout)
    print("verify: %s: git cannot resolve scenario %s at %r, using the current file" % (d, name, sha))
    return None


def verify(cfg):
    runs = runs_root(cfg)
    arm_of, stored = {}, {}
    for cf in glob.glob(os.path.join(runs, "**", "cells.jsonl"), recursive=True):
        b = os.path.dirname(cf)
        for rec in read_jsonl(cf):
            if rec.get("out_dir"):
                arm_of[os.path.realpath(os.path.join(b, rec["out_dir"]))] = rec.get("arm")
        for row in read_jsonl(os.path.join(b, "scores.jsonl")):
            if row.get("dir"):
                stored[os.path.realpath(os.path.join(b, row["dir"]))] = row.get("verdict")
    cols = ["scenario", "arm", "dir", "verdict", "tool_calls", "call_errors", "steps_to_evidence", "tool_output_chars", "cost", "compaction"]
    rows, mismatches, invalid, ungraded = [], [], [], []
    for p in sorted(glob.glob(os.path.join(runs, "**", "stream.jsonl"), recursive=True)):
        d = os.path.dirname(p)
        mp = os.path.join(d, "meta.json")
        arm = arm_of.get(os.path.realpath(d)) or (load_json(mp).get("arm") if os.path.isfile(mp) else None)
        try:
            s, _ = score.score(d, scenario=scenario_at_batch_sha(d))
        except score.Legacy as e:
            rows.append(dict({c: "" for c in cols}, scenario=load_json(mp).get("scenario"), arm=arm,
                             dir=os.path.relpath(d, runs), verdict="%s (%s)" % (stored.get(os.path.realpath(d)) or "no stored score", e)))
            continue
        except Exception as e:
            rows.append(dict({c: "" for c in cols}, scenario="?", arm=arm, dir=os.path.relpath(d, runs), verdict="ERROR: %s" % e))
            continue
        key = os.path.realpath(d)
        if key in stored and stored[key] != s.get("verdict"):
            mismatches.append("verify: MISMATCH %s: stored verdict %s, re-scored %s" % (os.path.relpath(d, runs), stored[key], s.get("verdict")))
        if s.get("verdict") == "INVALID":
            invalid.append("verify: INVALID %s: %s" % (os.path.relpath(d, runs), " ".join(s.get("sandbox_violations") or [])))
        if s.get("verdict") == "LEAK":
            invalid.append("verify: LEAK %s: %s" % (os.path.relpath(d, runs), " ".join(s.get("leak_hits") or [])))
        if s.get("mechanism") == "UNGRADED":
            ungraded.append("verify: UNGRADED %s: no cached grade in grades.jsonl (verify never calls the grader)"
                            % os.path.relpath(d, runs))
        rows.append({"scenario": s.get("scenario_used"), "arm": arm, "dir": os.path.relpath(d, runs), "verdict": s.get("verdict"),
                     "tool_calls": s.get("tool_calls"), "call_errors": s.get("call_errors"),
                     "steps_to_evidence": s.get("steps_to_evidence"), "tool_output_chars": s.get("tool_output_chars"),
                     "cost": s.get("cost_usd"), "compaction": s.get("compaction_events")})
    if rows:
        for r in [dict(zip(cols, cols))] + rows:
            print("\t".join(str(r[c]) for c in cols))
        for r in rows:
            if isinstance(r["compaction"], int) and r["compaction"] > 0:
                print("WARNING: %s: %d compaction event(s) in stream.jsonl - context numbers for this run are not comparable"
                      % (r["dir"], r["compaction"]))
    else:
        print("verify: no stream.jsonl under %s (RUNS_DIR)" % runs)
    for line in mismatches + invalid + ungraded:
        print(line)
    idx = os.path.join(RECORDS, "INDEX.md")
    current = read_text(idx) if os.path.isfile(idx) else ""
    stale = index_text(RECORDS) != current
    if stale:
        print("verify: FAIL - %s differs from what `bench.py index` generates; run it and commit the result" % idx)
    if mismatches:
        print("verify: FAIL - %d stored verdict(s) in scores.jsonl differ from a re-score of the raw files" % len(mismatches))
    if invalid:
        print("verify: FAIL - %d run(s) INVALID or LEAK: the sandbox check failed or the answer names a flag, so no "
              "result from their batch stands" % len(invalid))
    if ungraded:
        print("verify: FAIL - %d run(s) have no cached mechanism grade" % len(ungraded))
    if stale or mismatches or invalid or ungraded:
        return 1
    print("verify: %s matches the batch records" % idx)
    return 0


def index_text(runs):
    """records/INDEX.md, from manifest.json, cells.jsonl and scores.jsonl only."""
    rows = []
    scen_agg = {}
    for mp in glob.glob(os.path.join(runs, "**", "manifest.json"), recursive=True):
        b = os.path.dirname(mp)
        m = load_json(mp)
        cells, scores = read_jsonl(os.path.join(b, "cells.jsonl")), read_jsonl(os.path.join(b, "scores.jsonl"))
        if m.get("scenario"):
            sa = scen_agg.setdefault(m["scenario"], {"batches": 0, "cells": 0})
            sa["batches"] += 1
            sa["cells"] += len(scores)
        arms = m["arms"]
        n = {arm: len(counted(cells, arm)) for arm in arms}
        by = {arm: [s for s in scores if s.get("arm") == arm] for arm in arms}
        date = re.search(r"(\d{4})(\d{2})(\d{2})T", m["batch_id"])
        observed = sorted({s["model_asserted"] for s in scores if s.get("model_asserted")})
        rel = os.path.relpath(b, runs)
        rows.append([
            "[%s](%s/)" % (rel, rel),
            "-".join(date.groups()),
            m["scenario"],
            ", ".join("%s %d" % (arm, n[arm]) for arm in arms),
            "%s %s" % (m["client"], m["model_requested"]), ", ".join(observed) or NOT_RECORDED, m["effort"],
            m["client_version_pre"] or NOT_RECORDED, m["jaeger_image"], m["experiment"]["name"],
            ", ".join("%s %s" % (arm, "/".join(str(sum(1 for s in by[arm] if s.get("verdict") == v)) for v in ("PASS", "PARTIAL", "FAIL", "ABSTAIN", "INVALID", "LEAK"))) for arm in arms),
            ", ".join("%s %d" % (arm, band_code(n[arm], sum(1 for s in by[arm] if s.get("verdict") == "PASS"))) for arm in arms),
            "%s (legacy schema, stored scores)" % m.get("schema_version") if m.get("schema_version", score.SCHEMA_VERSION) < score.SCHEMA_VERSION
            else str(m.get("schema_version", score.SCHEMA_VERSION)),
        ])
    rows.sort(key=lambda r: (r[1], r[0]))

    scen_rows = []
    for sf in sorted(glob.glob(os.path.join(HARNESS, "scenarios", "*.json"))):
        name = os.path.splitext(os.path.basename(sf))[0]
        d = load_json(sf)
        rc = readiness(name, out=lambda *a, **k: None)
        sa = scen_agg.get(name, {"batches": 0, "cells": 0})
        scen_rows.append([name, d.get("deterministic") or NOT_RECORDED, "PASS" if rc == 0 else "FAIL",
                           str(sa["batches"]), str(sa["cells"])])
    scen_head = ["scenario", "deterministic", "readiness", "batches recorded", "cells scored"]

    head = ["batch", "date", "scenario", "arms (cells)", "client and model requested", "model observed", "effort",
            "client version", "jaeger image", "experiment", "PASS/PARTIAL/FAIL/ABSTAIN/INVALID/LEAK", "band", "record schema"]
    lines = ["# Run index", "",
             "Generated by `harness/bench.py index` from each batch's manifest.json, cells.jsonl and scores.jsonl.",
             "Do not edit by hand: `harness/bench.py verify` fails when this file differs from what it would generate.",
             "Band is the `bench.py band` exit code per arm from the same rows: 0 certified, 2 fewer than 10 cells",
             "(rank only), 3 pass rate 0 or 1 (read the trajectories).",
             "", "## Scenarios", "",
             "One row per harness/scenarios/*.json. Readiness is `bench.py readiness` run in process; batches and",
             "cells scored count manifest.json/scores.jsonl under records/ for that scenario.",
             "", "| " + " | ".join(scen_head) + " |", "|" + "---|" * len(scen_head)]
    lines += ["| " + " | ".join(r) + " |" for r in scen_rows]
    lines += ["", "## Batches", "",
              "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    lines += ["", "## Results", "",
              "One row per records/<experiment>/RESULT.md, the last `harness/judge.py` answer for that experiment.",
              "", "| experiment | verdict | result |", "|---|---|---|"]
    for rp in sorted(glob.glob(os.path.join(runs, "*", "RESULT.md"))):
        name = os.path.basename(os.path.dirname(rp))
        verdict = re.findall(r"^EXPERIMENT (?:PASS|FAIL)$", read_text(rp), re.M)
        lines.append("| %s | %s | [RESULT.md](%s/RESULT.md) |" % (name, verdict[-1] if verdict else NOT_RECORDED, name))
    return "\n".join(lines) + "\n"


def write_index(runs, out=print):
    os.makedirs(runs, exist_ok=True)
    path = os.path.join(runs, "INDEX.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(index_text(runs))
    out("index: wrote %s" % path)
    return 0


# ---- pack ----------------------------------------------------------------------

PACK_PATTERNS = ("claude.ai/code/session", "Claude-Session:")


def pack_members(batches):
    """(arcname, bytes) for every file of every batch, sorted; arcname is <scenario>/<batch-id>/<path>.
    mcp.json carries the real fixture URL by design: its url becomes the host-free :<port><path>.
    batch.log (the operator log: ssh target, fixture URLs, absolute paths by design) stays out."""
    for scenario, bdir in batches:
        files = []
        for d, _, names in os.walk(bdir):
            files += [os.path.join(d, n) for n in names if os.path.isfile(os.path.join(d, n)) and n != "batch.log"]
        for path in sorted(files):
            arc = "/".join([scenario, os.path.basename(bdir)] + os.path.relpath(path, bdir).split(os.sep))
            with open(path, "rb") as f:
                data = f.read()
            if os.path.basename(path) == "mcp.json":
                doc = json.loads(data)
                for srv in (doc.get("mcpServers") or {}).values():
                    if srv.get("url"):
                        srv["url"] = config.host_free(srv["url"])
                data = (json.dumps(doc, indent=2) + "\n").encode()
            yield arc, data


def leak_rx(cfg):
    """[(what, regex)] for every value that must never be published: fixture identity, keys, home, session ids."""
    needles = [(k, cfg.get(k)) for k in ("FIXTURE_HOST", "FIXTURE_SSH_USER") + SECRET_KEYS
               if not (k == "FIXTURE_HOST" and cfg.get(k) in ("localhost", "127.0.0.1", "::1"))]  # loopback names nothing
    needles.append(("home directory", os.path.expanduser("~")))
    rx = [(name, re.compile(r"(?<!\w)%s(?!\w)" % re.escape(v))) for name, v in needles if v]
    return rx + [(p, re.compile(re.escape(p))) for p in PACK_PATTERNS]


def pack_findings(batches, cfg):
    """['<arcname>: <what> (<count>)'] for every file that would leak; names the key, never its value."""
    rx, found = leak_rx(cfg), []
    for arc, data in pack_members(batches):
        text = data.decode("utf-8", errors="replace")
        found += ["%s: %s (%d)" % (arc, name, len(r.findall(text))) for name, r in rx if r.search(text)]
    return found


def pack(experiment, cfg):
    batches = []
    for mp in sorted(glob.glob(os.path.join(RECORDS, experiment, "*", "manifest.json"))):
        m = load_json(mp)
        bdir = os.path.join(runs_root(cfg), m["scenario"], m["batch_id"])
        if not os.path.isdir(bdir):
            print("pack: REFUSED - %s is recorded but %s is missing" % (m["batch_id"], bdir))
            return 1
        batches.append((m["scenario"], bdir))
    if not batches:
        print("pack: REFUSED - no batch recorded under %s" % os.path.join(RECORDS, experiment))
        return 1
    found = pack_findings(batches, cfg)
    if found:
        for line in found:
            print("pack: FOUND " + line)
        print("pack: REFUSED - %d file(s) would publish the above; nothing written" % len(found))
        return 1
    name = "%s-trajectories.tar.gz" % experiment
    os.makedirs(DIST, exist_ok=True)
    out = os.path.join(DIST, name)
    # Deterministic: sorted names, mtime 0, uid/gid 0, no gzip name or time, so the sha256 reproduces.
    with open(out + ".tmp", "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for arc, data in pack_members(batches):
            ti = tarfile.TarInfo(arc)
            ti.size, ti.mode, ti.mtime = len(data), 0o644, 0
            tar.addfile(ti, io.BytesIO(data))
    os.replace(out + ".tmp", out)
    digest = sha256_file(out)
    with open(os.path.join(RECORDS, experiment, "trajectories.sha256"), "w", encoding="utf-8") as f:
        f.write("%s  %s\n" % (digest, name))
    print("pack: batch.log left out; each mcp.json url rewritten to its host-free form")
    print("pack: wrote %s (%d batch(es)), sha256 %s in %s" % (
        os.path.relpath(out, ROOT), len(batches), digest, os.path.relpath(f.name, ROOT)))
    return 0


# ---- export ----------------------------------------------------------------------

def export(batch_dir, endpoint, cfg):
    """POST every scored trial of a RUNS_DIR batch to Phoenix as one OTLP request (harness/otlp.py).
    Reads the batch, writes nothing; refuses when a span would carry what pack refuses."""
    rx, spans, found = leak_rx(cfg), [], []
    try:
        m = load_json(os.path.join(batch_dir, "manifest.json"))
        exp = (m.get("experiment") or {}).get("name") or m["scenario"]
        rows = read_jsonl(os.path.join(batch_dir, "scores.jsonl"))
        for row in rows:
            trial = os.path.join(batch_dir, row["dir"])
            meta = load_json(os.path.join(trial, "meta.json"))
            events = [e for e in (json.loads(l) for l in read_text(os.path.join(trial, "stream.jsonl")).splitlines() if l.strip())
                      if isinstance(e, dict)]
            attrs = {"experiment": exp, "batch_id": m["batch_id"], "scenario": m["scenario"], "arm": row["arm"],
                     "trial_index": meta["trial_index"], "model": (meta.get("observed") or {}).get("model") or meta["model_requested"],
                     "client": meta["client"], "verdict": row.get("verdict") or "ERROR"}
            t0 = int(datetime.strptime(meta["started_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()) * 10**9
            trial_spans = otlp.trace_spans("%s/%s/%s" % (m["scenario"], m["batch_id"], row["dir"]), events, t0,
                                           [otlp.kv(k, v) for k, v in attrs.items()] + [otlp.kv("metadata", json.dumps(attrs))])
            text = "\n".join([s["name"] for s in trial_spans] +  # raw values: JSON escapes could hide a word boundary
                              [str(next(iter(a["value"].values()))) for s in trial_spans for a in s["attributes"]])
            found += ["%s: %s (%d)" % (row["dir"], name, len(r.findall(text))) for name, r in rx if r.search(text)]
            spans += trial_spans
    except (OSError, ValueError, KeyError) as e:
        print("export: REFUSED - cannot read the batch in %s (a RUNS_DIR batch with its trial directories): %r" % (batch_dir, e))
        return 1
    if not rows:
        print("export: REFUSED - no scores.jsonl rows in %s" % batch_dir)
        return 1
    if found:
        for line in found:
            print("export: FOUND " + line)
        print("export: REFUSED - %d trial(s) would send the above; nothing sent" % len(found))
        return 1
    request = {"resourceSpans": [{"resource": {"attributes": [otlp.kv("service.name", "jaeger-mcp-evals"),
                                                               otlp.kv("openinference.project.name", exp)]},
                                  "scopeSpans": [{"scope": {"name": "bench.py export"}, "spans": spans}]}]}
    url = (endpoint or "http://%s:%s" % (cfg.get("FIXTURE_HOST") or "localhost", cfg.get("PHOENIX_PORT") or "16006"))
    url = url.rstrip("/") + "/v1/traces"
    req = urllib.request.Request(url, data=otlp.encode(request), method="POST",
                                 headers={"Content-Type": "application/x-protobuf"})
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
    except OSError as e:  # HTTPError and URLError included
        print("export: FAILED - Phoenix at %s: %s" % (url, e))
        return 1
    print("export: sent %d trial(s), %d spans, to project %s at %s" % (len(rows), len(spans), exp, url))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="bench.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the batch an experiment file describes")
    r.add_argument("experiment_file")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--first-only", action="store_true")
    r.set_defaults(func=run)
    sub.add_parser("verify").set_defaults(func=lambda a, cfg: verify(cfg))
    bd = sub.add_parser("band")
    bd.add_argument("batch_dir")
    bd.add_argument("--arm")
    bd.set_defaults(func=lambda a, cfg: band(a.batch_dir, a.arm))
    lk = sub.add_parser("leak")
    lk.add_argument("files", nargs="+")
    lk.set_defaults(func=lambda a, cfg: leak(a.files))
    rd = sub.add_parser("readiness")
    rd.add_argument("scenario")
    rd.set_defaults(func=lambda a, cfg: readiness(a.scenario))
    orc = sub.add_parser("oracle")
    orc.add_argument("scenario")
    orc.set_defaults(func=lambda a, cfg: oracle(a.scenario, cfg))
    sk = sub.add_parser("soak")
    sk.add_argument("scenario")
    sk.add_argument("samples", nargs="?", type=int, default=30)
    sk.add_argument("interval_s", nargs="?", type=int, default=60)
    sk.set_defaults(func=lambda a, cfg: soak(cfg, a.scenario, a.samples, a.interval_s))
    sub.add_parser("index").set_defaults(func=lambda a, cfg: write_index(RECORDS))
    pk = sub.add_parser("pack")
    pk.add_argument("experiment")
    pk.set_defaults(func=lambda a, cfg: pack(a.experiment, cfg))
    ex = sub.add_parser("export")
    ex.add_argument("batch_dir")
    ex.add_argument("--endpoint", help="Phoenix base URL (default http://<FIXTURE_HOST>:<PHOENIX_PORT>)")
    ex.set_defaults(func=lambda a, cfg: export(a.batch_dir, a.endpoint, cfg))
    pw = sub.add_parser("power")
    pw.add_argument("n_per_arm", type=int)
    pw.set_defaults(func=lambda a, cfg: power(a.n_per_arm))
    a = p.parse_args(argv)
    return a.func(a, config.load())

if __name__ == "__main__":
    sys.exit(main())
