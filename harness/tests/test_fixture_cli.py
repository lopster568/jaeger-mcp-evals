#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""harness/fixture_cli.py: screen mode, blocker classification, pull counting, the failure block, and
`setup` end to end in pipe mode against docker, git and claude shims on PATH. No real docker."""
import contextlib
import http.server
import io
import json
import os
import pathlib
import subprocess
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
elif "pull" in a and os.environ.get("FAKE_PULL_FAILS") and not os.path.exists(os.environ["FAKE_PULL_FAILS"] + "x"):
    f = os.environ["FAKE_PULL_FAILS"]; n = int(open(f).read()) if os.path.exists(f) else 0; open(f, "w").write(str(n + 1))
    if n >= 2: open(f + "x", "w").close()
    names = a[a.index("pull") + 1:]
    if os.environ.get("FAKE_PULL_PROGRESS"): print(" %%s Pulled" %% names[n %% len(names)])
    print("Error: dial tcp: i/o timeout"); sys.exit(1)
elif "pull" in a:
    for n in a[a.index("pull") + 1:]: print(" %%s Pulling" %% n); print(" abc123 Pull complete"); print(" %%s Pulled" %% n)
elif "up" in a and "phoenix" not in a:
    print(" Container flagd  Recreated"); print(" Container jaeger  Started")
    if os.environ.get("FAKE_UP_RC", "0") != "0": print("Error response from daemon: port is already allocated")
    sys.exit(int(os.environ.get("FAKE_UP_RC", "0")))
elif a[:1] == ["update"]: open(os.environ["FAKE_UPDATES"], "a").write(" ".join(a) + "\n")
elif "ps" in a and "phoenix" in a:
    if os.environ.get("FAKE_PHOENIX"): print("ppp")
elif "ps" in a: print("aaa\nbbb\nccc")
""" % repr(SERVICES)
GIT = """#!/bin/bash
[ "$1" = clone ] && { d="${@: -1}"; mkdir -p "$d/src/flagd"; echo '{"flags": {}}' > "$d/src/flagd/demo.flagd.json"; exit 0; }
[ "$1 $2" = "rev-parse HEAD" ] && echo 1755859a0000000000000000000000000000000
"""
CLAUDE = """#!/bin/bash
echo "2.1.300 (Claude Code)"
"""
CODEX = """#!/bin/bash
echo "codex-cli 0.159.0"
"""

# Real bench.py run --dry-run output tails (Log.check and die() formats), screen lines on a pipe.
TOOLS = """Pre-flight
  ok    jaeger image quay.io/jaegertracing/jaeger:2.20.0 sha256:9ae01775e2b2
        arms here: baseline
        == pre-flight: capture MCP tools/list (http://localhost:16686/jaeger/ui/api/ai/mcp/) ==
bench: ABORT - could not capture tools/list: transport error talking to MCP server: URLError(ConnectionRefusedError(111, 'Connection refused'))"""
CONTAINERS = """  FAIL  only 7 containers running (need >= 19)
        fix: in ~/demo run `docker compose ps -a` to see which container is not up, then run again
bench: ABORT - stopped before any trial; the fault flag was not touched"""
PROBE = """  FAIL  agent can call only the Jaeger tools and its verdict tool
        sandbox probe baseline: FAIL - no model request with a JSON body reached the probe within 90s; claude said: Not logged in
bench: ABORT - the sandbox probe failed (see above); the flag was not touched"""
FLAGS = """bench: ABORT - Claude Code CLI 2.0.1 does not accept --effort; nothing was touched"""


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

    def test_first_failure_is_shortened(self):
        self.assertEqual(fc.blocker(TOOLS)[0], "could not capture tools/list: transport error talking to MCP server: U...")
        self.assertEqual(fc.blocker(CONTAINERS)[0], "only 7 containers running (need >= 19)")
        self.assertEqual(fc.blocker("bench: ABORT - something new and unexpected")[0], "something new and unexpected")

    def test_coloured_dry_run_output(self):  # FORCE_COLOR reaches the child; dry_run() strips it before counting
        with mock.patch.object(fc, "sh", return_value=(1, ["\x1b[32m  ok   \x1b[0m a", "\x1b[1;31m  FAIL \x1b[0m only 7 containers running (need >= 19)"])):
            rc, output = fc.dry_run(fc.SMOKE_RUN)
        self.assertEqual((fc.checks_passed(output), fc.blocker(output)[0]), (1, "only 7 containers running (need >= 19)"))

    def test_claude_rows_for_the_cli_path(self):
        self.assertEqual(fc.blocker(PROBE)[0], "claude is not logged in, or its request never arrived")
        self.assertEqual(fc.blocker(FLAGS)[0], "the Claude Code CLI lacks flags the harness passes")

    def test_a_goose_preflight_failure_gets_its_fix_line(self):
        cfg = {"FIXTURE_DEMO_DIR": "~/demo"}
        for bench_line, short, fix in (
                ("bench: the pinned Goose image is not on this machine; nothing was touched", "the pinned Goose image is not on this machine", "docker pull " + fc.goose_client.IMAGE),
                ("bench: docker is not on PATH; the goose client and the goose grader run Goose in a container", "docker is not installed, and the Goose client runs in a container", "install Docker Engine or Docker Desktop"),
                ("bench: run.provider is openrouter but OPENROUTER_API_KEY is empty in fixture.env; nothing was touched", "the provider's API key is empty in fixture.env", "set OPENROUTER_API_KEY or ANTHROPIC_API_KEY in fixture.env"),
                ("bench: the grader model is empty; nothing was touched", "the grader model is empty", "set GRADER_MODEL or GOOSE_MODEL in fixture.env"),
                ("bench: GRADER_CLIENT is 'x', not goose or claude; nothing was touched", "GRADER_CLIENT is not goose or claude", "set GRADER_CLIENT in fixture.env")):
            text, report = fc.blocker(bench_line)
            self.assertEqual(text, short)
            self.assertEqual(fc.preflight_failed(cfg, text, report, fc.SMOKE_RUN).tries[0][0], fix)


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
            fc.fail_block(fc.Step("Ready", "x"), "gave up after 10m00s: Jaeger not answering", ["report line"],
                          [("docker compose ps -a", "which container is not up")])
        self.assertEqual(buf.getvalue().splitlines(), [
            "FAIL  Ready        gave up after 10m00s: Jaeger not answering", "", "  report line", "",
            "  Try:  docker compose ps -a         which container is not up",
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
        for name, text in (("docker", DOCKER), ("git", GIT), ("claude", CLAUDE), ("codex", CODEX)):
            (t / "bin" / name).write_text(text)
            (t / "bin" / name).chmod(0o755)
        (t / "root").mkdir()
        (t / "root" / "fixture").symlink_to(REPO / "fixture")
        (t / "root" / "harness" / "experiments").mkdir(parents=True)  # ready() reads the smoke scenario's service
        (t / "root" / "harness" / "scenarios").symlink_to(REPO / "harness" / "scenarios")
        (t / "root" / "harness" / "experiments" / "smoke-paymentfailure.json").write_text((REPO / fc.SMOKE).read_text())
        (t / "root" / "fixture.env.example").write_text((REPO / "fixture.env.example").read_text())
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Phoenix)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.traces = mock.patch.object(bench, "traces", return_value=[{}] * 3).start()  # the baseline traffic Ready waits for
        self.addCleanup(mock.patch.stopall)
        self.t = t
        self.env = {"PATH": str(t / "bin") + os.pathsep + os.environ["PATH"], "FIXTURE_SSH_USER": "",
                    "FIXTURE_HOST": "localhost", "FIXTURE_DEMO_DIR": str(t / "demo"),
                    "PHOENIX_PORT": str(self.srv.server_address[1]), "JAEGER_UI_PORT": str(self.srv.server_address[1]),
                    "JAEGER_BASE_PATH": "", "MIN_CONTAINERS": "3", "GOOSE_PROVIDER": "openrouter", "GOOSE_MODEL": "vendor/model:free",
                    "OPENROUTER_API_KEY": "k", "CLIENT": "goose", "GRADER_CLIENT": "goose", "CODEX_BIN": "codex"}

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def setup_run(self, cmd="setup", *args, **env):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, dict(self.env, **env)), contextlib.redirect_stdout(buf), \
                mock.patch.multiple(fc, ROOT=str(self.t / "root"), LOG=str(self.t / "root" / ".make.log")):
            rc = fc.main([cmd, *args])
        return rc, buf.getvalue()

    def test_setup_succeeds(self):
        rc, text = self.setup_run(FAKE_PRESENT=SERVICES["flagd"])
        self.assertEqual(rc, 0, text)
        lines = text.splitlines()
        self.assertEqual(lines[0], "jaeger-ai-evals setup")
        self.assertRegex(lines[1], r"^ok    Tools        docker 29\.8, compose 5\.6, python 3\.\d+, goose openrouter vendor/model:free, OPENROUTER_API_KEY set$")  # claude is shimmed and installed: not shown for a goose agent and grader
        self.assertEqual(lines[2], "ok    Demo         OpenTelemetry Demo 3.0.0 cloned, overlay installed, 0s")
        self.assertIn("ok    Images       2 pulled, 1 present, 0s", lines)
        self.assertIn("ok    Containers   3 started, flagd recreated, 0s", lines)
        self.assertIn("ok    Goose image  pulled, 0s", lines)
        self.assertIn("ok    Ready        3 containers running, Jaeger answers, payment sending traces, 0s", lines)
        self.assertIn("ok    Phoenix      trajectory store up, 0s", lines)
        self.assertEqual(lines[-1], "Next: make smoke    one agent trial, cost depends on your provider and model")
        self.assertTrue((self.t / "demo" / "overlay" / "compose.overlay.yaml").is_file())
        self.assertTrue((self.t / "demo" / "overlay" / "demo.flagd.json.pristine").is_file())
        self.assertNotIn("Pulling", text)  # third-party output stays in the log
        self.assertIn("jaeger Pulled", (self.t / "root" / ".make.log").read_text())
        rc, text = self.setup_run(FAKE_PRESENT=" ".join(SERVICES.values()))  # second run: nothing to do
        self.assertEqual(rc, 0, text)
        self.assertIn("ok    Demo         OpenTelemetry Demo 3.0.0, overlay up to date", text)
        self.assertIn("ok    Images       3 present", text)
        self.assertIn("ok    Goose image  pulled", text)  # the shim has no goose image present either time

    def test_goose_image_present_is_not_pulled(self):
        rc, text = self.setup_run(FAKE_PRESENT=fc.goose_client.IMAGE)
        self.assertEqual(rc, 0, text)
        self.assertIn("ok    Goose image  present", text)

    def test_host_only_needs_no_goose_image(self):
        rc, text = self.setup_run(FIXTURE_HOST_ONLY="1")
        self.assertEqual(rc, 0, text)
        self.assertNotIn("Goose image", text)

    def test_claude_smoke_and_grader_need_no_goose_image(self):
        rc, text = self.setup_run(CLIENT="claude", GRADER_CLIENT="claude")
        self.assertEqual(rc, 0, text)
        self.assertNotIn("Goose image", text)
        self.assertIn("claude 2.1.300", text)
        self.assertTrue(text.splitlines()[-1].endswith("one agent trial, about 50k tokens"), text)

    def test_the_tools_line_shows_the_claude_grader_command(self):
        (self.t / "bin" / "claude-wrap").write_text(CLAUDE)
        (self.t / "bin" / "claude-wrap").chmod(0o755)
        rc, text = self.setup_run(CLIENT="goose", GRADER_CLIENT="claude", GRADER_BIN="claude-wrap")
        self.assertEqual(rc, 0, text)
        self.assertRegex(text.splitlines()[1], r", OPENROUTER_API_KEY set, claude-wrap 2\.1\.300$")  # the grader's command, not plain claude
        rc, text = self.setup_run(CLIENT="claude", GRADER_CLIENT="claude", GRADER_BIN="claude-wrap")
        self.assertRegex(text.splitlines()[1], r", claude 2\.1\.300, claude-wrap 2\.1\.300$")  # the agent runs plain claude
        rc, text = self.setup_run(CLIENT="claude", GRADER_CLIENT="claude", GRADER_BIN="")
        self.assertEqual(text.splitlines()[1].count("claude 2.1.300"), 1)  # one command, shown once

    def test_the_grader_follows_the_agent_when_grader_client_is_empty(self):
        rc, text = self.setup_run(CLIENT="claude", GRADER_CLIENT="")  # a claude agent grades on claude: no Goose image
        self.assertEqual(rc, 0, text)
        self.assertNotIn("Goose image", text)
        rc, text = self.setup_run(CLIENT="codex", GRADER_CLIENT="")  # codex has no grader client: goose grades
        self.assertEqual(rc, 0, text)
        self.assertIn("Goose image", text)

    def test_cli_is_read_as_claude_for_the_tools_line_and_the_goose_image(self):
        rc, text = self.setup_run(CLIENT="cli", GRADER_CLIENT="cli")
        self.assertEqual(rc, 0, text)
        self.assertNotIn("Goose image", text)
        self.assertIn("claude 2.1.300", text)

    def test_a_codex_smoke_shows_codex_and_not_claude(self):
        rc, text = self.setup_run(CLIENT="codex", GRADER_CLIENT="goose")
        self.assertEqual(rc, 0, text)
        tools = text.splitlines()[1]
        self.assertIn("codex 0.159.0", tools)
        self.assertNotIn("claude", tools)
        self.assertTrue(text.splitlines()[-1].endswith("one agent trial, counts against your Codex login"), text)

    def test_goose_contributor_without_claude_or_settings_still_reaches_ready(self):
        with mock.patch("shutil.which", return_value=None):  # no claude anywhere on PATH
            rc, text = self.setup_run(GOOSE_PROVIDER="", GOOSE_MODEL="")
        self.assertEqual(rc, 0, text)
        self.assertRegex(text, r"ok    Tools        .*goose settings incomplete\n")
        self.assertNotIn("claude", text.split("Demo")[0])
        self.assertNotIn("logged in", text)
        self.assertIn("ok    Ready        3 containers running, Jaeger answers", text)
        self.assertEqual(text.splitlines()[-1], "Before it: set OPENROUTER_API_KEY in fixture.env (another provider: GOOSE_PROVIDER, GOOSE_MODEL and its key)")

    def test_pull_is_retried_then_succeeds(self):
        with mock.patch.object(fc, "PULL_BACKOFF_S", 0):
            rc, text = self.setup_run(FAKE_PULL_FAILS=str(self.t / "n"))
        self.assertEqual(rc, 0, text)
        self.assertEqual(text.count("..    Images       pull failed at 0 of 3, retrying in 0s"), 3, text)
        self.assertIn("ok    Images       3 pulled", text)

    def test_pull_keeps_retrying_while_each_attempt_completes_an_image(self):
        # three failed attempts, each completing one more image: with PULL_TRIES 1 the old rule gave up after one
        with mock.patch.object(fc, "PULL_BACKOFF_S", 0), mock.patch.object(fc, "PULL_TRIES", 1):
            rc, text = self.setup_run(FAKE_PULL_FAILS=str(self.t / "n"), FAKE_PULL_PROGRESS="1")
        self.assertEqual(rc, 0, text)
        self.assertIn("..    Images       pull failed at 3 of 3, retrying in 0s", text)
        self.assertIn("ok    Images       3 pulled", text)

    def test_pull_gives_up_after_the_last_try(self):
        (self.t / "n").write_text("-99")  # never reaches the success count
        with mock.patch.object(fc, "PULL_BACKOFF_S", 0), mock.patch.object(fc, "PULL_TRIES", 2):
            rc, text = self.setup_run(FAKE_PULL_FAILS=str(self.t / "n"))
        self.assertEqual(rc, 1)
        self.assertIn("FAIL  Images       docker compose pull failed", text)

    def test_host_only_setup_shows_no_agent_tools(self):
        rc, text = self.setup_run(FIXTURE_HOST_ONLY="1")
        self.assertEqual(rc, 0, text)
        self.assertRegex(text, r"ok    Tools        docker 29\.8, compose 5\.6, python 3\.\d+\n")

    def test_tools_line_without_docker_prints_no_junk_version(self):
        # on WSL without Docker the shim prints "The command 'docker' could not be found..." and first_token returns "The"
        for ssh, docker, want in (("me", "The", "ok    Tools        local docker not running (remote fixture), python "),
                                  ("me", "29.8.1", "ok    Tools        docker 29.8, compose ?, python "),
                                  ("", "The", "ok    Tools        docker ?, compose ?, python ")):
            buf = io.StringIO()
            with mock.patch.object(fc, "first_token", side_effect=[docker, "The", "2.1.300"]), contextlib.redirect_stdout(buf):
                fc.tools({"FIXTURE_SSH_USER": ssh})
            self.assertTrue(buf.getvalue().startswith(want), buf.getvalue())
            self.assertNotIn("The", buf.getvalue())

    def test_compose_up_failure_shows_log_tail(self):
        rc, text = self.setup_run(FAKE_UP_RC="1")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL  Containers   docker compose up failed (exit 1)", text)
        self.assertIn("    Error response from daemon: port is already allocated", text)
        self.assertIn("  Log:  ", text)
        self.assertNotIn("Ready", text)
        self.assertNotIn('"services"', text)  # the compose config JSON is data for the caller, not the error
        self.assertNotIn('"services"', (self.t / "root" / ".make.log").read_text())

    def test_ready_waits_for_the_baseline_traces(self):
        self.traces.side_effect = [[], [], [{}] * 3]
        with mock.patch.object(fc, "READY_SLEEP_S", 0):
            rc, text = self.setup_run(FAKE_PRESENT=SERVICES["flagd"])
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.traces.call_count, 3)
        self.assertIn("Ready        3 containers running, Jaeger answers, payment sending traces", text)

    def test_up_starts_phoenix_only_when_its_container_exists(self):
        self.setup_run(FAKE_PRESENT=" ".join(SERVICES.values()))  # clones the demo
        rc, text = self.setup_run("up", FAKE_PRESENT=" ".join(SERVICES.values()))
        self.assertEqual(rc, 0, text)
        self.assertNotIn("Phoenix", text)
        rc, text = self.setup_run("up", FAKE_PRESENT=" ".join(SERVICES.values()), FAKE_PHOENIX="1")
        self.assertEqual(rc, 0, text)
        self.assertIn("ok    Phoenix      trajectory store up", text)


    def test_phoenix_gets_a_restart_policy_without_touching_the_overlay(self):
        self.setup_run("fixture")  # clones the demo
        updates = self.t / "updates"
        rc, text = self.setup_run("phoenix", FAKE_PHOENIX="1", FAKE_UPDATES=str(updates))
        self.assertEqual(rc, 0, text)
        self.assertEqual(updates.read_text(), "update --restart unless-stopped ppp\n")

    def test_down_says_what_survives(self):
        self.setup_run("fixture")
        rc, text = self.setup_run("down", FAKE_UPDATES=str(self.t / "u"))
        self.assertEqual(rc, 0, text)
        self.assertIn("3 stopped; Jaeger's traces are lost (in-memory store), Phoenix keeps its own", text)

    def test_smoke_dry_run_spends_nothing(self):
        with mock.patch.object(fc, "dry_run", return_value=(0, ["ok"])), mock.patch.object(fc, "checks_passed", return_value=1), \
                mock.patch.object(fc.os, "execv") as ex, mock.patch.object(fc.time, "sleep") as nap:
            rc, text = self.setup_run("smoke", "--dry-run")
        self.assertEqual(rc, 0, text)
        self.assertIn("dry run only; nothing spent", text)
        ex.assert_not_called()  # no bench.py run
        nap.assert_not_called()  # no 5 s window either
        self.assertEqual(fc.main(["smoke", "--bogus"]), 2)


MAKE = ["make", "-n", "--no-print-directory", "-C", str(REPO)]


def make_n(*args):
    r = subprocess.run(MAKE + list(args), capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


class Make(unittest.TestCase):
    """The Makefile at make -n time: no recipe runs."""

    def test_lower_case_make_variable_stops(self):
        for args, want in ((["smoke", "client=claude"], "use CLIENT=claude"), (["measure", "n=3"], "use N=3"),
                           (["run", "experiment=x"], "use EXPERIMENT=x")):
            rc, out, err = make_n(*args)
            self.assertEqual(rc, 2, args)
            self.assertIn(want, err)
            self.assertEqual(out, "")

    def test_make_passes_every_option(self):
        rc, out, err = make_n("measure", "JAEGER=/x", "SCENARIO=y", "CLIENT=claude", "N=3", "BASE=main", "DRY=1", "YES=1")
        self.assertEqual(rc, 0, err)
        self.assertIn('CLIENT=claude bash harness/measure.sh "/x" "y" --n 3 --base main --dry-run --yes', out)
        out = make_n("measure", "JAEGER=/x", "SCENARIO=y", "DRY=0", "YES=0")[1]  # 0 is not a switch
        self.assertRegex(out, r'measure\.sh "/x" "y"\s*\n')
        self.assertNotIn("--dry-run", out)
        self.assertNotIn("--yes", out)
        out = make_n("smoke", "CLIENT=codex", "DRY=1")[1]
        self.assertIn("CLIENT=codex", out)
        self.assertIn("fixture_cli.py smoke --dry-run", out)
        self.assertNotIn("--dry-run", make_n("smoke", "DRY=0")[1])
        self.assertIn("harness/experiments/records.json", make_n("run", "EXPERIMENT=records")[1])  # a name, not the directory
        out = make_n("run", "EXPERIMENT=foo", "DRY=1", "FIRST=1")[1]
        self.assertIn("bench.py run harness/experiments/foo.json --dry-run --first-only", out)
        out = make_n("run", "EXPERIMENT=records/inputs/skill-callee-down-cli55.json")[1]  # a path is passed as given
        self.assertIn("bench.py run records/inputs/skill-callee-down-cli55.json", out)
        r = subprocess.run(["make", "-C", str(REPO), "run"], capture_output=True, text=True)  # only the usage test runs
        self.assertEqual((r.returncode, "usage: make run EXPERIMENT=" in r.stdout), (2, True))


class SmokeFile(unittest.TestCase):
    """smoke_file: which experiment the smoke runs, and the stop at once on missing Goose settings."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        exp = pathlib.Path(self.tmp.name, "harness", "experiments")
        exp.mkdir(parents=True)
        (exp / "smoke-paymentfailure.json").write_text((REPO / fc.SMOKE).read_text())
        p = mock.patch.object(fc, "ROOT", self.tmp.name)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        with mock.patch.dict(os.environ, {}, clear=True):
            fc.set_mode(io.StringIO())

    def filled(self, cfg, **run):
        """smoke_file(cfg) writes SMOKE_RUN: the template with run's three keys added, and a file bench.py loads."""
        path = fc.smoke_file(cfg)
        self.assertEqual(path, fc.SMOKE_RUN)
        e = json.loads(pathlib.Path(self.tmp.name, path).read_text())
        base = json.loads((REPO / fc.SMOKE).read_text())
        self.assertFalse({"client", "provider", "model"} & set(base["run"]))
        want = dict(base["run"], **run)
        if run["client"] != "claude":
            del want["max_budget_usd"]  # only claude enforces it
        if run["client"] == "goose":
            want["effort"] = None  # no GOOSE_THINKING_EFFORT, as the Jaeger demo
        self.assertEqual(e["run"], want)
        self.assertEqual({k: v for k, v in e.items() if k != "run"}, {k: v for k, v in base.items() if k != "run"})
        fc.bench.load_experiment(os.path.join(self.tmp.name, path))

    def test_goose_default_fills_in_goose(self):
        self.filled({"CLIENT": "goose", "GOOSE_PROVIDER": "openrouter", "GOOSE_MODEL": "m/x:free"}, client="goose", provider="openrouter", model="m/x:free")

    def test_claude_option_fills_in_claude(self):
        self.filled({"CLIENT": "claude", "CLAUDE_MODEL": "claude-x", "GOOSE_PROVIDER": "openrouter", "GOOSE_MODEL": "m"},
                    client="claude", provider=None, model="claude-x")
        self.filled({"CLIENT": "cli", "CLAUDE_MODEL": "claude-x", "GOOSE_PROVIDER": "openrouter", "GOOSE_MODEL": "m"},  # cli is claude
                    client="claude", provider=None, model="claude-x")

    def test_codex_option_fills_in_codex(self):
        self.filled({"CLIENT": "codex", "CODEX_MODEL": "gpt-x", "GOOSE_PROVIDER": "openrouter", "GOOSE_MODEL": "m"},
                    client="codex", provider=None, model="gpt-x")

    def test_an_empty_model_key_stops_at_once(self):
        for client in ("claude", "codex"):
            with self.assertRaises(fc.Failed) as e:
                fc.smoke_file({"CLIENT": client, client.upper() + "_MODEL": ""})
            self.assertEqual(e.exception.text, "%s_MODEL is empty" % client.upper())

    def test_unknown_client_names_all_three(self):
        for raw in ("bogus", ""):
            with self.assertRaises(fc.Failed) as e:
                fc.smoke_file({"CLIENT": raw})
            self.assertEqual(e.exception.text, "CLIENT is %r, not goose, claude or codex" % raw)

    def test_bench_refuses_the_template(self):
        with self.assertRaisesRegex(ValueError, "is the make smoke template .*; run make smoke, which fills them in"):
            fc.bench.load_experiment(str(REPO / fc.SMOKE))

    def test_empty_goose_settings_stop_at_once(self):
        for cfg, text in (({}, "GOOSE_PROVIDER is ''; it must be anthropic or openrouter"),
                          ({"GOOSE_PROVIDER": "openrouter"}, "GOOSE_MODEL is empty in fixture.env"),
                          ({"GOOSE_MODEL": "m"}, "GOOSE_PROVIDER is ''; it must be anthropic or openrouter"),
                          ({"GOOSE_PROVIDER": "openai", "GOOSE_MODEL": "m"}, "GOOSE_PROVIDER is 'openai'; it must be anthropic or openrouter")):
            with self.assertRaises(fc.Failed) as e:
                fc.smoke_file(dict(cfg, CLIENT="goose"))
            self.assertEqual(e.exception.text, text)
            self.assertTrue(any("OPENROUTER_API_KEY" in l and "scripts/ai-sidecar" not in l for l in e.exception.block))
            self.assertTrue(any("scripts/ai-sidecar/goose" in l for l in e.exception.block))

    def test_smoke_stops_before_any_dry_run(self):
        buf = io.StringIO()
        with mock.patch.object(fc, "dry_run") as dr, contextlib.redirect_stdout(buf), self.assertRaises(fc.Failed):
            fc.smoke({})
        fc.STEP.fail("")
        dr.assert_not_called()


if __name__ == "__main__":
    unittest.main()
