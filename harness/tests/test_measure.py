#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""harness/measure.sh on a throwaway git repo: --dry-run prints the plan and builds nothing, a dirty
checkout is refused. No docker, no ssh, no model; syntax of the three fixture scripts is checked too."""
import json
import os
import pathlib
import shutil
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
        env = dict(os.environ, **dict({"GOOSE_PROVIDER": "openrouter", "GOOSE_MODEL": "vendor/model-x", "OPENROUTER_API_KEY": "k",
                                                         "CLIENT": "goose"}, **env))  # a local fixture.env may set CLIENT; empty would refuse
        return subprocess.run([shutil.which("bash"), MEASURE, self.repo, "cartFailure", *args], capture_output=True, text=True, cwd=ROOT, env=env)

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
        # no colour, so the Batch line parses with POSIX sed: FORCE_COLOR emptied, as it wins over NO_COLOR
        self.assertTrue(all(l.startswith("+ env FORCE_COLOR= NO_COLOR=1 ") for l in runs), runs)
        tail = [l for l in out if "judge.py" in l or "bench.py export" in l]
        self.assertIn("judge.py", tail[0])
        self.assertEqual([l.split("bench.py ")[-1] for l in tail[1:]], ["export <baseline batch dir>", "export <variant batch dir>"])

    def planned_pair(self, *args, **env):
        out = self.measure("--dry-run", *args, **env).stdout
        text = out.split("would write %s:\n" % self.pair, 1)[1]
        return json.JSONDecoder().raw_decode(text)[0]

    def test_pair_file_runs_goose_by_default(self):
        run = self.planned_pair()["run"]
        self.assertEqual((run["client"], run["provider"], run["model"]), ("goose", "openrouter", "vendor/model-x"))
        self.assertNotIn("bill_api_key", run)
        self.assertIsNone(run["effort"])  # no GOOSE_THINKING_EFFORT, as the Jaeger demo

    def test_client_claude_runs_the_claude_cli_on_the_api_key(self):
        run = self.planned_pair("--client", "claude", ANTHROPIC_API_KEY="k")["run"]
        self.assertEqual((run["client"], run["provider"], run["model"], run["bill_api_key"], run["effort"]),
                         ("claude", None, "claude-sonnet-5-5", True, "high"))
        self.assertEqual(self.planned_pair("--client", "cli", ANTHROPIC_API_KEY="k")["run"], run)  # the old name, the same file

    def test_client_from_the_environment_without_a_flag(self):
        self.assertEqual(self.planned_pair(CLIENT="claude", ANTHROPIC_API_KEY="k")["run"]["client"], "claude")
        self.assertEqual(self.planned_pair(CLIENT="claude", ANTHROPIC_API_KEY="k", CLAUDE_MODEL="claude-x")["run"]["model"], "claude-x")
        self.assertEqual(self.planned_pair("--client", "goose", CLIENT="claude")["run"]["client"], "goose")  # the flag wins

    def test_client_claude_needs_the_api_key_before_any_build(self):
        r = self.measure("--dry-run", "--client", "claude", ANTHROPIC_API_KEY="")
        self.assertEqual(r.returncode, 2)
        self.assertIn("ANTHROPIC_API_KEY is empty; measure on claude bills the API key, not your claude.ai login", r.stderr)
        self.assertNotIn("build-image.sh", r.stdout)
        self.assertFalse(self.pair.exists())

    def test_client_codex_runs_codex_with_no_budget_cap(self):
        out = self.measure("--dry-run", "--client", "codex").stdout
        run = json.JSONDecoder().raw_decode(out.split("would write %s:\n" % self.pair, 1)[1])[0]["run"]
        self.assertEqual((run["client"], run["provider"], run["model"]), ("codex", None, "gpt-5.6-terra"))
        self.assertNotIn("max_budget_usd", run)
        self.assertIn("counts against your Codex login", out)
        self.assertEqual(self.measure("--dry-run", "--client", "codex", CODEX_MODEL="").returncode, 2)

    def test_an_unknown_client_names_what_was_typed(self):
        r = self.measure("--dry-run", "--client", "bogus")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--client is 'bogus', not goose, claude or codex", r.stderr)
        r = self.measure("--dry-run", CLIENT="bogus")
        self.assertIn("CLIENT is 'bogus', not goose, claude or codex", r.stderr)
        self.assertIn("CLIENT is '', not goose", self.measure("--dry-run", CLIENT="").stderr)

    def test_an_unknown_scenario_lists_the_scenarios(self):
        env = dict(os.environ, GOOSE_PROVIDER="openrouter", GOOSE_MODEL="m", OPENROUTER_API_KEY="k", CLIENT="goose")
        r = subprocess.run([shutil.which("bash"), MEASURE, self.repo, "nope", "--dry-run"], capture_output=True, text=True, cwd=ROOT, env=env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("no such scenario: nope; scenarios: ", r.stderr)
        self.assertIn(" paymentFailure", r.stderr)

    def test_an_existing_cli_pair_file_matches_a_claude_request(self):
        doc = self.planned_pair("--client", "claude", ANTHROPIC_API_KEY="k")
        doc["run"]["client"] = "cli"  # written by measure.sh before 2026-11-23
        self.pair.write_text(json.dumps(doc))
        r = self.measure("--dry-run", "--client", "claude", ANTHROPIC_API_KEY="k")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("exists and matches, keeping it", r.stdout)

    def test_goose_settings_required(self):
        for env, said in (({"GOOSE_MODEL": ""}, "GOOSE_MODEL is empty"), ({"GOOSE_PROVIDER": ""}, "GOOSE_PROVIDER must be")):
            r = self.measure("--dry-run", **env)
            self.assertEqual(r.returncode, 2)
            self.assertIn(said, r.stderr)
            self.assertIn("fix: set", r.stderr)

    def test_goose_key_required(self):
        r = self.measure("--dry-run", OPENROUTER_API_KEY="")
        self.assertEqual(r.returncode, 2)
        self.assertIn("OPENROUTER_API_KEY is empty", r.stderr)
        r = self.measure("--dry-run", GOOSE_PROVIDER="anthropic", ANTHROPIC_API_KEY="")
        self.assertIn("ANTHROPIC_API_KEY is empty", r.stderr)

    def test_free_model_warns_in_the_cost_lines(self):
        self.assertIn("free OpenRouter key covers a few trials a day, not 20", self.measure("--dry-run", GOOSE_MODEL="x:free").stdout)
        self.assertNotIn("free OpenRouter key", self.measure("--dry-run").stdout)

    def test_missing_go_stops_before_any_build(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bin_dir = pathlib.Path(tmp.name)
        for tool in ("git", "dirname", "grep", "sed", "mktemp"):
            (bin_dir / tool).symlink_to(shutil.which(tool))
        r = self.measure("--yes", PATH=str(bin_dir))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("Go (the version in Jaeger's go.mod) is needed to build", r.stderr)
        self.assertNotIn("build-image.sh", r.stdout)
        self.assertFalse(self.pair.exists())

    def test_dry_run_warns_when_go_is_missing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bin_dir = pathlib.Path(tmp.name)
        for tool in ("git", "dirname", "grep", "sed", "mktemp", "python3", "cut", "cat", "tr", "uname", "date"):
            if shutil.which(tool):
                (bin_dir / tool).symlink_to(shutil.which(tool))
        r = self.measure("--dry-run", PATH=str(bin_dir))
        self.assertEqual(r.returncode, 0, r.stderr)  # a dry run reports it and goes on
        self.assertIn("measure: WARN go is not on PATH; a real run stops here", r.stdout)

    def test_plan_lines(self):
        out = self.measure("--dry-run", "--client", "claude", "--n", "3", ANTHROPIC_API_KEY="k").stdout
        for want in ("measure: client claude, model claude-sonnet-5-5, 3 trials per arm, 2 arms, PAID, bills ANTHROPIC_API_KEY",
                     "measure: cost: 0.04 to 0.24 USD agent per trial (measured), 0.2 to 1.4 USD for 6 trials, plus one grader call each",
                     "measure: time: two image builds (skipped when the image exists), 2 x 5 min settle, then 6 trials (recorded trials: 12 to 83 s each)",
                     "measure: grader: claude claude-fable-5-1", "measure: go: ",
                     "n=3 per arm is rank only", "  baseline_passes\tmin_variant_passes_significant"):
            self.assertIn(want, out)
        self.assertNotIn("rank only", self.measure("--dry-run", "--n", "10").stdout)  # a table from 10 up is not needed
        self.assertIn("measure: grader: goose openrouter vendor/model-x", self.measure("--dry-run").stdout)
        self.assertIn("(Codex has no grader client)", self.measure("--dry-run", "--client", "codex").stdout)

    def test_pre_registration_commit_comes_after_the_cost_lines_and_before_the_first_swap(self):
        # the prompt sits between them and the commit; a dry run never prompts, so the plan order is what is checked
        out = self.measure("--dry-run").stdout
        self.assertLess(out.index("PAID"), out.index("+ git commit"))
        self.assertLess(out.index("+ git commit"), out.index("swap-jaeger.sh"))

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
