#!/usr/bin/env python3
"""fixture/preflight.sh: busy fixture ports fail unless the stack is already ours. docker is a PATH shim."""
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


class Preflight(unittest.TestCase):
    def run_script(self, port, jaeger):
        with tempfile.TemporaryDirectory() as d:
            docker = os.path.join(d, "docker")
            pathlib.Path(docker).write_text(DOCKER)
            os.chmod(docker, os.stat(docker).st_mode | stat.S_IEXEC)
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
            self.assertEqual(self.run_script(port, jaeger=True).returncode, 0)
        self.assertEqual(self.run_script(port, jaeger=False).returncode, 0)  # free again


if __name__ == "__main__":
    unittest.main()
