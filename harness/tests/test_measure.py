#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""harness/measure.sh on a throwaway git repo: --dry-run prints the plan and builds nothing, a dirty
checkout is refused. No docker, no ssh, no model; syntax of the three fixture scripts is checked too."""
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
MEASURE = str(ROOT / "harness" / "measure.sh")


def git(repo, *args):
    subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t", *args], check=True, capture_output=True)


class Measure(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = self._tmp.name
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "base")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "change")
        self.head = subprocess.run(["git", "-C", self.repo, "rev-parse", "--short=8", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.pair = ROOT / "harness" / "experiments" / ("measure-cartFailure-%s.json" % self.head)
        self.addCleanup(lambda: self.pair.unlink(missing_ok=True))

    def measure(self, *args, **env):
        env = dict(os.environ, **dict({"GOOSE_PROVIDER": "openrouter", "GOOSE_MODEL": "vendor/model-x"}, **env))
        return subprocess.run(["bash", MEASURE, self.repo, "cartFailure", *args], capture_output=True, text=True, cwd=ROOT, env=env)

    def test_scripts_parse(self):
        for f in ("harness/measure.sh", "fixture/build-image.sh", "fixture/swap-jaeger.sh"):
            self.assertEqual(subprocess.run(["bash", "-n", str(ROOT / f)]).returncode, 0, f)

    def test_dry_run_prints_the_plan_and_builds_nothing(self):
        r = self.measure("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        for want in ("build-image.sh", "swap-jaeger.sh", "bench.py run", "judge.py", "fisher_p_max", "vendor/model-x"):
            self.assertIn(want, r.stdout)
        self.assertFalse(self.pair.exists())  # dry run writes no pair file
        self.assertEqual(subprocess.run(["git", "-C", self.repo, "worktree", "list"], capture_output=True, text=True).stdout.count("\n"), 1)

    def test_dry_run_runs_without_export_then_exports_both_batches_after_judge(self):
        out = self.measure("--dry-run").stdout.splitlines()
        runs = [l for l in out if "bench.py run" in l]
        self.assertEqual(len(runs), 2)
        self.assertTrue(all(l.endswith("--no-export") for l in runs), runs)
        tail = [l for l in out if "judge.py" in l or "bench.py export" in l]
        self.assertIn("judge.py", tail[0])
        self.assertEqual([l.split("bench.py ")[-1] for l in tail[1:]], ["export <baseline batch dir>", "export <variant batch dir>"])

    def planned_pair(self, *args):
        out = self.measure("--dry-run", *args).stdout
        text = out.split("would write %s:\n" % self.pair, 1)[1]
        return json.JSONDecoder().raw_decode(text)[0]

    def test_pair_file_runs_goose_by_default(self):
        run = self.planned_pair()["run"]
        self.assertEqual((run["client"], run["provider"], run["model"]), ("goose", "openrouter", "vendor/model-x"))
        self.assertNotIn("bill_api_key", run)

    def test_client_cli_runs_the_claude_cli_on_the_api_key(self):
        run = self.planned_pair("--client", "cli")["run"]
        self.assertEqual((run["client"], run["provider"], run["model"], run["bill_api_key"]), ("cli", None, "claude-sonnet-5-5", True))

    def test_goose_settings_required(self):
        for env, said in (({"GOOSE_MODEL": ""}, "GOOSE_MODEL is empty"), ({"GOOSE_PROVIDER": ""}, "GOOSE_PROVIDER must be")):
            r = self.measure("--dry-run", **env)
            self.assertEqual(r.returncode, 2)
            self.assertIn(said, r.stderr)
            self.assertIn("fix: set", r.stderr)

    def test_generated_pair_file_is_not_pinned_to_a_client_version(self):
        self.assertNotIn("client_version", self.planned_pair()["run"])

    def test_existing_pair_file_keeps_its_client_version_pin(self):
        doc = self.planned_pair()
        doc["run"]["client_version"] = "2.1.285"
        self.pair.write_text(json.dumps(doc))
        r = self.measure("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("exists and matches, keeping it (a run.client_version in it is kept as the exact re-run pin)", r.stdout)
        doc["run"]["max_turns"] = 31  # a real difference still refuses
        self.pair.write_text(json.dumps(doc))
        r = self.measure("--dry-run")
        self.assertEqual(r.returncode, 2)
        self.assertIn("exists and differs", r.stderr)

    def test_dirty_checkout_refused(self):
        (pathlib.Path(self.repo) / "x").write_text("x")
        r = self.measure("--dry-run")
        self.assertEqual(r.returncode, 2)
        self.assertIn("uncommitted changes", r.stderr)


if __name__ == "__main__":
    unittest.main()
