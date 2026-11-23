#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""fixture.env loading: harness/config.py and fixture/lib.sh read the same values from the same two files."""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "harness"))
import config  # noqa: E402

KEYS = ("GOOSE_PROVIDER", "GOOSE_MODEL", "GRADER_CLIENT", "RUNS_DIR", "PHOENIX_PORT")


class EnvFiles(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "fixture").mkdir()
        shutil.copy(REPO / "fixture" / "lib.sh", self.root / "fixture")
        shutil.copy(REPO / "fixture.env.example", self.root)

    def both(self):
        py = config.load(str(self.root))
        env = {k: v for k, v in os.environ.items() if k not in config.load(str(self.root))}
        out = subprocess.run(["bash", "-c", '. fixture/lib.sh; for k in %s; do echo "$k=${!k}"; done' % " ".join(KEYS)],
                             cwd=self.root, env=env, capture_output=True, text=True, check=True).stdout
        sh = dict(line.split("=", 1) for line in out.splitlines())
        return {k: py[k] for k in KEYS}, sh

    def test_commented_defaults_apply(self):
        py, sh = self.both()
        want = {"GOOSE_PROVIDER": "openrouter", "GOOSE_MODEL": "nvidia/nemotron-3.5-lightning:free", "GRADER_CLIENT": "goose", "RUNS_DIR": "runs", "PHOENIX_PORT": "16006"}
        self.assertEqual(py, want)
        self.assertEqual(sh, want)

    def test_fixture_env_overrides_and_its_comments_stay_comments(self):
        (self.root / "fixture.env").write_text("GOOSE_PROVIDER=openrouter   # inline comment\n"
                                               "GRADER_CLIENT='cli'\n#RUNS_DIR=elsewhere\n")
        py, sh = self.both()
        want = {"GOOSE_PROVIDER": "openrouter", "GOOSE_MODEL": "nvidia/nemotron-3.5-lightning:free", "GRADER_CLIENT": "cli", "RUNS_DIR": "runs", "PHOENIX_PORT": "16006"}
        self.assertEqual(py, want)
        self.assertEqual(sh, want)

    def test_example_lists_every_key_once(self):
        keys = [line.lstrip("#").split("=", 1)[0] for line in (REPO / "fixture.env.example").read_text().splitlines()
                if line.lstrip("#")[:1].isupper() and "=" in line]
        self.assertEqual(len(keys), len(set(keys)))


if __name__ == "__main__":
    unittest.main()
