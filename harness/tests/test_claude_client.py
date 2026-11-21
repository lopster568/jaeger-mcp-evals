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

    def test_full_batch(self):
        Fake.served_descriptions = {"get_services": "Summaries with an error flag per trace."}  # stock Jaeger wording passes
        self.assertEqual(self.run_batch(1, ANTHROPIC_API_KEY="sk-test",
                                    OPENAI_API_KEY="sk-o", OPENAI_BASE_URL="http://x"), 0, self.err)
        b = self.batch_dir()
        for f in ("manifest.json", "cells.jsonl", "scores.jsonl", "restore.json", "preflight.json", "batch.log", "tools.json"):
            self.assertTrue(os.path.isfile(os.path.join(b, f)), f)
        m = load(os.path.join(b, "manifest.json"))
        for k in ("scenario", "batch_id", "n_per_arm", "model_requested", "effort", "arms", "seed", "order",
                  "file_hashes_sha256", "system_under_test", "tools_list_sha256", "tools_list_path", "schema_version"):
            self.assertIn(k, m)
        self.assertEqual(m["schema_version"], 5)
        self.assertEqual(m["arms"], ["noskill", "skill"])
        self.assertEqual(m["batch_id"], os.path.basename(b))
        self.assertEqual(m["tools_list_sha256"], bench.sha256_file(os.path.join(b, "tools.json")))
        self.assertEqual(m["jaeger_commit"], "abc123")
        # The knob file rides along verbatim, with its hash. It lives outside the repo in this
        # test (the tmp dir), so "file" is just its basename: a relpath would otherwise carry
        # the tmp dir's absolute path into the record (bench.repo_relpath).
        self.assertEqual(m["experiment"], {"name": "t", "file": os.path.basename(self.knob_path),
                                           "sha256": bench.sha256_file(self.knob_path), "content": load(self.knob_path)})
        cells = bench.read_jsonl(os.path.join(b, "cells.jsonl"))
        self.assertEqual(sorted(c["arm"] for c in cells), ["noskill", "skill"])
        scores = bench.read_jsonl(os.path.join(b, "scores.jsonl"))
        self.assertEqual([s["verdict"] for s in scores], ["PASS", "PASS"])
        self.assertNotIn("read_skill_called", scores[0])
        for c in cells:
            trial = os.path.join(b, "%d-%s" % (c["order_index"], c["arm"]))
            self.assertEqual(os.path.join(b, c["out_dir"]), trial)
            meta = load(os.path.join(trial, "meta.json"))
            self.assertEqual(list(meta), META_V4_KEYS)
            self.assertEqual((meta["schema_version"], meta["client"], meta["provider"], meta["agent_loop"]), (5, "cli", None, None))
            self.assertEqual(meta["experiment"], {k: m["experiment"][k] for k in ("name", "file", "sha256")})
            self.assertEqual(meta["run_id"], "%s/%d-%s" % (m["batch_id"], c["order_index"], c["arm"]))
            self.assertEqual(meta["arm"], c["arm"])
            self.assertEqual(meta["exit_code"], 0)
            self.assertEqual(meta["observed"], {"client_version": "2.1.282", "model": "claude-sonnet-5",
                                                "tools": ["StructuredOutput"] + ["mcp__jaeger__" + n for n in (
                                                    "get_critical_path", "get_trace_errors", "get_trace_topology", "get_services")],
                                                "skills": ["s1"], "agents": ["a1"], "compaction_events": 0})
            self.assertEqual(meta["client_version_pre"], "2.1.282 (Claude Code)")
            self.assertEqual(meta["jaeger_image"], STOCK_IMAGE)
            self.assertEqual(meta["jaeger_image_id"], "sha256:feedface")
            self.assertEqual(len(meta["otel_demo_ref"]), 40)
            self.assertEqual(meta["tools_list_sha256"], m["tools_list_sha256"])
            self.assertEqual(meta["tools_count"], 4)
            self.assertEqual(meta["tool_descriptions_check"], "recorded_only")
            self.assertTrue(meta["fault"]["ofrep_confirmed"])
            self.assertEqual(meta["fault"]["signal_traces_seen"], 5)
            self.assertEqual(meta["preflight"]["fixture_leak_under_fault"], "PASS")
            self.assertEqual(meta["max_budget_usd"], 2.0)
            self.assertEqual(meta["mcp_config_sha256"], bench.sha256_file(os.path.join(trial, "mcp.json")))
            self.assertEqual(load(os.path.join(trial, "mcp.json")),
                             {"mcpServers": {"jaeger": {"type": "http", "url": "http://127.0.0.1:%s/jaeger/ui/api/ai/mcp/" % self.port}}})
            self.assertEqual(read(os.path.join(trial, "exit.txt")), "exit=0\n")
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
        g = graders[0]["argv"]
        for flag in ("--strict-mcp-config", "--restricted", "--no-session-persistence"):
            self.assertIn(flag, g)
        self.assertEqual([g[g.index(k) + 1] for k in ("--tools", "--setting-sources", "--model", "--system-prompt")],
                         ["", "", "claude-fable-5-1", text("grader-prompt.txt")])
        self.assertFalse(graders[0]["claudecode"] or graders[0]["api_key"])
        self.assertNotEqual(graders[0]["cwd"], bench.ROOT)
        self.assertEqual(m["grader"], {"client": "cli", "model": "claude-fable-5-1", "cli_version": "2.1.282 (Claude Code)",
                                       "prompt_sha256": bench.sha256_file(os.path.join(HARNESS_DIR, "grader-prompt.txt"))})
        self.assertEqual(sorted((s["mechanism_grade"], s["grader_cached"]) for s in scores), [("correct", False), ("correct", True)])
        self.assertEqual(m["preflight"]["fixture_leak_under_fault"], "PASS")
        r = load(os.path.join(b, "restore.json"))
        self.assertTrue(r["cp_ok"] and r["default_confirmed"])
        self.assertEqual(self.flag_file(), self.pristine())
        blog = read(os.path.join(b, "batch.log"))
        self.assertIn("band paymentFailure/noskill: n=1 passes=1", blog)
        self.assertIn("pre-flight PASS", blog)
        self.assertEqual(m["scenario_version"], 2)
        self.assertEqual(m["preflight"]["oracle"], "PASS")
        self.assertIn("noskill: n=1 PASS=1 PARTIAL=0 FAIL=0 ABSTAIN=0 ERROR=0 INVALID=0 LEAK=0 stops=- ", blog)
        # One stream: everything a human reads is on stderr, and batch.log carries every screen line too.
        self.assertEqual(self.out, "")
        for line in ("ok    paymentFailure ready", "[1/2] ", "tools used: get_trace_errors 1", "Records "):
            self.assertIn(line, self.err)
            self.assertIn(line, blog)
        # The results table: a header, then one row per arm.
        lines = self.err.splitlines()
        head = next(i for i, l in enumerate(lines) if l.split()[:2] == ["arm", "pass"])
        self.assertEqual([l.split()[:2] for l in lines[head + 1:] if l.startswith("  ") and not l.startswith("    ")][:2],
                         [["noskill", "1/1"], ["skill", "1/1"]])
        self.assertEqual(sum(1 for l in lines if l.lstrip().startswith(("noskill ", "skill ")) and "/1 " in l), 2)

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
        self.assertEqual(claude_client.observe(p)["model"], "m")

    def test_client_version_mismatch_refuses_before_the_fixture(self):
        self.refused(lambda d: d["run"].update(client_version="2.1.283"),
                     "run.client_version is 2.1.283 but the cli client reports 2.1.282")
        self.assertIn("fix: remove run.client_version from the experiment file to run on your CLI, or install 2.1.283 for an exact re-run", self.err)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.flag_file(), self.pristine())

    def test_client_version_match_runs_and_is_recorded(self):
        self.assertEqual(self.run_batch(1, run={"client_version": "2.1.282"}), 0, self.err)
        m = load(os.path.join(self.batch_dir(), "manifest.json"))
        self.assertEqual(m["preflight"]["client_version"], "PASS")
        self.assertEqual(m["experiment"]["content"]["run"]["client_version"], "2.1.282")
        self.assertEqual(load(os.path.join(self.batch_dir(), "preflight.json"))["client_version"], "PASS")

    def test_export_posts_one_trace_per_trial_with_run_attributes(self):
        self.assertEqual(self.run_batch(1, "--no-export"), 0, self.err)
        b = self.batch_dir()
        self.assertEqual(self.bench("export", b, PHOENIX_PORT=self.phoenix_port), 0, self.out)  # default endpoint from fixture.env
        self.assertEqual(self.bench("export", b, "--endpoint", "http://127.0.0.1:%s/" % self.phoenix_port), 0, self.out)
        (ctype, body), (_, again) = Fake.otlp_posts
        self.assertEqual(ctype, "application/x-protobuf")
        self.assertEqual(body, again)  # deterministic: a re-export sends the same trace and span ids
        resource, spans = self.spans(body)
        self.assertEqual(resource["openinference.project.name"], "t")
        kinds = [s["attrs"]["openinference.span.kind"] for s in spans]
        self.assertEqual(sorted(kinds), ["AGENT", "AGENT", "LLM", "LLM", "TOOL", "TOOL"])
        batch_id = os.path.basename(b)
        for root in (s for s in spans if s["attrs"]["openinference.span.kind"] == "AGENT"):
            a = root["attrs"]
            self.assertEqual({k: a[k] for k in ("experiment", "batch_id", "model", "client", "provider", "verdict", "trial_index")},
                             {"experiment": "t", "batch_id": batch_id, "model": "claude-sonnet-5", "client": "cli",
                              "provider": "anthropic", "verdict": "PASS", "trial_index": 0})
            trial = [d for d in os.listdir(b) if d.endswith("-" + a["arm"])][0]
            tid = root[1][0]
            self.assertEqual(tid.hex(), bench.hashlib.sha256(("paymentFailure/%s/%s" % (batch_id, trial)).encode()).hexdigest()[:32])
            children = [s for s in spans if s[1][0] == tid and s is not root]
            self.assertEqual(len(children), 2)
            self.assertTrue(all(s[4][0] == root[2][0] for s in children))  # parent is the AGENT span
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

    def write_recorded(self, rows):
        os.makedirs(os.path.join(self.records, "old", "batch-1"))
        with open(os.path.join(self.records, "old", "batch-1", "manifest.json"), "w") as f:  # as every record has
            json.dump({"client": "cli"}, f)
        with open(os.path.join(self.records, "old", "batch-1", "scores.jsonl"), "w") as f:
            f.writelines(json.dumps(dict({"scenario_used": "paymentFailure", "model_asserted": "sonnet"}, **r)) + "\n" for r in rows)

    def test_stage_one_names_the_cli_version_and_says_when_cost_is_unknown(self):
        self.assertEqual(self.run_batch(1, "--dry-run"), 0, self.err)
        self.assertIn("ok    claude CLI 2.1.282 accepts the flags the harness passes", self.err)
        self.assertIn("ok    expected cost unknown: no recorded cli trial of paymentFailure on sonnet", self.err)

    def test_expected_cost_is_per_arm_median_times_n(self):
        self.write_recorded([{"cost_usd": x} for x in (0.1, 0.2, 0.3)])
        self.assertEqual(bench.expected_cost("paymentFailure", "sonnet", 10, 2, records=self.records),
                         [(True, "expected cost about 2.00 USD per arm of 10 trials (recorded max 0.30 per trial, cap 2)")])
        # recorded cli trials say nothing about a goose run on the same model id
        self.assertEqual(bench.expected_cost("paymentFailure", "sonnet", 10, 2, records=self.records, client="goose"),
                         [(True, "expected cost unknown: no recorded goose trial of paymentFailure on sonnet")])
        os.remove(os.path.join(self.records, "old", "batch-1", "manifest.json"))  # no manifest: no known client, not read
        self.assertEqual(bench.expected_cost("paymentFailure", "sonnet", 10, 2, records=self.records),
                         [(True, "expected cost unknown: no recorded cli trial of paymentFailure on sonnet")])

    def test_max_budget_usd_is_required_for_the_cli(self):
        self.refused(lambda d: d["run"].pop("max_budget_usd"), "run.max_budget_usd must be a positive number")
        self.write_recorded([{"cost_usd": 0.3}])
        with open(os.path.join(self.records, "old", "batch-1", "manifest.json"), "w") as f:
            json.dump({"client": "goose"}, f)
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
        with open(os.path.join(self.records, "old", "batch-1", "manifest.json"), "w") as f:
            json.dump({"client": "goose"}, f)
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
