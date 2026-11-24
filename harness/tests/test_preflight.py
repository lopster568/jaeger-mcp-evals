#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""fixture/preflight.sh: busy fixture ports fail unless the stack is already ours. docker and claude are PATH shims (claude answers auth status)."""
import os
import pathlib
import shutil
import socket
import stat
import subprocess
import tempfile
import unittest

SCRIPT = str(pathlib.Path(__file__).resolve().parents[2] / "fixture" / "preflight.sh")
DOCKER = """#!/bin/bash
[ "$1 $2" = "compose version" ] && { echo "Docker Compose version ${FAKE_COMPOSE:-v2.29.1}"; exit 0; }
[ "$1" = ps ] && [ -n "$FAKE_JAEGER" ] && echo abc123
[ "$1" = info ] && [ -n "$FAKE_INFO" ] && { echo "$FAKE_INFO" >&2; echo "stdout is not the cause"; exit 1; }
exit 0
"""
CLAUDE = """#!/bin/bash
[ "$1 $2" = "auth status" ] && exit %d
exit 0
"""


class Preflight(unittest.TestCase):
    def run_script(self, port, jaeger, logged_in=True, host_only="", remote="", grader="", bind="", compose="", client="", no_docker=False, info="", home=None, no_claude=False, wrap=None):
        """wrap: True or False puts a claude-wrap shim, logged in or not, on PATH and names it in GRADER_BIN."""
        with tempfile.TemporaryDirectory() as d:
            if wrap is not None:
                pathlib.Path(d, "claude-wrap").write_text(CLAUDE % int(not wrap))
                os.chmod(os.path.join(d, "claude-wrap"), 0o755)
            for name in ("docker", "claude"):
                if (name == "docker" and no_docker) or (name == "claude" and no_claude):
                    continue
                shim = os.path.join(d, name)
                body = ("#!/bin/bash\n[ \"$1\" = info ] && { [ -n \"$FAKE_INFO\" ] && exit 1; exit 0; }\nexit 1\n" if remote else DOCKER) if name == "docker" else CLAUDE % int(not logged_in)
                pathlib.Path(shim).write_text(body)
                os.chmod(shim, os.stat(shim).st_mode | stat.S_IEXEC)
            overlay = os.path.join(d, "overlay.yaml")
            pathlib.Path(overlay).write_text('    ports:\n      - "%s%d:16686"\n' % (bind, port))
            path = d + os.pathsep + os.environ["PATH"]
            if no_docker or no_claude:  # a PATH of only what the script needs, so a docker or claude installed here is not found
                tools = os.path.join(d, "tools")
                os.mkdir(tools)
                for t in ("bash", "env", "dirname", "git", "curl", "python3", "grep", "sed", "head"):
                    os.symlink(shutil.which(t), os.path.join(tools, t))
                path = d + os.pathsep + tools
            env = dict(os.environ, PATH=path, OVERLAY=overlay, FAKE_JAEGER="1" if jaeger else "", FIXTURE_HOST_ONLY=host_only,
                       FIXTURE_SSH_USER=remote, GRADER_CLIENT=grader, CLIENT=client, FAKE_COMPOSE=compose, FAKE_INFO=info,
                       GRADER_BIN="" if wrap is None else "claude-wrap")
            if home:
                env["HOME"] = home
            return subprocess.run(["bash", SCRIPT], env=env, capture_output=True, text=True)

    def test_busy_port_fails_unless_stack_is_ours(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            port = s.getsockname()[1]
            for bind in ("", "127.0.0.1:", "${FIXTURE_BIND:-127.0.0.1}:"):  # the overlay form, plain and fixed binds parse
                r = self.run_script(port, jaeger=False, bind=bind)
                self.assertEqual(r.returncode, 1, bind)
                self.assertIn("port %d is in use" % port, r.stdout)
                self.assertIn("lsof -iTCP:%d -sTCP:LISTEN" % port, r.stdout)  # macOS has no ss
            ok = self.run_script(port, jaeger=True)
            self.assertEqual(ok.returncode, 0)
            self.assertEqual(ok.stdout, "")  # silent on success; make prints the Tools line
        self.assertEqual(self.run_script(port, jaeger=False).returncode, 0)  # free again

    def test_host_only_skips_the_claude_checks(self):
        self.assertEqual(self.run_script(1, jaeger=False, logged_in=False, host_only="1").returncode, 0)
        self.assertEqual(self.run_script(1, jaeger=False, logged_in=False, host_only="0", grader="claude").returncode, 1)

    def test_remote_fixture_skips_docker_and_ports_on_the_agent_machine(self):
        # FIXTURE_SSH_USER set: Docker and the demo's ports live on the fixture host; here docker always fails
        self.assertEqual(self.run_script(1, jaeger=False, remote="u").returncode, 0)  # docker is on PATH; compose is not asked
        self.assertEqual(self.run_script(1, jaeger=False).returncode, 0)  # local: the working docker shim passes

    def test_remote_agent_machine_needs_docker_unless_everything_is_claude(self):
        # Goose and the Goose grader run in Docker on the agent machine, whatever the fixture host is
        r = self.run_script(1, jaeger=False, remote="u", no_docker=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("docker is not installed", r.stdout)
        self.assertEqual(self.run_script(1, jaeger=False, remote="u", no_docker=True, client="goose", grader="claude").returncode, 1)
        self.assertEqual(self.run_script(1, jaeger=False, remote="u", no_docker=True, client="codex", grader="claude").returncode, 0)
        self.assertEqual(self.run_script(1, jaeger=False, remote="u", no_docker=True, client="codex", grader="goose").returncode, 1)
        # GRADER_CLIENT empty: the grader follows the agent (claude grades claude; codex has no grader, goose does)
        self.assertEqual(self.run_script(1, jaeger=False, remote="u", no_docker=True, client="claude").returncode, 0)
        self.assertEqual(self.run_script(1, jaeger=False, remote="u", no_docker=True, client="cli").returncode, 0)
        self.assertEqual(self.run_script(1, jaeger=False, remote="u", no_docker=True, client="codex").returncode, 1)
        self.assertEqual(self.run_script(1, jaeger=False, remote="u", no_docker=True, host_only="1").returncode, 0)  # no agent here

    def test_compose_must_be_2_24_or_newer_locally(self):
        for version, ok in (("v2.23.3", False), ("v2.24.0", True), ("v2.29.1-desktop.1", True), ("2.9.0", False), ("v5.0.0", True)):
            r = self.run_script(1, jaeger=False, compose=version)
            self.assertEqual(r.returncode, 0 if ok else 1, version)
            if not ok:
                self.assertIn("2.24", r.stdout)
        self.assertEqual(self.run_script(1, jaeger=False, remote="u", compose="v2.1.0").returncode, 0)  # the fixture host's own check

    def test_logged_out_claude_fails_only_for_the_claude_grader(self):
        r = self.run_script(1, jaeger=False, logged_in=False, grader="cli")  # the old name still means claude
        self.assertEqual(r.returncode, 1)
        self.assertIn("claude is not logged in", r.stdout)
        r = self.run_script(1, jaeger=False, logged_in=False, grader="goose")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")  # the Goose defaults need no claude: nothing to say
        r = self.run_script(1, jaeger=False, logged_in=False, client="claude", grader="goose")
        self.assertEqual(r.returncode, 0)
        self.assertIn("needed for CLIENT=claude", r.stdout)
        # GRADER_CLIENT empty: a claude agent is graded by claude, so a logged-out claude fails
        r = self.run_script(1, jaeger=False, logged_in=False, client="claude")
        self.assertEqual(r.returncode, 1)
        self.assertIn("claude is not logged in", r.stdout)
        self.assertEqual(self.run_script(1, jaeger=False, logged_in=False, client="codex").returncode, 0)  # codex: goose grades

    def test_the_claude_grader_is_checked_on_grader_bin(self):
        # GRADER_BIN logged in, plain claude logged out: the grader passes; a goose agent needs no plain claude
        self.assertEqual(self.run_script(1, jaeger=False, logged_in=False, grader="claude", wrap=True).returncode, 0)
        r = self.run_script(1, jaeger=False, logged_in=True, grader="claude", wrap=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("claude-wrap is not logged in; run claude-wrap", r.stdout)
        # a claude agent still runs plain claude: logged out, a note, while the grader's own command passes
        r = self.run_script(1, jaeger=False, logged_in=False, client="claude", wrap=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("needed for CLIENT=claude", r.stdout)
        r = self.run_script(1, jaeger=False, grader="claude", no_claude=True, wrap=True)  # no plain claude at all
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_docker_that_will_not_answer_names_the_cause(self):
        r = self.run_script(1, jaeger=False, info="permission denied while trying to connect to the Docker daemon socket")
        self.assertEqual(r.returncode, 1)
        self.assertIn("cannot use it (permission denied)", r.stdout)
        self.assertIn("usermod -aG docker", r.stdout)
        r = self.run_script(1, jaeger=False, info="Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not running; start Docker", r.stdout)
        # a remote fixture with a claude agent and grader needs no docker here, so a broken one is not asked
        self.assertEqual(self.run_script(1, jaeger=False, remote="u", client="claude", grader="claude", info="x").returncode, 0)

    def test_claude_installed_off_path_says_so(self):
        with tempfile.TemporaryDirectory() as home:
            r = self.run_script(1, jaeger=False, grader="claude", no_claude=True, home=home)
            self.assertIn("is not installed", r.stdout)
            os.makedirs(os.path.join(home, ".local", "bin"))
            pathlib.Path(home, ".local", "bin", "claude").write_text("#!/bin/bash\n")
            os.chmod(os.path.join(home, ".local", "bin", "claude"), 0o755)
            r = self.run_script(1, jaeger=False, grader="claude", no_claude=True, home=home)
            self.assertEqual(r.returncode, 1)
            self.assertIn("not on your PATH", r.stdout)


if __name__ == "__main__":
    unittest.main()
