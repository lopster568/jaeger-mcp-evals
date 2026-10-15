#!/usr/bin/env python3
"""fixture_cli.py: what the make targets run, one line per step (fixture/FIXTURE.md stays the manual).

  fixture_cli.py setup     tools, fixture, up, phoenix: everything free, in order
  fixture_cli.py tools     the Tools line (after fixture/preflight.sh has checked them)
  fixture_cli.py fixture   clone the OpenTelemetry Demo 3.0.0 into FIXTURE_DEMO_DIR, install the overlay
  fixture_cli.py up        pull missing images, start the demo, wait until the smoke dry run passes pre-flight
  fixture_cli.py phoenix   start the Phoenix trajectory store and wait until it answers
  fixture_cli.py smoke     the smoke dry run, then ONE paid agent trial
  fixture_cli.py down      stop the demo containers, keeping their data
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
from bench import BOLD_RED, CYAN, DIM, GREEN, dur, paint

ROOT = config.ROOT
LOG = os.path.join(ROOT, ".make.log")
SMOKE = os.path.join("harness", "experiments", "smoke-paymentfailure.json")
BENCH = [sys.executable, os.path.join("harness", "bench.py")]
# Measured 2026-10-15 for this smoke trial: 45,896 + 1,555 and 49,652 + 1,509 input + output tokens.
SMOKE_SIZE = "about 50k tokens"
DEMO_COMMIT = "1755859a"
OVERLAY = {"env.override": ".env.override", "compose.overlay.yaml": "overlay/compose.overlay.yaml",
           "jaeger-config.yml": "overlay/jaeger-config.yml", "otelcol-config-extras.yml": "overlay/otelcol-config-extras.yml"}
COMPOSE = ["docker", "compose", "--env-file", ".env", "--env-file", ".env.override", "-f", "compose.yaml",
           "-f", "compose.full.yaml", "-f", "compose.observability.yaml", "-f", "overlay/compose.overlay.yaml"]
READY_BUDGET_S, READY_SLEEP_S, THRASH_STOP = 600, 30, 3  # the ready wait: 10 min, a dry run every 30 s
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
    TTY, COLOR, GLYPHS = stream.isatty(), bench.use_color(stream), bench.glyphs(stream)


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
    to the log only. Returns (exit code, output lines)."""
    global CHILD
    with open(LOG, "a", encoding="utf-8") as log:
        log.write("$ %s\n" % shlex.join(argv))
        log.flush()
        with subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT if merge else log, text=True, errors="replace") as CHILD:
            lines = []
            for l in CHILD.stdout:
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
    out("\n  " + paint("1", "jaeger-mcp-evals") + "  " + cmd + "\n" if TTY else "jaeger-mcp-evals " + cmd)


def first_token(argv):
    try:
        return (subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30).stdout.split()
                or ["?"])[0]
    except (OSError, subprocess.TimeoutExpired):
        return "?"


def tools():
    mm = lambda v: ".".join(v.lstrip("v").split(".")[:2])
    versions = ("docker " + mm(first_token(["docker", "version", "--format", "{{.Server.Version}}"])),
                "compose " + mm(first_token(["docker", "compose", "version", "--short"])),
                "python %d.%d" % sys.version_info[:2], "claude " + first_token(["claude", "--version"]))
    out(line("ok", "Tools", sep().join(versions) + ", logged in"))


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
    rc, _ = sh(COMPOSE[:2] + ["--progress", "plain"] + COMPOSE[2:] + ["pull"]
               + sorted(n for n, i in services.items() if i in missing), cwd=d, on_line=count)
    must(rc, "docker compose pull")
    present = len(want) - len(missing)
    st.ok("%d pulled" % len(missing) + (sep() + "%d present" % present if present else ""))
    return services


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


def si(x):
    return "%.1fM" % (x / 1e6) if x >= 1e6 else "%dk" % (x // 1000) if x >= 1000 else "%d" % x


# A pre-flight blocker: (pattern, retry?, short text). Retry only for the fixture warming up; the first
# match wins and anything else stops at once, since waiting cannot fix it. "thrash" retries THRASH_STOP times.
BLOCKERS = [
    (r"only (\d+) running containers \(need >= (\d+)\)", True, "containers starting, {0} of {1} running"),
    (r"could not read container count|could not read the running jaeger image", True, "waiting for the containers"),
    (r"only \d+ baseline traces for service=([\w-]+)", True, "waiting for {0} traces"),
    (r"could not capture tools/list", True, "waiting for Jaeger's MCP tools"),
    (r"OFREP reports", True, "waiting for flagd"),
    (r"FAIL\s+fixture reachable", True, "fixture not reachable"),
    (r"telemetry names no flag", True, "waiting for clean telemetry"),
    (r"host not thrashing \(vmstat bi mean ([\d.]+)", "thrash", "host thrashing, {0} blocks/s read (limit 50k)"),
    (r"not logged in|no model request with a JSON body", False, "claude is not logged in, or its request never arrived"),
    (r"does not accept", False, "the Claude Code CLI lacks flags the harness passes"),
    (r"FAIL\s+sandbox probe", False, "the sandbox probe failed"),
    (r"leak", False, "a leak check failed"),
    (r"no arm of experiment", False, "the fixture runs a jaeger image the smoke experiment does not expect"),
]
TRIES = {
    "claude is": ("claude, then /login", "log the Claude Code CLI in"),
    "the Claude Code CLI": ("claude update", "a CLI that accepts the harness flags"),
    "host thrashing": ("docker stats --no-stream", "find the container reading the disk"),
    "containers starting": ("cd %s && docker compose ps -a", "which container is not up"),
}


def blocker(output):
    """(short text, retry?, report lines) for a failed dry run, from its first FAIL check (with that
    check's detail), else bench.py's abort line, else its last line."""
    lines = [l for l in output.splitlines() if l.strip()] or ["no output"]
    i = next((k for k, l in enumerate(lines) if re.match(r"\s*FAIL\s", l) or l.startswith("bench: ")), len(lines) - 1)
    first, check = lines[i], re.match(r"\s*FAIL\s+(.*)", lines[i])
    report = [first.strip()]
    if check:
        report = [mark("fail") + " " + check.group(1)] + ["  " + l.strip() for l in lines[i + 1:i + 7] if l.startswith("        ")]
    for pat, retry, short in BLOCKERS:
        m = re.search(pat, "\n".join([first] + report))
        if m:
            return short.format(*[si(float(x)) if retry == "thrash" else x for x in m.groups()]), retry, report
    text = re.sub(r"^\s*(FAIL\s+|bench: (ABORT - )?)", "", first)
    return text[:70] + ("..." if len(text) > 70 else ""), False, report


def checks_passed(output):
    return len(re.findall(r"(?m)^\s+ok\s", output))


def dry_run():
    rc, lines = sh(BENCH + ["run", SMOKE, "--dry-run"], cwd=ROOT)
    return rc, "\n".join(lines)


def preflight_failed(cfg, text, report, prefix=""):
    t = next((t for k, t in TRIES.items() if text.startswith(k)), None)
    tries = [(t[0].replace("%s", cfg["FIXTURE_DEMO_DIR"]), t[1])] if t else [
        ("python3 harness/bench.py run %s --dry-run" % SMOKE, "the full pre-flight report")]
    return Failed(prefix + text, ["bench.py pre-flight, last attempt:"] + ["  " + l for l in report], tries)


def ready(cfg):
    """FIXTURE.md, Bring up: wait until bench.py's own pre-flight passes, through the smoke dry run."""
    st, thrash = Step("Ready", "running pre-flight"), 0
    while True:
        rc, output = dry_run()
        if rc == 0:
            n = checks_passed(output)
            st.ok("pre-flight %d of %d" % (n, n))
            return
        text, retry, report = blocker(output)
        thrash = thrash + 1 if retry == "thrash" else 0
        if not retry:
            raise preflight_failed(cfg, text, report)
        if thrash >= THRASH_STOP:
            raise preflight_failed(cfg, text, report, "gave up after %d tries: " % thrash)
        if time.monotonic() - st.t0 + READY_SLEEP_S > READY_BUDGET_S:
            raise preflight_failed(cfg, text, report, "gave up after %s: " % st.elapsed())
        st.text = text
        time.sleep(READY_SLEEP_S)


def phoenix(cfg, d):
    """FIXTURE.md, Trajectory store. Phoenix takes one to two minutes to answer."""
    st = Step("Phoenix", "starting")
    must(sh(COMPOSE + ["--profile", "store", "up", "-d", "--no-deps", "phoenix"], cwd=d)[0], "docker compose up phoenix")
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


def footer(cfg, t0):
    jaeger, phx = config.jaeger_base(cfg), "http://%s:%s" % (cfg["FIXTURE_HOST"], cfg["PHOENIX_PORT"])
    if not TTY:
        out("Ready in %s. Jaeger UI %s  Phoenix %s" % (dur(time.monotonic() - t0), jaeger, phx))
        out("Next: make smoke    one agent trial, %s" % SMOKE_SIZE)
        return
    out("\n  " + paint("1", "Ready in %s" % dur(time.monotonic() - t0)) + "\n")
    out("    Jaeger UI   " + jaeger)
    out("    Phoenix     " + phx + "\n")
    out("  Next: " + paint("1", "make smoke") + "    " + paint(DIM, "one agent trial, %s" % SMOKE_SIZE) + "\n")


def smoke(cfg):
    """The dry run, a 5 s window to cancel, then bench.py's own run output (this process becomes bench.py)."""
    st = Step("Pre-flight", "dry run")
    rc, output = dry_run()
    if rc:
        text, _, report = blocker(output)
        raise preflight_failed(cfg, text, report)
    n = checks_passed(output)
    st.ok("%d of %d%sdry run, nothing spent" % (n, n, sep()), False)
    with open(os.path.join(ROOT, SMOKE), encoding="utf-8") as f:
        e = json.load(f)
    pad = "  " if TTY else ""
    out("\n" + pad + "One agent trial: " + sep().join((e["run"]["model"], "arm " + ", ".join(e["arms"]), SMOKE_SIZE)))
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
    os.execv(BENCH[0], BENCH + ["run", SMOKE])


def down(cfg, remove=False):
    d = demo_dir(cfg)
    st = Step("Containers", "removing" if remove else "stopping")
    ids = [i for i in sh(COMPOSE + ["--profile", "store", "ps", "-q"], cwd=d, merge=False)[1] if i.strip()]
    must(sh(COMPOSE + ["--profile", "store", "down" if remove else "stop"], cwd=d)[0],
         "docker compose " + ("down" if remove else "stop"))
    st.ok("%d removed, Jaeger traces gone; volumes and images kept" % len(ids) if remove else "%d stopped, data kept" % len(ids))
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
    if len(argv) != 1 or argv[0] not in ("setup", "tools", "fixture", "up", "phoenix", "smoke", "down", "clean", "test"):
        print(__doc__.strip())
        return 2
    cmd, t0 = argv[0], time.monotonic()
    set_mode(sys.stdout)
    if cmd in ("setup", "smoke"):
        open(LOG, "w").close()
    if cmd in ("setup", "fixture") and not os.path.isfile(os.path.join(ROOT, "fixture.env")):
        shutil.copyfile(os.path.join(ROOT, "fixture.env.example"), os.path.join(ROOT, "fixture.env"))
    cfg = config.load(ROOT)
    try:
        if cmd == "test":
            return test()
        if cmd != "tools":
            header(cmd)
        if cmd in ("setup", "tools", "smoke"):
            tools()
        if cmd == "smoke":
            smoke(cfg)
        if cmd in ("down", "clean"):
            down(cfg, remove=cmd == "clean")
        if cmd in ("setup", "fixture"):
            fixture(cfg)
        if cmd in ("setup", "up"):
            d = demo_dir(cfg)
            containers(d, images(d))
            ready(cfg)
        if cmd in ("setup", "phoenix"):
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
