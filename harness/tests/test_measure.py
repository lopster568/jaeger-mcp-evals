#!/usr/bin/env python3
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

    def measure(self, *args):
        return subprocess.run(["bash", MEASURE, self.repo, "cartFailure", *args], capture_output=True, text=True, cwd=ROOT)

    def test_scripts_parse(self):
        for f in ("harness/measure.sh", "fixture/build-image.sh", "fixture/swap-jaeger.sh", "fixture/build-variant.sh"):
            self.assertEqual(subprocess.run(["bash", "-n", str(ROOT / f)]).returncode, 0, f)

    def test_dry_run_prints_the_plan_and_builds_nothing(self):
        r = self.measure("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        for want in ("build-image.sh", "swap-jaeger.sh", "bench.py run", "judge.py", "fisher_p_max", "claude-sonnet-5-5"):
            self.assertIn(want, r.stdout)
        for rec in ("measure-base.json", "measure-change.json"):  # stock wording: no desc-change.json check on the variant
            self.assertTrue(any('d["descriptions"]=None' in l and l.endswith(rec) for l in r.stdout.splitlines()), rec)
        self.assertFalse(self.pair.exists())  # dry run writes no pair file
        self.assertEqual(subprocess.run(["git", "-C", self.repo, "worktree", "list"], capture_output=True, text=True).stdout.count("\n"), 1)

    def planned_pair(self):
        out = self.measure("--dry-run").stdout
        text = out.split("would write %s:\n" % self.pair, 1)[1]
        return json.JSONDecoder().raw_decode(text)[0]

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
