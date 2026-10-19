#!/usr/bin/env python3
"""fixture/preflight.sh: busy fixture ports fail unless the stack is already ours. docker and claude are PATH shims (claude answers auth status)."""
import os
import pathlib
import socket
import stat
import subprocess
import tempfile
import unittest

SCRIPT = str(pathlib.Path(__file__).resolve().parents[2] / "fixture" / "preflight.sh")
DOCKER = """#!/bin/bash
[ "$1 $2" = "compose version" ] && { echo "Docker Compose version v2.29.1"; exit 0; }
[ "$1" = ps ] && [ -n "$FAKE_JAEGER" ] && echo abc123
exit 0
"""
CLAUDE = """#!/bin/bash
[ "$1 $2" = "auth status" ] && exit %d
exit 0
"""


class Preflight(unittest.TestCase):
    def run_script(self, port, jaeger, logged_in=True):
        with tempfile.TemporaryDirectory() as d:
            for name in ("docker", "claude"):
                shim = os.path.join(d, name)
                pathlib.Path(shim).write_text(DOCKER if name == "docker" else CLAUDE % int(not logged_in))
                os.chmod(shim, os.stat(shim).st_mode | stat.S_IEXEC)
            overlay = os.path.join(d, "overlay.yaml")
            pathlib.Path(overlay).write_text('    ports:\n      - "%d:16686"\n' % port)
            env = dict(os.environ, PATH=d + os.pathsep + os.environ["PATH"], OVERLAY=overlay, FAKE_JAEGER="1" if jaeger else "")
            return subprocess.run(["bash", SCRIPT], env=env, capture_output=True, text=True)

    def test_busy_port_fails_unless_stack_is_ours(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            port = s.getsockname()[1]
            r = self.run_script(port, jaeger=False)
            self.assertEqual(r.returncode, 1)
            self.assertIn("port %d is in use" % port, r.stdout)
            ok = self.run_script(port, jaeger=True)
            self.assertEqual(ok.returncode, 0)
            self.assertEqual(ok.stdout, "")  # silent on success; make prints the Tools line
        self.assertEqual(self.run_script(port, jaeger=False).returncode, 0)  # free again

    def test_logged_out_claude_fails(self):
        r = self.run_script(1, jaeger=False, logged_in=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("claude is not logged in", r.stdout)


if __name__ == "__main__":
    unittest.main()
