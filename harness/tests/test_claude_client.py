#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""harness/claude_client.py, the Claude Code CLI adapter (run.client cli), through `bench.py run` against the
fake fixture of test_bench_batch.py: the exact `claude -p` argv, the flag check, plan-versus-key billing, the
sandbox probe, the model pin and budget warning, and the cli shape of a record (meta, manifest, cli grader,
export) that verify and the re-scoring of the published cli records read.

Run with: python3 -m unittest discover -s harness/tests
"""
import glob
import json
import os
import pathlib
import shutil
import sys
import unittest
from unittest import mock

import test_bench_batch as tbb
from test_bench_batch import CLI, HARNESS_DIR, META_V4_KEYS, RUN, STOCK_IMAGE, BenchCase, Fake, arm, load, read, text
import bench  # noqa: E402  (test_bench_batch put harness/ on sys.path)
import claude_client  # noqa: E402


class CliCase(BenchCase):
    """BenchCase on the cli client: no API key in the environment (a logged-in plan), calls() reads the fake claude's log."""
    RUN = dict(RUN, **CLI)

    def setUp(self):
        super().setUp()
        del self.env["ANTHROPIC_API_KEY"]

    def calls(self):
        return bench.read_jsonl(os.path.join(self.home, "claude-calls.jsonl"))


class TestClaudeBatch(CliCase):
    refused = tbb.TestKnobFile.refused
    spans = tbb.TestExport.spans
    assert_restored = tbb.TestBatch.assert_restored

    def test_hung_cell_is_killed_recorded_failed_and_flag_restored(self):  # bench.py kills claude itself: no graceful stop
        with mock.patch.object(bench, "CELL_TIMEOUT_S", 0.5):
            self.assertEqual(self.run_batch(3, FAKE_CLAUDE_HANG="1"), 4, self.err)
        b = self.assert_restored()
        cells = bench.read_jsonl(os.path.join(b, "cells.jsonl"))
        self.assertEqual([(c["failed"], c["trial_exit_code"]) for c in cells], [(True, 124)] * 3)
        self.assertIn("cell killed after", read(os.path.join(b, "%d-%s" % (cells[0]["order_index"], cells[0]["arm"]), "stderr.txt")))

    def test_three_failures_abort_exit_4(self):
        self.assertEqual(self.run_batch(3, FAKE_CLAUDE_EXIT="1"), 4, self.err)
        b = self.batch_dir()
        cells = bench.read_jsonl(os.path.join(b, "cells.jsonl"))
        self.assertEqual([c["failed"] for c in cells], [True] * 3)
        self.assertTrue(load(os.path.join(b, "restore.json"))["default_confirmed"])
        self.assertEqual(self.flag_file(), self.pristine())


    def test_full_batch(self):
        Fake.served_descriptions = {"get_services": "Summaries with an error flag per trace."}  # stock Jaeger wording passes
        self.assertEqual(self.run_batch(1, ANTHROPIC_API_KEY="sk-test"), 0, self.err)
        b = self.batch_dir()
        m = load(os.path.join(b, "manifest.json"))
        cells = bench.read_jsonl(os.path.join(b, "cells.jsonl"))
        for c in cells:
            trial = os.path.join(b, "%d-%s" % (c["order_index"], c["arm"]))
            meta = load(os.path.join(trial, "meta.json"))
            self.assertEqual(list(meta), META_V4_KEYS)
            self.assertEqual((meta["schema_version"], meta["client"], meta["provider"], meta["agent_loop"]), (5, "claude", None, None))
            self.assertEqual(meta["observed"], {"client_version": "2.1.282", "model": "claude-sonnet-5",
                                                "tools": ["StructuredOutput"] + ["mcp__jaeger__" + n for n in (
                                                    "get_critical_path", "get_trace_errors", "get_trace_topology", "get_services")],
                                                "skills": ["s1"], "agents": ["a1"], "compaction_events": 0})
            self.assertEqual(meta["client_version_pre"], "2.1.282 (Claude Code)")
            self.assertEqual(meta["max_budget_usd"], 2.0)
            self.assertEqual(meta["client_argv"][2], "sha256:" + meta["prompt_sha256"])
            self.assertEqual(meta["client_argv"][meta["client_argv"].index("--mcp-config") + 1], "mcp.json")
        # The exact cli argv, pinned.
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        first = cells[0]
        mcp = os.path.join(b, "%d-%s" % (first["order_index"], first["arm"]), "mcp.json")
        self.assertEqual(calls[0]["argv"][1:], [
            "-p", text("prompts/neutral.txt"), "--output-format", "stream-json", "--verbose",
            "--mcp-config", mcp, "--strict-mcp-config", "--setting-sources", "", "--restricted", "--tools", "",
            "--allowedTools", "mcp__jaeger__*", "--system-prompt", text("system-prompt.txt"),
            "--json-schema", text("verdict-schema.json"), "--model", "sonnet", "--effort", "xhigh",
            "--max-turns", "30", "--max-budget-usd", "2"])
        self.assertFalse(calls[0]["claudecode"])
        self.assertFalse(calls[0]["api_key"])
        self.assertEqual([c["cwd_listing"] for c in calls], [[], []])
        # The grader: sandboxed like the agent; both trials gave the same answer, so one call and one cache hit.
        graders = bench.read_jsonl(os.path.join(self.home, "grader-calls.jsonl"))
        self.assertEqual(len(graders), 1)
        self.assertFalse(graders[0]["claudecode"] or graders[0]["api_key"])
        self.assertEqual(m["grader"]["client"], "claude")

    def test_a_cli_experiment_file_runs_and_records_claude(self):
        """run.client cli, as every file under records/inputs says, loads as claude: same history policy, content untouched."""
        self.knob()
        doc = load(self.knob_path)
        doc["run"]["client"] = "cli"
        pathlib.Path(self.knob_path).write_text(json.dumps(doc))
        self.assertEqual(self.bench("run", self.knob_path, ANTHROPIC_API_KEY="sk-test"), 0, self.err)
        b = self.batch_dir()
        m = load(os.path.join(b, "manifest.json"))
        self.assertEqual((m["client"], m["history_policy"]), ("claude", {"harness_truncation": "none", "cli_autocompact": "default"}))
        self.assertEqual(m["experiment"]["content"]["run"]["client"], "cli")  # the record is the file as written
        self.assertEqual(m["experiment"]["sha256"], bench.sha256_file(self.knob_path))
        for meta in glob.glob(os.path.join(b, "*-*", "meta.json")):
            self.assertEqual(load(meta)["client"], "claude")

    def test_cli_refused_before_anything_is_touched_when_claude_lacks_a_flag(self):
        self.assertEqual(self.run_batch(1, FAKE_CLAUDE_HELP="--restricted --effort"), 1)
        out = self.err + self.out
        self.assertIn("does not accept --setting-sources --strict-mcp-config --json-schema", out)
        self.assertIn("claude update", out)
        self.assertEqual(self.calls(), [])
        self.assertEqual(glob.glob(os.path.join(self.runs, "paymentFailure", "batch-*")), [])

    def test_cli_says_the_api_key_is_ignored(self):
        self.assertEqual(self.run_batch(1, "--dry-run", ANTHROPIC_API_KEY="sk-x"), 0, self.err)
        self.assertIn("runs on the logged-in plan and ignores it", self.err + self.out)

    def test_bill_api_key_runs_cli_and_grader_on_the_key(self):
        self.addCleanup(setattr, bench.grade, "API_KEY", None)
        self.assertEqual(self.run_batch(1, run={"bill_api_key": True}, ANTHROPIC_API_KEY="sk-test-secret"), 0, self.err)
        self.assertIn("agent and grader bill ANTHROPIC_API_KEY", self.err + self.out)
        self.assertTrue(all(c["api_key"] for c in self.calls()))
        graders = bench.read_jsonl(os.path.join(self.home, "grader-calls.jsonl"))
        self.assertTrue(graders and all(g["api_key"] and "--verbose" in g["argv"] for g in graders))
        b = self.batch_dir()
        self.assertEqual(load(os.path.join(b, "manifest.json"))["billing"], "ANTHROPIC_API_KEY")
        for path in glob.glob(os.path.join(b, "**"), recursive=True) + glob.glob(os.path.join(self.records, "**"), recursive=True):
            if os.path.isfile(path):
                self.assertNotIn("sk-test-secret", read(path), path)
        self.assertNotIn("sk-test-secret", json.dumps([c["argv"] for c in self.calls()]))

    def test_bill_api_key_skips_the_grader_login_check(self):
        self.addCleanup(setattr, bench.grade, "API_KEY", None)
        self.assertEqual(self.run_batch(1, "--dry-run", run={"bill_api_key": True}, ANTHROPIC_API_KEY="sk-x", FAKE_LOGGED_OUT="1"), 0, self.err)
        self.assertFalse(os.path.exists(os.path.join(self.home, "auth-calls.jsonl")))  # the grader bills the key: its login is unused
        self.assertEqual(self.run_batch(1, "--dry-run", FAKE_LOGGED_OUT="1"), 1)
        self.assertIn("grader is not logged in", self.err)

    def test_bill_api_key_aborts_when_the_stream_reports_another_source(self):
        self.addCleanup(setattr, bench.grade, "API_KEY", None)
        self.assertEqual(self.run_batch(2, run={"bill_api_key": True}, ANTHROPIC_API_KEY="sk-x", FAKE_KEY_SOURCE="none"), 4)
        self.assertIn("billing: run.bill_api_key is true but the stream init reports apiKeySource 'none'", self.err + self.out)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.flag_file(), self.pristine())

    def test_model_pin_aborts_when_the_stream_reports_another_model(self):
        self.assertEqual(self.run_batch(2, run={"model": "claude-sonnet-5-5"}), 4, self.err)
        self.assertIn("model pin: run.model is claude-sonnet-5-5 but the stream reports claude-sonnet-5", self.err)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.flag_file(), self.pristine())
        self.assertEqual(self.run_batch(1, "--dry-run"), 0, self.err)  # sonnet is an alias: warned, not pinned
        self.assertIn("WARNING: run.model 'sonnet' is an alias", self.err)

    def test_bill_api_key_needs_the_key(self):
        self.assertEqual(self.run_batch(1, run={"bill_api_key": True}, ANTHROPIC_API_KEY=""), 1)
        self.assertIn("run.bill_api_key is true but ANTHROPIC_API_KEY is empty", self.err)
        self.assertEqual(self.calls(), [])

    def test_default_cli_still_strips_the_key(self):
        self.assertEqual(self.run_batch(1, ANTHROPIC_API_KEY="sk-x"), 0, self.err)
        self.assertFalse(any(c["api_key"] for c in self.calls()))
        self.assertFalse(any(g["api_key"] for g in bench.read_jsonl(os.path.join(self.home, "grader-calls.jsonl"))))
        self.assertNotIn("billing", load(os.path.join(self.batch_dir(), "manifest.json")))

    def test_summary_counts_errors_and_stops(self):
        self.assertEqual(self.run_batch(1, FAKE_STOP="error_max_turns"), 0, self.err)
        blog = read(os.path.join(self.batch_dir(), "batch.log"))
        self.assertIn("noskill: n=1 PASS=1 PARTIAL=0 FAIL=0 ABSTAIN=0 ERROR=0 INVALID=0 LEAK=0 stops=error_max_turns:1 ", blog)
        self.assertIn("ERROR=0 INVALID=0 LEAK=0 stops=error_max_turns:1 read_skill_attempted", blog)
        self.assertIn("stops error_max_turns:1", self.err)
        shutil.rmtree(self.runs)
        self.assertEqual(self.run_batch(1, FAKE_JUNK="1"), 7, self.err)
        blog = read(os.path.join(self.batch_dir(), "batch.log"))
        self.assertIn("noskill: n=1 PASS=0 PARTIAL=0 FAIL=0 ABSTAIN=0 ERROR=1 INVALID=0 LEAK=0 stops=- ", blog)
        self.assertIn("band paymentFailure/noskill: n=1 passes=0", blog)
        self.assertIn("ERROR=1 INVALID=0 LEAK=0 stops=- read_skill_attempted", blog)
        self.assertRegex(self.err, r"noskill #0 +ERROR +- .*unscorable")

    def test_observe_skips_non_object_lines(self):
        p = os.path.join(self.home, "s.jsonl")
        pathlib.Path(p).write_text('[1, 2]\n"x"\n{"type": "system", "subtype": "init", "model": "m"}\n')
        self.assertEqual(bench.observe(p)["model"], "m")

    def test_export_posts_one_trace_per_trial_with_run_attributes(self):
        self.assertEqual(self.run_batch(1, "--no-export"), 0, self.err)
        b = self.batch_dir()
        self.assertEqual(self.bench("export", b, PHOENIX_PORT=self.phoenix_port), 0, self.out)  # default endpoint from fixture.env
        (_, body), = Fake.otlp_posts
        resource, spans = self.spans(body)
        kinds = [s["attrs"]["openinference.span.kind"] for s in spans]
        self.assertEqual(sorted(kinds), ["AGENT", "AGENT", "LLM", "LLM", "TOOL", "TOOL"])
        for root in (s for s in spans if s["attrs"]["openinference.span.kind"] == "AGENT"):
            a = root["attrs"]
            self.assertEqual((a["client"], a["provider"]), ("claude", "anthropic"))  # provider from the claude fallback
            tid = root[1][0]
            children = [s for s in spans if s[1][0] == tid and s is not root]
            tool = next(s for s in children if s["attrs"]["openinference.span.kind"] == "TOOL")
            self.assertEqual(tool["attrs"]["tool.name"], "mcp__jaeger__get_trace_errors")
            self.assertEqual(tool["attrs"]["output.value"], "Payment request failed. Invalid token.")


class TestSandboxProbe(CliCase):
    """Pre-flight's sandbox probe: the trial's own claude argv against a local 400 server."""
    SERVED = ["StructuredOutput"] + ["mcp__jaeger__" + n for n in
                                     ("get_critical_path", "get_trace_errors", "get_trace_topology", "get_services")]
    refused = tbb.TestKnobFile.refused

    def probes(self):
        p = os.path.join(self.home, "probe-calls.jsonl")
        return bench.read_jsonl(p) if os.path.isfile(p) else []

    def test_pass_records_result_and_tool_names_and_runs_the_trial_argv(self):
        arms = {"noskill": arm("neutral"), "off": arm("neutral", tools=False)}
        self.assertEqual(self.run_batch(1, arms=arms, ANTHROPIC_API_KEY="sk-test"), 0, self.err)
        pre = load(os.path.join(self.batch_dir(), "preflight.json"))
        self.assertEqual(pre["sandbox_probe"], {"result": "PASS", "tools": {"noskill": self.SERVED, "off": ["StructuredOutput"]}})
        probes = {bool(p["mcp"]["mcpServers"]): p for p in self.probes()}
        url = "http://127.0.0.1:%s/jaeger/ui/api/ai/mcp/" % self.port
        self.assertEqual(probes[True]["mcp"], {"mcpServers": {"jaeger": {"type": "http", "url": url}}})
        trial = self.calls()[0]["argv"]  # both arms use prompt neutral: argvs differ only in --mcp-config
        self.assertEqual(probes[True]["argv"][-1], "--no-session-persistence")
        drop = lambda argv: [x for i, x in enumerate(argv) if i == 0 or argv[i - 1] != "--mcp-config"]
        self.assertEqual(drop(probes[True]["argv"][1:-1]), drop(trial[1:]))
        self.assertNotIn(self.home, probes[True]["cwd"])
        for root in (self.runs, self.records):
            for dirpath, _, files in os.walk(root):
                for f in files:
                    self.assertNotIn("probe-secret", read(os.path.join(dirpath, f)), f)

    def test_trial_probe_and_grader_get_the_client_env_and_it_is_not_in_argv(self):
        self.assertEqual(self.run_batch(1), 0, self.err)
        want = ["false", "1"]
        for seen in (self.calls(), self.probes(), bench.read_jsonl(os.path.join(self.home, "grader-calls.jsonl"))):
            self.assertTrue(seen)
            self.assertEqual({tuple(c["client_env"]) for c in seen}, {tuple(want)})
        self.assertNotIn("ENABLE_TOOL_SEARCH", json.dumps(load(glob.glob(os.path.join(self.batch_dir(), "0-*", "meta.json"))[0])["client_argv"]))

    def test_user_plugin_dirs_never_reach_the_trial_probe_or_grader(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_PLUGIN_DIRS": "/somewhere/my-plugin"}):
            self.assertEqual(self.run_batch(1), 0, self.err)
        for seen in (self.calls(), self.probes(), bench.read_jsonl(os.path.join(self.home, "grader-calls.jsonl"))):
            self.assertTrue(seen)
            self.assertEqual({c["plugin_dirs"] for c in seen}, {None})

    def test_failure_names_the_unexpected_tool_and_the_cli_version_in_the_fix(self):
        self.assertEqual(self.run_batch(1, FAKE_PROBE="extra_tool"), 1, self.err)
        self.assertIn("fix: claude CLI 2.1.282 sends a tool the harness does not expect (Bash). If it is new in this CLI version, "
                      "find the setting that turns it off and add it to grade.CLIENT_ENV, or run a CLI version the probe accepts", self.err)

    def test_failures_abort_before_the_flag(self):
        for mode, why in (("extra_tool", "tools sent"), ("server_tool", "server-side tool web_search"),
                          ("memory", "memory marker 'CLAUDE.md'"), ("silent", "no model request")):
            with self.subTest(mode=mode):
                self.assertEqual(self.run_batch(1, FAKE_PROBE=mode), 1, self.err)
                self.assertIn("sandbox probe", self.err)
                self.assertIn(why, self.err)
                self.assertNotIn("probe-secret", self.err)
                self.assertEqual(self.calls(), [])
                self.assertEqual(self.flag_file(), self.pristine())
                self.assertNotIn("python3 -", read(os.path.join(self.home, "ssh.log")))
                self.assertFalse(os.path.exists(self.runs))

    def write_recorded(self, rows, client="cli"):
        os.makedirs(os.path.join(self.records, "old", "batch-1"), exist_ok=True)
        with open(os.path.join(self.records, "old", "batch-1", "manifest.json"), "w") as f:  # as every record has
            json.dump({"client": client}, f)
        with open(os.path.join(self.records, "old", "batch-1", "scores.jsonl"), "w") as f:
            f.writelines(json.dumps(dict({"scenario_used": "paymentFailure", "model_asserted": "sonnet"}, **r)) + "\n" for r in rows)

    def test_stage_one_names_the_cli_version_and_says_when_cost_is_unknown(self):
        self.assertEqual(self.run_batch(1, "--dry-run"), 0, self.err)
        self.assertIn("ok    claude CLI 2.1.282 accepts the flags the harness passes", self.err)
        self.assertIn("ok    expected cost unknown: no recorded claude trial of paymentFailure on sonnet", self.err)

    def test_expected_cost_is_per_arm_median_times_n(self):
        self.write_recorded([{"cost_usd": x} for x in (0.1, 0.2, 0.3)])
        self.assertEqual(bench.expected_cost("paymentFailure", "sonnet", 10, 2, records=self.records),
                         [(True, "expected cost about 2.00 USD per arm of 10 trials (recorded max 0.30 per trial, cap 2)")])
        # recorded claude trials say nothing about a goose run on the same model id
        self.assertEqual(bench.expected_cost("paymentFailure", "sonnet", 10, 2, records=self.records, client="goose"),
                         [(True, "expected cost unknown: no recorded goose trial of paymentFailure on sonnet")])
        os.remove(os.path.join(self.records, "old", "batch-1", "manifest.json"))  # no manifest: no known client, not read
        self.assertEqual(bench.expected_cost("paymentFailure", "sonnet", 10, 2, records=self.records),
                         [(True, "expected cost unknown: no recorded claude trial of paymentFailure on sonnet")])

    def test_a_cli_manifest_counts_for_the_claude_expected_cost(self):
        self.write_recorded([{"cost_usd": x} for x in (0.1, 0.2, 0.3)], client="cli")
        self.assertEqual(bench.expected_cost("paymentFailure", "sonnet", 10, 2, records=self.records, client="claude")[0][1][:28],
                         "expected cost about 2.00 USD")

    def test_max_budget_usd_is_required_for_the_cli(self):
        self.refused(lambda d: d["run"].pop("max_budget_usd"), "run.max_budget_usd must be a positive number")
        self.write_recorded([{"cost_usd": 0.3}], client="goose")
        self.assertEqual(bench.expected_cost("paymentFailure", "sonnet", 1, None, records=self.records, client="goose"),
                         [(True, "expected cost about 0.30 USD per arm of 1 trials (recorded max 0.30 per trial)")])  # no cap to name

    def test_expected_cost_from_recorded_trials_and_no_warning_when_far_below_the_cap(self):
        self.write_recorded([{"cost_usd": c, "num_turns": 10} for c in (0.1, 0.2, 0.3)] +
                            [{"cost_usd": 9, "num_turns": 30, "scenario_used": "other"}, {"cost_usd": 9, "model_asserted": "m2"}])
        self.assertEqual(self.run_batch(1, "--dry-run"), 0, self.err)
        self.assertRegex(self.err, r"ok    expected cost about 0\.\d\d USD per arm of \d+ trials \(recorded max 0\.30 per trial, cap 2\)")
        self.assertNotIn("cut off", self.err)
        self.assertNotIn("turns;", self.err)

    def test_warns_before_spending_when_a_recorded_trial_is_near_the_cap(self):
        self.write_recorded([{"cost_usd": 1.2, "num_turns": 25}, {"cost_usd": 0.4, "num_turns": 9}])
        self.assertEqual(self.run_batch(1, "--dry-run"), 0, self.err)
        self.assertIn("WARNING: a trial may be cut off by the per-trial cap and lose its verdict; raise run.max_budget_usd "
                      "or expect ERROR trials", self.err)
        self.assertNotIn("of 30 turns", self.err)
        self.write_recorded([{"cost_usd": 1.2, "num_turns": 25}, {"cost_usd": 0.4, "num_turns": 9}], client="goose")
        lines = bench.expected_cost("paymentFailure", "sonnet", 1, 2, records=self.records, client="goose")
        self.assertEqual([ok for ok, _ in lines], [True])  # goose gets no budget cap, so nothing to warn about

    def test_dry_run_probes_too(self):
        self.assertEqual(self.run_batch(1, "--dry-run"), 0, self.err)
        self.assertEqual(len(self.probes()), 2)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.run_batch(1, "--dry-run", FAKE_PROBE="extra_tool"), 1, self.err)

    def test_a_hung_cli_is_killed_at_the_timeout(self):
        t0 = bench.time.monotonic()
        problems, names = claude_client.sandbox_probe([sys.executable, "-c", "import time; time.sleep(60)"], dict(os.environ), [], timeout=1)
        self.assertLess(bench.time.monotonic() - t0, 10)
        self.assertEqual((names, len(problems)), ([], 1))
        self.assertIn("no model request", problems[0])

    def test_a_logged_out_cli_says_so_in_the_problem(self):
        code = "import sys; sys.stderr.write('Not logged in \\u00b7 Please run /login\\n'); sys.exit(1)"
        problems, names = claude_client.sandbox_probe([sys.executable, "-c", code], dict(os.environ), [], timeout=10)
        self.assertEqual(names, [])
        self.assertIn("claude said: Not logged in \u00b7 Please run /login", problems[0])

    def test_need_key_requires_an_x_api_key_header_and_reports_only_booleans(self):
        code = ("import os, urllib.request as u, urllib.error as e\n"
                "r = u.Request(os.environ['ANTHROPIC_BASE_URL'] + '/v1/messages', b'{}', {'authorization': 'Bearer SECRETTOKEN'}"
                " if os.environ.get('NOKEY') else {'x-api-key': 'SECRETKEY'})\n"
                "try: u.urlopen(r)\n"
                "except e.HTTPError: pass\n")
        ok, _ = claude_client.sandbox_probe([sys.executable, "-c", code], dict(os.environ), [], timeout=10, need_key=True)
        self.assertFalse([p for p in ok if "billing" in p])
        bad, _ = claude_client.sandbox_probe([sys.executable, "-c", code], dict(os.environ, NOKEY="1"), [], timeout=10, need_key=True)
        msg = [p for p in bad if "billing" in p]
        self.assertEqual(len(msg), 1)
        self.assertIn("authorization header present: True", msg[0])
        self.assertNotIn("SECRET", " ".join(bad))
        off, _ = claude_client.sandbox_probe([sys.executable, "-c", code], dict(os.environ, NOKEY="1"), [], timeout=10)
        self.assertFalse([p for p in off if "billing" in p])


if __name__ == "__main__":
    unittest.main()
