#!/usr/bin/env python3
"""fixture/forward.sh: port and argument parsing (sourced, no ssh), --check against a listening socket,
and the restart loop against a PATH shim for ssh that dies at once."""
import os
import pathlib
import signal
import socket
import subprocess
import tempfile
import time
import unittest

SCRIPT = str(pathlib.Path(__file__).resolve().parents[2] / "fixture" / "forward.sh")
ENV = dict(os.environ, FIXTURE_HOST="127.0.0.3", JAEGER_UI_PORT="16686", OFREP_PORT="8016", PHOENIX_PORT="16006")


def bash(code, env=ENV):
    return subprocess.run(["bash", "-c", code], env=env, capture_output=True, text=True)


class Forward(unittest.TestCase):
    def test_syntax(self):
        self.assertEqual(subprocess.run(["bash", "-n", SCRIPT]).returncode, 0)

    def test_ports_and_args(self):
        self.assertEqual(bash('PHOENIX=; . %s; fwd_ports' % SCRIPT).stdout.split(), ["16686", "8016"])
        self.assertEqual(bash('PHOENIX=1; . %s; fwd_ports' % SCRIPT).stdout.split(), ["16686", "8016", "16006"])
        self.assertEqual(bash('PHOENIX=; . %s; fwd_args' % SCRIPT).stdout.split(),
                         ["-L", "127.0.0.3:16686:127.0.0.1:16686", "-L", "127.0.0.3:8016:127.0.0.1:8016"])

    def test_non_loopback_host_refused(self):
        r = subprocess.run([SCRIPT, "somevm"], env=dict(ENV, FIXTURE_HOST="10.0.0.5"), capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("loopback", r.stderr)

    def test_check_reports_each_port(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            up = str(s.getsockname()[1])
            env = dict(ENV, FIXTURE_HOST="127.0.0.1", JAEGER_UI_PORT=up, OFREP_PORT="1")
            r = subprocess.run([SCRIPT, "--check"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout.splitlines(), ["ok    127.0.0.1:" + up, "DOWN  127.0.0.1:1"])

    def test_restarts_and_exits_clean_on_sigint(self):
        with tempfile.TemporaryDirectory() as d:
            shim = pathlib.Path(d, "ssh")
            shim.write_text('#!/bin/bash\necho "$@" >> %s/args\nexit 255\n' % d)
            shim.chmod(0o755)
            env = dict(ENV, PATH=d + os.pathsep + os.environ["PATH"], FORWARD_SLEEP="0.2")
            p = subprocess.Popen([SCRIPT, "vm-target"], env=env, stderr=subprocess.PIPE, text=True)
            time.sleep(1)
            p.send_signal(signal.SIGINT)
            err = p.communicate(timeout=10)[1]
            args = pathlib.Path(d, "args").read_text().splitlines()
        self.assertEqual(p.returncode, 0)
        self.assertGreaterEqual(len(args), 2)
        for opt in ("ExitOnForwardFailure=yes", "ServerAliveInterval=15", "ServerAliveCountMax=8", "-N",
                    "-L 127.0.0.3:16686:127.0.0.1:16686", "vm-target"):
            self.assertIn(opt, args[0])
        self.assertRegex(err, r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ forward.sh: ssh exited \(255\), restarting")


if __name__ == "__main__":
    unittest.main()
