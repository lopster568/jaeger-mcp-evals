#!/usr/bin/env python3
"""harness/fixture_cli.py: screen mode, blocker classification, pull counting, the failure block, and
`setup` end to end in pipe mode against docker, git and claude shims on PATH. No real docker."""
import contextlib
import http.server
import io
import json
import os
import pathlib
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import bench  # noqa: E402
import fixture_cli as fc  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
SERVICES = {"jaeger": "quay.io/jaegertracing/jaeger:2.20.0", "frontend": "ghcr.io/open-telemetry/demo:2.0.0-frontend",
            "flagd": "ghcr.io/open-feature/flagd:v0.12.9"}
# Answers what fixture_cli asks docker. FAKE_PRESENT: images already pulled; FAKE_UP_RC: compose up's exit code.
DOCKER = r"""#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
services = %s
if a[:2] == ["version", "--format"]: print("29.8.1")
elif a[:3] == ["compose", "version", "--short"]: print("5.6.0")
elif a[:2] == ["image", "inspect"]: sys.exit(0 if a[2] in os.environ.get("FAKE_PRESENT", "") else 1)
elif "config" in a: print(json.dumps({"services": {n: {"image": i} for n, i in services.items()}}))
elif "pull" in a:
    for n in a[a.index("pull") + 1:]: print(" %%s Pulling" %% n); print(" abc123 Pull complete"); print(" %%s Pulled" %% n)
elif "up" in a and "phoenix" not in a:
    print(" Container flagd  Recreated"); print(" Container jaeger  Started")
    if os.environ.get("FAKE_UP_RC", "0") != "0": print("Error response from daemon: port is already allocated")
    sys.exit(int(os.environ.get("FAKE_UP_RC", "0")))
elif "ps" in a: print("aaa\nbbb\nccc")
""" % repr(SERVICES)
GIT = """#!/bin/bash
[ "$1" = clone ] && { d="${@: -1}"; mkdir -p "$d/src/flagd"; echo '{"flags": {}}' > "$d/src/flagd/demo.flagd.json"; exit 0; }
[ "$1 $2" = "rev-parse HEAD" ] && echo 1755859a0000000000000000000000000000000
"""
CLAUDE = """#!/bin/bash
echo "2.1.300 (Claude Code)"
"""
BENCH_OK = """import sys
print("Pre-flight")
for c in ("claude accepts the flags", "fixture reachable", "jaeger image x"): print("  ok    " + c)
"""

# Real bench.py run --dry-run output tails (Log.check and die() formats), screen lines on a pipe.
THRASH = """Pre-flight
  ok    claude accepts the flags the harness passes
  FAIL  host not thrashing (vmstat bi mean 1525860.5 <= 50000)
        pre-flight: vmstat bi mean(non-first)=1525860.5 loadavg=17.83 16.74 14.14
bench: ABORT - host thrashing or vmstat unreadable: bi mean 1525860.5 (limit 50000, non-first samples)"""
TOOLS = """Pre-flight
  ok    jaeger image quay.io/jaegertracing/jaeger:2.20.0 sha256:9ae01775e2b2
        arms here: baseline
        == pre-flight: capture MCP tools/list (http://localhost:16686/jaeger/ui/api/ai/mcp/) ==
bench: ABORT - could not capture tools/list: transport error talking to MCP server: URLError(ConnectionRefusedError(111, 'Connection refused'))"""
CONTAINERS = """bench: ABORT - only 7 running containers (need >= 19)"""
PROBE = """  FAIL  sandbox probe: the CLI sends only its verdict tool and the Jaeger tools
        sandbox probe baseline: FAIL - no model request with a JSON body reached the probe within 90s; claude said: Not logged in
bench: ABORT - the sandbox probe failed (see above); the flag was not touched"""
FLAGS = """bench: ABORT - Claude Code CLI 2.0.1 does not accept --effort; update with `claude update`; nothing was touched"""


class Tty(io.StringIO):
    def isatty(self):
        return True


class Mode(unittest.TestCase):
    def mode(self, stream, env, enc="UTF-8"):
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("locale.getpreferredencoding", return_value=enc):
            fc.set_mode(stream)
            return fc.TTY, fc.COLOR, fc.GLYPHS

    def test_tty_pipe_color_and_glyphs(self):
        self.assertEqual(self.mode(Tty(), {"TERM": "xterm"}), (True, True, True))
        self.assertEqual(self.mode(io.StringIO(), {"TERM": "xterm"}), (False, False, False))
        self.assertEqual(self.mode(Tty(), {"TERM": "xterm", "NO_COLOR": "1"}), (True, False, True))
        self.assertEqual(self.mode(Tty(), {"TERM": "xterm", "NO_COLOR": ""}), (True, True, True))  # empty does not count
        self.assertEqual(self.mode(Tty(), {"TERM": "dumb"}), (False, False, False))
        self.assertEqual(self.mode(io.StringIO(), {"FORCE_COLOR": "1"}), (False, True, False))
        self.assertEqual(self.mode(Tty(), {"TERM": "xterm"}, enc="ANSI_X3.4-1968"), (True, True, False))

    def test_lines(self):
        self.mode(io.StringIO(), {})
        self.assertEqual(fc.line("ok", "Images", "23 pulled, 4m02s"), "ok    Images       23 pulled, 4m02s")
        self.mode(Tty(), {"NO_COLOR": "1"})
        self.assertEqual(bench.ANSI.sub("", fc.line("ok", "Images", "x")), "  ✓ Images       x")
        self.mode(Tty(), {"NO_COLOR": "1"}, enc="ascii")
        self.assertEqual(bench.ANSI.sub("", fc.line("fail", "Ready", "x")), "  FAIL Ready        x")

    def test_durations(self):
        self.assertEqual([bench.dur(s) for s in (0, 41, 60, 151, 242.9)], ["0s", "41s", "1m00s", "2m31s", "4m02s"])


class Blockers(unittest.TestCase):
    def setUp(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            fc.set_mode(io.StringIO())

    def test_anything_else_retries(self):
        self.assertEqual(fc.blocker(TOOLS)[:2], ("could not capture tools/list: transport error talking to MCP server: U...", True))
        self.assertEqual(fc.blocker(CONTAINERS)[:2], ("only 7 running containers (need >= 19)", True))
        text, retry, _ = fc.blocker("bench: ABORT - something new and unexpected")
        self.assertEqual((text, retry), ("something new and unexpected", True))

    def test_coloured_dry_run_output(self):  # FORCE_COLOR reaches the child; dry_run() strips it before counting
        with mock.patch.object(fc, "sh", return_value=(1, ["\x1b[32m  ok   \x1b[0m a", "\x1b[1;31m  FAIL \x1b[0m host not thrashing (vmstat bi mean 9.0 <= 1)"])):
            rc, output = fc.dry_run()
        self.assertEqual((fc.checks_passed(output), fc.blocker(output)[1]), (1, "thrash"))

    def test_thrash_is_its_own_kind(self):
        text, retry, report = fc.blocker(THRASH)
        self.assertEqual((text, retry), ("host thrashing, 1.5M blocks/s read (limit 50k)", "thrash"))
        self.assertEqual([bench.ANSI.sub("", l) for l in report], ["FAIL host not thrashing (vmstat bi mean 1525860.5 <= 50000)",
                                  "  pre-flight: vmstat bi mean(non-first)=1525860.5 loadavg=17.83 16.74 14.14"])

    def test_unfixable_by_waiting_stops(self):
        self.assertEqual(fc.blocker(PROBE)[:2], ("claude is not logged in, or its request never arrived", False))
        self.assertEqual(fc.blocker(FLAGS)[:2], ("the Claude Code CLI lacks flags the harness passes", False))

    def test_ready_stops_after_three_thrashing_attempts(self):
        with mock.patch.object(fc, "dry_run", return_value=(1, THRASH)) as dr, mock.patch.object(fc, "READY_SLEEP_S", 0), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(fc.Failed) as e:
                fc.ready({"FIXTURE_DEMO_DIR": "~/demo"})
            fc.STEP.fail("")
        self.assertEqual(dr.call_count, 3)
        self.assertTrue(e.exception.text.startswith("gave up after 3 tries: host thrashing"))
        self.assertEqual(e.exception.tries[0][0], "docker stats --no-stream")


class Pull(unittest.TestCase):
    def test_counts_pulled_images_not_layers(self):
        sample = [" jaeger Pulling", " 9b1a Pulling fs layer", " 9b1a Pull complete", " jaeger Pulled",
                  " Image ghcr.io/open-feature/flagd:v0.12.9 Pulled", " frontend Pulling"]
        self.assertEqual({fc.pulled_image(l, SERVICES) for l in sample} - {None},
                         {SERVICES["jaeger"], SERVICES["flagd"]})


class FailureBlock(unittest.TestCase):
    def test_block_try_and_log(self):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(buf):
            fc.set_mode(sys.stdout)
            fc.fail_block(fc.Step("Ready", "x"), "gave up after 10m00s: host thrashing", ["report line"],
                          [("docker stats --no-stream", "find the container reading the disk")])
        self.assertEqual(buf.getvalue().splitlines(), [
            "FAIL  Ready        gave up after 10m00s: host thrashing", "", "  report line", "",
            "  Try:  docker stats --no-stream     find the container reading the disk",
            "        fixture/FIXTURE.md, Gotchas  known fixture failures and fixes",
            "  Log:  " + bench.shown(fc.LOG)])


class Phoenix(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


class Setup(unittest.TestCase):
    """make setup in pipe mode: the steps run against shims; nothing real is pulled or started."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = pathlib.Path(self.tmp.name)
        (t / "bin").mkdir()
        for name, text in (("docker", DOCKER), ("git", GIT), ("claude", CLAUDE)):
            (t / "bin" / name).write_text(text)
            (t / "bin" / name).chmod(0o755)
        (t / "bench.py").write_text(BENCH_OK)
        (t / "root").mkdir()
        (t / "root" / "fixture").symlink_to(REPO / "fixture")
        (t / "root" / "fixture.env.example").write_text((REPO / "fixture.env.example").read_text())
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Phoenix)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.t = t
        self.env = {"PATH": str(t / "bin") + os.pathsep + os.environ["PATH"], "FIXTURE_SSH_USER": "",
                    "FIXTURE_HOST": "localhost", "FIXTURE_DEMO_DIR": str(t / "demo"),
                    "PHOENIX_PORT": str(self.srv.server_address[1])}

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def setup_run(self, **env):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, dict(self.env, **env)), contextlib.redirect_stdout(buf), \
                mock.patch.multiple(fc, ROOT=str(self.t / "root"), LOG=str(self.t / "root" / ".make.log"),
                                    BENCH=[sys.executable, str(self.t / "bench.py")]):
            rc = fc.main(["setup"])
        return rc, buf.getvalue()

    def test_setup_succeeds(self):
        rc, text = self.setup_run(FAKE_PRESENT=SERVICES["flagd"])
        self.assertEqual(rc, 0, text)
        lines = text.splitlines()
        self.assertEqual(lines[0], "jaeger-mcp-evals setup")
        self.assertRegex(lines[1], r"^ok    Tools        docker 29\.8, compose 5\.6, python 3\.\d+, claude 2\.1\.300, logged in$")
        self.assertEqual(lines[2], "ok    Demo         OpenTelemetry Demo 3.0.0 cloned, overlay installed, 0s")
        self.assertIn("ok    Images       2 pulled, 1 present, 0s", lines)
        self.assertIn("ok    Containers   3 started, flagd recreated, 0s", lines)
        self.assertIn("ok    Ready        pre-flight 3 of 3, 0s", lines)
        self.assertIn("ok    Phoenix      trajectory store up, 0s", lines)
        self.assertEqual(lines[-1], "Next: make smoke    one agent trial, about 50k tokens")
        self.assertTrue((self.t / "demo" / "overlay" / "compose.overlay.yaml").is_file())
        self.assertTrue((self.t / "demo" / "overlay" / "demo.flagd.json.pristine").is_file())
        self.assertNotIn("Pulling", text)  # third-party output stays in the log
        self.assertIn("jaeger Pulled", (self.t / "root" / ".make.log").read_text())
        rc, text = self.setup_run(FAKE_PRESENT=" ".join(SERVICES.values()))  # second run: nothing to do
        self.assertEqual(rc, 0, text)
        self.assertIn("ok    Demo         OpenTelemetry Demo 3.0.0, overlay up to date", text)
        self.assertIn("ok    Images       3 present", text)

    def test_compose_up_failure_shows_log_tail(self):
        rc, text = self.setup_run(FAKE_UP_RC="1")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL  Containers   docker compose up failed (exit 1)", text)
        self.assertIn("    Error response from daemon: port is already allocated", text)
        self.assertIn("  Log:  ", text)
        self.assertNotIn("Ready", text)


if __name__ == "__main__":
    unittest.main()
