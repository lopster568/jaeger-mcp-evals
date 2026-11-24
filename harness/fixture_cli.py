#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""fixture_cli.py: what the make targets run, one line per step (fixture/FIXTURE.md stays the manual).

  fixture_cli.py setup     tools, fixture, up, phoenix: everything free, in order
  fixture_cli.py tools     the Tools line (after fixture/preflight.sh has checked them)
  fixture_cli.py fixture   clone the OpenTelemetry Demo 3.0.0 into FIXTURE_DEMO_DIR, install the overlay
  fixture_cli.py up        pull missing images (and the Goose image), start the demo (and Phoenix, if it exists), wait until it is up
  fixture_cli.py phoenix   start the Phoenix trajectory store and wait until it answers
  fixture_cli.py smoke [--dry-run]  the smoke dry run, then ONE paid agent trial (CLIENT: goose by default, claude or codex)
  fixture_cli.py down      stop the demo containers (Jaeger traces are lost, Phoenix keeps its own)
  fixture_cli.py clean     remove the demo containers and network
  fixture_cli.py test      the offline unit tests

On a TTY each step is one line redrawn in place; on a pipe a start line (for steps over a second), at most
one heartbeat a minute, and an end line. Third-party output goes to .make.log and is shown only on failure.
Settings come from fixture.env as everywhere else (harness/config.py). Local mode only.
"""
import filecmp
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import unittest
import urllib.request

import bench
import config
import goose_client
from bench import BOLD_RED, CYAN, DIM, GREEN, dur, paint

ROOT = config.ROOT
LOG = os.path.join(ROOT, ".make.log")
SMOKE = os.path.join("harness", "experiments", "smoke-paymentfailure.json")  # a template: no run.client, provider, model
SMOKE_RUN = os.path.join("harness", "experiments", "smoke-run.json")  # SMOKE filled in by smoke_file(), git-ignored
BENCH = [sys.executable, os.path.join("harness", "bench.py")]
# Measured for the claude smoke trial only: 45,896 + 1,555 and 49,652 + 1,509 input + output tokens.
# A Goose trial costs what its provider and model charge; bench.py prints its own expected-cost line.
SMOKE_SIZES = {"claude": "about 50k tokens", "goose": "cost depends on your provider and model",
               "codex": "counts against your Codex login"}
DEMO_COMMIT = "1755859a"
OVERLAY = {"env.override": ".env.override", "compose.overlay.yaml": "overlay/compose.overlay.yaml",
           "jaeger-config.yml": "overlay/jaeger-config.yml",
           "otelcol-config-extras.yml": "overlay/otelcol-config-extras.yml"}
COMPOSE = ["docker", "compose", "--env-file", ".env", "--env-file", ".env.override", "-f", "compose.yaml",
           "-f", "compose.full.yaml", "-f", "compose.observability.yaml", "-f", "overlay/compose.overlay.yaml"]
PULL_TRIES, PULL_BACKOFF_S = 4, 10  # give up after PULL_TRIES attempts in a row with no new image; pulls resume per layer
READY_BUDGET_S, READY_SLEEP_S = 600, 30  # the ready wait: 10 min, a check every 30 s
GOTCHAS = ("fixture/FIXTURE.md, Gotchas", "known fixture failures and fixes")
NAME_W = 13

# How the screen looks: set by main() from sys.stdout. tty: live redraw; color; glyphs: ✓ ✗ · and braille.
TTY = COLOR = GLYPHS = False
CHILD = STEP = None


class Failed(Exception):
    """A step failed: its end text, the lines printed under it, and (command, why) pairs to try."""
    def __init__(self, text, block=(), tries=()):
        super().__init__(text)
        self.text, self.block, self.tries = text, list(block), list(tries)


def set_mode(stream):
    global TTY, COLOR, GLYPHS
    TTY = stream.isatty() and os.environ.get("TERM") != "dumb"
    COLOR, GLYPHS = bench.use_color(stream), TTY and bench.glyphs(stream)


def out(msg=""):
    print(msg if COLOR else bench.ANSI.sub("", msg), flush=True)


def sep():
    return " · " if GLYPHS else ", "


def mark(kind):
    """ok / FAIL / a spinner frame, as a glyph on a UTF-8 TTY, else as the word bench.py prints."""
    if kind == "ok":
        return paint(GREEN, "✓" if GLYPHS else "ok")
    if kind == "fail":
        return paint(BOLD_RED, "✗" if GLYPHS else "FAIL")
    return paint(CYAN, kind)


def line(kind, name, text):
    if not TTY:  # 'ok    Images       23 pulled, 4m02s'
        return "%-6s%s%s" % ({"ok": "ok", "fail": "FAIL"}.get(kind, ".."), name.ljust(NAME_W), text)
    w = 1 if GLYPHS else 4
    m = mark(kind)
    return "  " + m + " " * (w + 1 - len(bench.ANSI.sub("", m))) + paint("1", name.ljust(NAME_W)) + text


class Step:
    """One step's line: a spinner redrawn in place on a TTY; on a pipe a start line once it has run a
    second, then a heartbeat at most once a minute. ok() or fail() resolves it."""
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, name, text):
        global STEP
        STEP, self.name, self.text, self.t0 = self, name, text, time.monotonic()
        self.stop, self.drawn = threading.Event(), False
        self.thread = threading.Thread(target=self.tick, daemon=True)
        self.thread.start()

    def elapsed(self):
        return dur(time.monotonic() - self.t0)

    def tick(self):
        frames, i, said = self.FRAMES if GLYPHS else "-\\|/", 0, 0.0
        while not self.stop.wait(0.1):
            if TTY:
                s = line(frames[i % len(frames)], self.name, self.text + paint(DIM, sep() + self.elapsed()))
                cols = shutil.get_terminal_size().columns - 1
                if len(bench.ANSI.sub("", s)) > cols:  # a wrapped line cannot be redrawn in place
                    s = bench.ANSI.sub("", s)[:cols]
                sys.stdout.write("\r\x1b[K" + (s if COLOR else bench.ANSI.sub("", s)))
                sys.stdout.flush()
                self.drawn, i = True, i + 1
            elif time.monotonic() - self.t0 - said >= (60 if said else 1):
                said = time.monotonic() - self.t0
                out(line("..", self.name, self.text + (sep() + self.elapsed() if said >= 60 else "")))

    def end(self, kind, text):
        global STEP
        self.stop.set()
        self.thread.join()
        STEP = None
        if self.drawn:
            sys.stdout.write("\r\x1b[K")
        out(line(kind, self.name, text))

    def ok(self, text, timed=True):
        self.end("ok", text + (paint(DIM, sep() + self.elapsed()) if timed else ""))

    def fail(self, text):
        self.end("fail", text)


def sh(argv, cwd=None, merge=True, on_line=None):
    """Run a third-party command, its output appended to .make.log. merge: stderr joins stdout (both
    returned and passed to on_line line by line); otherwise stdout alone is returned and stderr goes
    to the log only (stdout is data for the caller, so it stays out of the log and its failure tail). Returns (exit code, output lines)."""
    global CHILD
    with open(LOG, "a", encoding="utf-8") as log:
        log.write("$ %s\n" % shlex.join(argv))
        log.flush()
        with subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT if merge else log, text=True, errors="replace") as CHILD:
            lines = []
            for l in CHILD.stdout:
                if merge:
                    log.write(l)
                lines.append(l.rstrip("\n"))
                if on_line:
                    on_line(lines[-1])
        rc, CHILD = CHILD.returncode, None
    return rc, lines


def log_tail(n=20):
    with open(LOG, encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()[-n:]


def must(rc, what):
    """A third-party command's failure: its exit code and the log's last 20 lines."""
    if rc:
        raise Failed("%s failed (exit %d)" % (what, rc), ["%s, last 20 lines of its output:" % what]
                     + ["  " + l for l in log_tail()])


def fail_block(step, text, block, tries):
    """The failed step's line, then what broke, what to try, where the log is."""
    step.fail(text)
    if block:
        out()
        for l in block:
            out("  " + l)
    out()
    tries = list(tries) + [GOTCHAS]
    w = max(len(t) for t, _ in tries) + 2
    for i, (t, why) in enumerate(tries):
        out("  %s%s%s" % ("Try:  " if i == 0 else "      ", t.ljust(w), paint(DIM, why)))
    out("  Log:  " + bench.shown(LOG))


def header(cmd):
    out("\n  " + paint("1", "jaeger-ai-evals") + "  " + cmd + "\n" if TTY else "jaeger-ai-evals " + cmd)


def first_token(argv, digit=False):
    """The first word a command prints ("?" when none); with digit, the first word starting with a digit."""
    try:
        words = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30).stdout.split()
        return next((w for w in words if w[:1].isdigit() or not digit), "?")
    except (OSError, subprocess.TimeoutExpired):
        return "?"


def host_only(cfg):
    return cfg.get("FIXTURE_HOST_ONLY") == "1"


def agent_client(cfg):
    return config.client_name(cfg.get("CLIENT", ""))


def smoke_size(cfg):
    return SMOKE_SIZES.get(agent_client(cfg), SMOKE_SIZES["goose"])


def goose_needed(cfg):
    """The Goose image is needed by a goose smoke and by the goose grader (bench.py pre-flight checks both)."""
    agent = agent_client(cfg)
    return agent == "goose" or config.grader_client(cfg, agent) == "goose"


def smoke_file(cfg):
    """The experiment make smoke dry-runs and runs: SMOKE filled in with Goose's provider and model from
    GOOSE_PROVIDER and GOOSE_MODEL and no effort (as measure.sh writes its pair file), or for CLIENT=claude or codex
    with CLAUDE_MODEL or CODEX_MODEL and the template's effort."""
    client, provider, model = agent_client(cfg), cfg.get("GOOSE_PROVIDER", ""), cfg.get("GOOSE_MODEL", "")
    if client in ("claude", "codex"):
        key = client.upper() + "_MODEL"
        provider, model = None, cfg.get(key, "")
        if not model:
            raise Failed("%s is empty" % key, tries=[("set %s in fixture.env" % key, "a full model id")])
    elif client != "goose":
        raise Failed("CLIENT is %r, not goose, claude or codex" % cfg.get("CLIENT", ""),
                     tries=[("set CLIENT in fixture.env or make smoke CLIENT=claude", "goose, claude or codex")])
    elif provider not in goose_client.KEYS or not model:
        raise Failed("GOOSE_PROVIDER is %r; it must be anthropic or openrouter" % provider if provider not in goose_client.KEYS
                     else "GOOSE_MODEL is empty in fixture.env",
                     ["GOOSE_PROVIDER must be anthropic or openrouter and GOOSE_MODEL a full model id of it;",
                      "the key (ANTHROPIC_API_KEY or OPENROUTER_API_KEY) goes in fixture.env too.",
                      "The Jaeger AI Assistant demo's scripts/ai-sidecar/goose uses the same two settings."],
                     [("set GOOSE_PROVIDER, GOOSE_MODEL and the key in fixture.env", "then run again"),
                      ("make smoke CLIENT=claude", "or smoke on Claude Code")])
    with open(os.path.join(ROOT, SMOKE), encoding="utf-8") as f:
        e = json.load(f)
    e["run"] = dict(client=client, provider=provider, model=model, **e["run"])
    if client != "claude":
        del e["run"]["max_budget_usd"]  # only the claude client enforces it
    if client == "goose":
        e["run"]["effort"] = None  # no GOOSE_THINKING_EFFORT, as the Jaeger demo's scripts/ai-sidecar/goose
    with open(os.path.join(ROOT, SMOKE_RUN), "w", encoding="utf-8") as f:
        json.dump(e, f, indent=2)
        f.write("\n")
    return SMOKE_RUN


def tools(cfg):
    def mm(v):
        v = v.lstrip("v")
        return ".".join(v.split(".")[:2]) if re.match(r"\d+(\.\d+)+", v) else None
    docker, compose = (mm(first_token(a)) for a in (["docker", "version", "--format", "{{.Server.Version}}"],
                                                    ["docker", "compose", "version", "--short"]))
    if docker is None and cfg.get("FIXTURE_SSH_USER"):  # absent, a WSL shim or a stopped daemon: the probe cannot tell which
        versions = ("local docker not running (remote fixture)",)
    else:
        versions = ("docker " + (docker or "?"), "compose " + (compose or "?"))
    versions += ("python %d.%d" % sys.version_info[:2],) + (() if host_only(cfg) else agent_tools(cfg))
    out(line("ok", "Tools", sep().join(versions)))


def agent_tools(cfg):
    """The Tools line's agent part: Goose's settings (never the key) when it is the smoke client; claude when it is the
    smoke client, and the claude grader's command (GRADER_BIN); codex when it is the smoke client. Login is not checked
    here (preflight.sh does)."""
    parts, p, key = [], cfg.get("GOOSE_PROVIDER", ""), None
    agent = agent_client(cfg)
    if agent == "goose":
        key = goose_client.KEYS.get(p)
        ok = key and cfg.get("GOOSE_MODEL") and cfg.get(key)
        parts.append("goose %s %s, %s set" % (p, cfg["GOOSE_MODEL"], key) if ok else "goose settings incomplete")
    cmds = (["claude"] if agent == "claude" else []) + (
        [cfg.get("GRADER_BIN") or "claude"] if config.grader_client(cfg, agent) == "claude" else [])
    for c in dict.fromkeys(cmds):  # one entry when the agent and the grader run the same command
        parts.append("%s %s" % (c, first_token([c, "--version"])))
    if agent == "codex":  # "codex-cli 0.159.0": the first token starting with a digit
        v = first_token([cfg.get("CODEX_BIN") or "codex", "--version"], digit=True)
        parts.append("codex " + v)
    return tuple(parts)


def demo_dir(cfg):
    """FIXTURE_DEMO_DIR, absolute or relative to the home directory, as bench.fixture_sh reads it."""
    if cfg.get("FIXTURE_SSH_USER"):
        raise Failed("FIXTURE_SSH_USER is set: run make on the fixture host")
    return os.path.join(os.path.expanduser("~"), os.path.expanduser(cfg["FIXTURE_DEMO_DIR"]))


def fixture(cfg):
    """FIXTURE.md, Install. Never overwrites fixture.env or the pristine flag file."""
    d = demo_dir(cfg)
    st = Step("Demo", "checking %s" % cfg["FIXTURE_DEMO_DIR"])
    cloned = not os.path.isdir(d)
    if cloned:
        st.text = "cloning OpenTelemetry Demo 3.0.0"
        must(sh(["git", "clone", "--branch", "3.0.0", "--depth", "1",
                 "https://github.com/open-telemetry/opentelemetry-demo", d])[0], "git clone")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d, capture_output=True, text=True).stdout.strip()
    if not head.startswith(DEMO_COMMIT):
        raise Failed("%s is at %s, not tag 3.0.0 (%s)" % (cfg["FIXTURE_DEMO_DIR"], head[:8] or "no commit", DEMO_COMMIT),
                     ["if it is a partial clone, remove %s and re-run make fixture" % cfg["FIXTURE_DEMO_DIR"]])
    os.makedirs(os.path.join(d, "overlay"), exist_ok=True)
    fresh, replaced = False, []
    for src, dst in OVERLAY.items():
        src, dst = os.path.join(ROOT, "fixture", src), os.path.join(d, dst)
        if not os.path.isfile(dst):
            fresh = True
        elif not filecmp.cmp(src, dst, shallow=False):
            replaced.append(os.path.basename(dst))
            shutil.copyfile(dst, dst + ".bak")
        shutil.copyfile(src, dst)
    pristine = os.path.join(d, cfg["FIXTURE_PRISTINE_FLAG_FILE"])
    if not os.path.isfile(pristine):
        shutil.copyfile(os.path.join(d, cfg["FIXTURE_FLAG_FILE"]), pristine)
    if replaced:
        st.ok("overlay updated (%s; old cop%s kept as .bak)" % (", ".join(replaced), "ies" if len(replaced) > 1 else "y"), False)
    else:
        st.ok("OpenTelemetry Demo 3.0.0%s" % (" cloned, overlay installed" if cloned else ", overlay installed" if fresh
                                              else ", overlay up to date"), cloned)


def pulled_image(l, services):
    """The image a compose --progress plain line reports pulled ('frontend Pulled', or 'Image <ref> Pulled'), else None."""
    m = re.match(r"\s*(?:Image\s+)?(\S+)\s+Pulled\s*$", l)
    return m and services.get(m.group(1), m.group(1))


def images(d):
    """Pull only the images not present locally, counting completed pulls in compose's plain progress."""
    st = Step("Images", "checking")
    rc, conf = sh(COMPOSE + ["config", "--format", "json"], cwd=d, merge=False)
    must(rc, "docker compose config")
    services = {n: s["image"] for n, s in json.loads("\n".join(conf))["services"].items() if s.get("image")}
    want = sorted(set(services.values()))
    missing = [i for i in want if subprocess.run(["docker", "image", "inspect", i], stdout=subprocess.DEVNULL,
                                                 stderr=subprocess.DEVNULL).returncode]
    if not missing:
        st.ok("%d present" % len(want), False)
        return services
    pulled = set()

    def count(l):
        if pulled_image(l, services) in missing:
            pulled.add(pulled_image(l, services))
            st.text = "pulling %d of %d" % (len(pulled), len(missing))

    st.text = "pulling %d image%s" % (len(missing), "s" if len(missing) > 1 else "")
    stalls = 0  # attempts in a row that completed no new image; a pull that keeps progressing keeps going
    while True:
        before = len(pulled)
        rc, _ = sh(COMPOSE[:2] + ["--progress", "plain"] + COMPOSE[2:] + ["pull"]
                   + sorted(n for n, i in services.items() if i in missing), cwd=d, on_line=count)
        stalls = 0 if len(pulled) > before else stalls + 1
        if rc == 0 or stalls >= PULL_TRIES:
            break
        wait = PULL_BACKOFF_S * max(stalls, 1)
        out(line("..", "Images", "pull failed at %d of %d, retrying in %ds" % (len(pulled), len(missing), wait)))
        time.sleep(wait)
    must(rc, "docker compose pull")
    present = len(want) - len(missing)
    st.ok("%d pulled" % len(missing) + (sep() + "%d present" % present if present else ""))
    return services


def goose_image(cfg):
    """bench.py's pre-flight needs the pinned Goose image; pull it like the demo's when absent. A FIXTURE_HOST_ONLY host
    runs no agent, so it never needs it."""
    if host_only(cfg) or not goose_needed(cfg):
        return
    st, image = Step("Goose image", "checking"), goose_client.IMAGE
    if not subprocess.run(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        return st.ok("present", False)
    st.text = "pulling " + image.split("@")[0]
    must(sh(["docker", "pull", image])[0], "docker pull")
    st.ok("pulled")


def containers(d, services):
    st = Step("Containers", "starting %d" % len(services))
    rc, lines = sh(COMPOSE[:2] + ["--progress", "plain"] + COMPOSE[2:] + ["up", "-d"], cwd=d)
    must(rc, "docker compose up")
    rc, ids = sh(COMPOSE + ["ps", "-q"], cwd=d, merge=False)
    must(rc, "docker compose ps")
    recreated = sorted({m.group(1) for m in (re.search(r"Container (\S+)\s+Recreated", l) for l in lines) if m})
    started = any(re.search(r"Container \S+\s+Started", l) for l in lines)
    st.ok("%d %s" % (len([i for i in ids if i.strip()]), "started" if started else "running")
          + (sep() + ", ".join(recreated) + " recreated" if recreated else ""))


# A failed smoke dry run's first FAIL, in short form; the first matching row wins.
SHORT = [
    (r"claude is not on PATH", "claude is not on PATH (the claude client needs it)"),
    (r"not logged in|no model request with a JSON body", "claude is not logged in, or its request never arrived"),
    (r"docker is not on PATH", "docker is not installed, and the Goose client runs in a container"),
    (r"pinned Goose image is not on this machine", "the pinned Goose image is not on this machine"),
    (r"(run\.provider|grader provider) is \w+ but \w+ is empty", "the provider's API key is empty in fixture.env"),
    (r"grader model is empty", "the grader model is empty"),
    (r"grader provider is '", "the grader provider is not anthropic or openrouter"),
    (r"GRADER_CLIENT is", "GRADER_CLIENT is not goose or claude"),
    (r"does not accept", "the Claude Code CLI lacks flags the harness passes"),
    (r"leak|name no fault", "a leak check failed"),
    (r"harness trajectories in Jaeger's store", "Jaeger's store holds exported trajectories; restart the jaeger container"),
]
TRIES = {
    r"^claude is not on PATH": ("install the Claude Code CLI, run claude, then /login", "only the claude client needs it; the default smoke runs Goose"),
    r"^claude is": ("claude, then /login", "log the Claude Code CLI in"),
    r"^docker is not installed": ("install Docker Engine or Docker Desktop", "or GRADER_CLIENT=claude with CLIENT=claude"),
    r"^the pinned Goose image": ("docker pull " + goose_client.IMAGE, "the image the Goose client and grader run"),
    r"^the provider's API key": ("set OPENROUTER_API_KEY or ANTHROPIC_API_KEY in fixture.env", "the key of GOOSE_PROVIDER (or GRADER_PROVIDER)"),
    r"^the grader model": ("set GRADER_MODEL or GOOSE_MODEL in fixture.env", "a full model id, or GRADER_CLIENT=claude"),
    r"^the grader provider": ("set GRADER_PROVIDER or GOOSE_PROVIDER in fixture.env", "anthropic or openrouter"),
    r"^GRADER_CLIENT": ("set GRADER_CLIENT in fixture.env", "goose or claude"),
    r"^the Claude Code CLI": ("claude update", "a CLI that accepts the harness flags"),
    r"containers running": ("cd %s && docker compose ps -a", "which container is not up"),
}


def blocker(output):
    """(short text, report lines) for a failed dry run, from its first FAIL check (with that
    check's detail), else bench.py's abort line, else its last line."""
    lines = [l for l in output.splitlines() if l.strip()] or ["no output"]
    i = next((k for k, l in enumerate(lines) if re.match(r"\s*FAIL\s", l) or l.startswith("bench: ")), len(lines) - 1)
    first, check = lines[i], re.match(r"\s*FAIL\s+(.*)", lines[i])
    report = [first.strip()]
    if check:
        report = [mark("fail") + " " + check.group(1)] + ["  " + l.strip() for l in lines[i + 1:i + 7] if l.startswith("        ")]
    detail = "\n".join([first] + report)
    for pat, short in SHORT:
        if re.search(pat, detail):
            return short, report
    text = re.sub(r"^\s*(FAIL\s+|bench: (ABORT - )?)", "", first)
    return text[:70] + ("..." if len(text) > 70 else ""), report


def checks_passed(output):
    return len(re.findall(r"(?m)^\s+ok\s", output))


def dry_run(exp):
    rc, lines = sh(BENCH + ["run", exp, "--dry-run"], cwd=ROOT)
    return rc, bench.ANSI.sub("", "\n".join(lines))  # FORCE_COLOR reaches the child; the counts and regexes need plain text


def preflight_failed(cfg, text, report, exp):
    t = next((t for k, t in TRIES.items() if re.search(k, text)), None)
    tries = [(t[0].replace("%s", cfg["FIXTURE_DEMO_DIR"]), t[1])] if t else [
        ("python3 harness/bench.py run %s --dry-run" % exp, "the full pre-flight report")]
    return Failed(text, ["bench.py pre-flight:"] + ["  " + l for l in report], tries)


def ready(cfg):
    """FIXTURE.md, Bring up: wait on the fixture alone (MIN_CONTAINERS running, Jaeger answering), whatever the
    agent settings; bench.py's pre-flight runs in every bench.py run and in make smoke's dry run."""
    st, why = Step("Ready", "waiting for the fixture"), ""
    with open(os.path.join(ROOT, SMOKE), encoding="utf-8") as f:  # the baseline make smoke's pre-flight asks for
        with open(os.path.join(ROOT, "harness", "scenarios", json.load(f)["scenario"] + ".json"), encoding="utf-8") as g:
            service = json.load(g)["ground_truth"]["service"]
    while True:
        n = len(sh(["docker", "ps", "-q"], merge=False)[1])
        try:
            with urllib.request.urlopen(config.jaeger_base(dict(cfg, FIXTURE_HOST="localhost")) + "/", timeout=5):
                why = "%d of %s containers" % (n, cfg["MIN_CONTAINERS"]) if n < int(cfg["MIN_CONTAINERS"]) else ""
        except OSError:
            why = "Jaeger not answering"
        if not why:  # Jaeger answers; the load generator needs a few minutes more to fill its in-memory store
            try:
                seen = len(bench.traces(dict(cfg, FIXTURE_HOST="localhost"), service, bench.now_us() - bench.BASELINE_LOOKBACK_S * 10**6,
                                        bench.now_us(), 20, 10))
            except Exception:
                seen = 0
            why = "waiting for %s traces (%d of 3)" % (service, seen) if seen < 3 else ""
        if not why:
            st.ok("%d containers running, Jaeger answers, %s sending traces" % (n, service))
            return
        if time.monotonic() - st.t0 + READY_SLEEP_S > READY_BUDGET_S:
            raise Failed("gave up after %s: %s" % (st.elapsed(), why), [], [("docker compose ps -a", "which container is not up")])
        st.text = why
        time.sleep(READY_SLEEP_S)


def phoenix(cfg, d):
    """FIXTURE.md, Trajectory store. Phoenix takes one to two minutes to answer."""
    st = Step("Phoenix", "starting")
    must(sh(COMPOSE + ["--profile", "store", "up", "-d", "--no-deps", "phoenix"], cwd=d)[0], "docker compose up phoenix")
    # Survive a reboot like the demo's services; the overlay stays untouched (its hash is recorded in every batch).
    for cid in sh(COMPOSE + ["--profile", "store", "ps", "-q", "phoenix"], cwd=d, merge=False)[1]:
        if cid.strip():
            sh(["docker", "update", "--restart", "unless-stopped", cid.strip()])
    st.text = "waiting for port %s" % cfg["PHOENIX_PORT"]
    for _ in range(60):
        try:
            with urllib.request.urlopen("http://localhost:%s/" % cfg["PHOENIX_PORT"], timeout=5) as r:
                if r.status == 200:
                    break
        except OSError:
            pass
        time.sleep(5)
    else:
        raise Failed("no answer on port %s after %s" % (cfg["PHOENIX_PORT"], st.elapsed()),
                     tries=[("cd %s && docker compose logs phoenix" % cfg["FIXTURE_DEMO_DIR"], "why it is not up")])
    st.ok("trajectory store up")


def smoke_ready(cfg):
    """True when make smoke can start: a claude or codex smoke, or a Goose smoke with provider, model and key set."""
    key = goose_client.KEYS.get(cfg.get("GOOSE_PROVIDER", ""))
    return agent_client(cfg) != "goose" or bool(key and cfg.get("GOOSE_MODEL") and cfg.get(key))


def footer(cfg, t0):
    jaeger, phx = config.jaeger_base(cfg), "http://%s:%s" % (cfg["FIXTURE_HOST"], cfg["PHOENIX_PORT"])
    if not TTY:
        out("Ready in %s. Jaeger UI %s  Phoenix %s" % (dur(time.monotonic() - t0), jaeger, phx))
        out("Next: make smoke    one agent trial, %s" % smoke_size(cfg))
        if not smoke_ready(cfg):
            out("Before it: set OPENROUTER_API_KEY in fixture.env (another provider: GOOSE_PROVIDER, GOOSE_MODEL and its key)")
        return
    out("\n  " + paint("1", "Ready in %s" % dur(time.monotonic() - t0)) + "\n")
    out("    Jaeger UI   " + jaeger)
    out("    Phoenix     " + phx + "\n")
    out("  Next: " + paint("1", "make smoke") + "    " + paint(DIM, "one agent trial, %s" % smoke_size(cfg)) + "\n")
    if not smoke_ready(cfg):
        out("  Before it: set OPENROUTER_API_KEY in fixture.env (another provider: GOOSE_PROVIDER, GOOSE_MODEL and its key)\n")


def smoke(cfg, dry=False):
    """The dry run, a 5 s window to cancel, then bench.py's own run output (this process becomes bench.py); dry stops after the dry run."""
    st = Step("Pre-flight", "dry run")
    exp = smoke_file(cfg)
    rc, output = dry_run(exp)
    if rc:
        text, report = blocker(output)
        raise preflight_failed(cfg, text, report, exp=exp)
    n = checks_passed(output)
    st.ok("%d of %d%sdry run, nothing spent" % (n, n, sep()), False)
    with open(os.path.join(ROOT, exp), encoding="utf-8") as f:
        e = json.load(f)
    pad = "  " if TTY else ""
    out("\n" + pad + "One agent trial: " + sep().join((e["run"]["model"], "arm " + ", ".join(e["arms"]), smoke_size(cfg))))
    if dry:
        out(pad + "dry run only; nothing spent")
        return
    if TTY and sys.stdin.isatty():
        for s in range(5, 0, -1):
            sys.stdout.write("\r\x1b[K  Starting in %ds, Ctrl-C to cancel" % s)
            sys.stdout.flush()
            time.sleep(1)
        sys.stdout.write("\r\x1b[K\n")
    else:
        out(pad + "Starting in 5s, Ctrl-C to cancel\n")
        time.sleep(5)
    sys.stdout.flush()
    os.chdir(ROOT)
    os.execv(BENCH[0], BENCH + ["run", exp])


def down(cfg, remove=False):
    d = demo_dir(cfg)
    st = Step("Containers", "removing" if remove else "stopping")
    ids = [i for i in sh(COMPOSE + ["--profile", "store", "ps", "-q"], cwd=d, merge=False)[1] if i.strip()]
    must(sh(COMPOSE + ["--profile", "store", "down" if remove else "stop"], cwd=d)[0],
         "docker compose " + ("down" if remove else "stop"))
    st.ok("%d removed, Jaeger traces gone; volumes and images kept" % len(ids) if remove else "%d stopped; Jaeger's traces are lost (in-memory store), Phoenix keeps its own" % len(ids))
    if not remove:
        out(("  " if TTY else "") + "Next: make up to start them again")


def test():
    """The unit tests, their output captured; on failure the full report and a rerun line per failing test."""
    tests = os.path.join(ROOT, "harness", "tests")
    st = Step("Tests", "running")
    st.text = "running %d" % unittest.defaultTestLoader.discover(tests).countTestCases()
    r = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", tests], cwd=ROOT,
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    report = r.stdout + r.stderr
    ran = re.search(r"^Ran (\d+) test", report, re.M)
    ran = int(ran.group(1)) if ran else 0
    if r.returncode == 0:
        skipped = int((re.search(r"skipped=(\d+)", report) or [0, 0])[1])
        st.ok("%d passed" % (ran - skipped) + (", %d skipped" % skipped if skipped else ""))
        return 0
    bad = re.findall(r"^(?:FAIL|ERROR): \S+ \((\S+)\)", report, re.M)
    st.fail("%d of %d failed" % (len(bad), ran) + paint(DIM, sep() + st.elapsed()))
    out("\n" + report.rstrip() + "\n")
    for t in bad:
        out("  rerun: %s -B -m unittest discover -s harness/tests -k %s" % (os.path.basename(sys.executable), t))
    return 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    dry = argv[1:] == ["--dry-run"] and argv[0] == "smoke"
    if (len(argv) != 1 and not dry) or argv[0] not in ("setup", "tools", "fixture", "up", "phoenix", "smoke", "down", "clean", "test"):
        print(__doc__.strip())
        return 2
    cmd, t0 = argv[0], time.monotonic()
    set_mode(sys.stdout)
    if cmd in ("setup", "smoke"):
        open(LOG, "w").close()
    if cmd in ("setup", "fixture") and not os.path.isfile(os.path.join(ROOT, "fixture.env")):
        shutil.copyfile(os.path.join(ROOT, "fixture.env.example"), os.path.join(ROOT, "fixture.env"))
    cfg = config.load(ROOT)
    if cfg.get("FIXTURE_BIND"):
        os.environ.setdefault("FIXTURE_BIND", cfg["FIXTURE_BIND"])  # compose reads it from the environment
    try:
        if cmd == "test":
            return test()
        if cmd != "tools":
            header(cmd)
        if cmd in ("setup", "tools", "smoke"):
            tools(cfg)
        if cmd == "smoke":
            smoke(cfg, dry)
        if cmd in ("down", "clean"):
            down(cfg, remove=cmd == "clean")
        if cmd in ("setup", "fixture"):
            fixture(cfg)
        if cmd in ("setup", "up"):
            d = demo_dir(cfg)
            services = images(d)
            goose_image(cfg)
            containers(d, services)
            ready(cfg)
        if cmd in ("setup", "phoenix") or (cmd == "up" and any(sh(COMPOSE + ["--profile", "store", "ps", "-aq", "phoenix"], cwd=demo_dir(cfg), merge=False)[1])):
            phoenix(cfg, demo_dir(cfg))
        if cmd == "setup":
            footer(cfg, t0)
    except Failed as e:
        if STEP:
            fail_block(STEP, e.text, e.block, e.tries)
        else:  # before any step: FIXTURE_SSH_USER set
            out(line("fail", "Settings", e.text))
        return 1
    except KeyboardInterrupt:
        if CHILD:
            CHILD.terminate()
            CHILD.wait()
        if STEP:
            STEP.fail("Cancelled")
        else:  # smoke's countdown
            out(line("fail", "Cancelled", "nothing spent"))
        out(("  " if TTY else "") + "re-run make %s to continue" % cmd)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
