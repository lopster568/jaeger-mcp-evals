#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""harness/codex_client.py, the OpenAI Codex CLI adapter (run.client codex, experimental), through `bench.py run`
against the fake fixture of test_bench_batch.py and on its own: the stream it writes, max_turns, the verdict schema,
no sandbox probe, and no provider in an export.

Run with: python3 -m unittest discover -s harness/tests
"""
import json
import os
import pathlib
import subprocess
import sys
import unittest

import test_bench_batch as tbb
from test_bench_batch import HARNESS_DIR, RUN, BenchCase, Fake, load, text
import bench  # noqa: E402  (test_bench_batch put harness/ on sys.path)
import codex_client  # noqa: E402


class TestCodexBatch(BenchCase):
    RUN = dict(RUN, client="codex", provider=None, model="gpt-t")
    spans = tbb.TestExport.spans

    def test_codex_client_scores_through_the_stream(self):
        self.assertEqual(self.run_batch(1, "--dry-run"), 0, self.err)
        self.assertIn("codex gpt-t xhigh", self.err)
        self.assertIn("codex_client.py", self.err)
        self.assertEqual(self.run_batch(1, OPENAI_API_KEY="sk-o", ANTHROPIC_API_KEY="sk-a"), 0, self.err)
        b = self.batch_dir()
        self.assertEqual([s["verdict"] for s in bench.read_jsonl(os.path.join(b, "scores.jsonl"))], ["PASS", "PASS"])
        m = load(os.path.join(b, "manifest.json"))
        self.assertIn("codex_client.py", m["file_hashes_sha256"])
        calls = bench.read_jsonl(os.path.join(self.home, "codex-calls.jsonl"))
        self.assertEqual(len(calls), 2)
        url = "http://127.0.0.1:%s/jaeger/ui/api/ai/mcp/" % self.port
        for c in bench.read_jsonl(os.path.join(b, "cells.jsonl")):
            trial = os.path.join(b, "%d-%s" % (c["order_index"], c["arm"]))
            meta = load(os.path.join(trial, "meta.json"))
            self.assertEqual((meta["client"], meta["model_requested"], meta["client_version_pre"]), ("codex", "gpt-t", "codex-cli 0.153.2"))
            self.assertEqual(meta["history_policy"], {"harness_truncation": "none", "cli_autocompact": "unknown"})
            self.assertEqual(meta["client_argv"][:2], [os.path.basename(sys.executable), "harness/codex_client.py"])
            self.assertFalse([x for x in meta["client_argv"] if os.path.isabs(x)])
            init = bench.read_jsonl(os.path.join(trial, "stream.jsonl"))[0]
            endpoint = ":%s/jaeger/ui/api/ai/mcp/" % self.port
            self.assertEqual(init["mcp_servers"], [{"name": "jaeger", "url": endpoint}])
            self.assertEqual(init["codex_bin"], "codex")
            self.assertIn('mcp_servers.jaeger.url="%s"' % endpoint, init["codex_argv"])
            self.assertEqual(init["codex_argv"][init["codex_argv"].index("--output-schema") + 1], "harness/verdict-schema.json")
            self.assertNotIn("127.0.0.1", json.dumps(init))
            self.assertEqual(meta["observed"]["client_version"], "codex-cli 0.153.2")
            s, _ = bench.score.score(trial)
            self.assertEqual((s["tool_calls"], s["steps_to_evidence"], s["stop"], s["input_tokens_total"]),
                             (1, 1, "success", 100))
            stream = bench.read_jsonl(os.path.join(trial, "stream.jsonl"))
            tool_use_names = [b["name"] for e in stream if e.get("type") == "assistant"
                               for b in e["message"].get("content", []) if b.get("type") == "tool_use"]
            self.assertNotIn("error", tool_use_names)
            self.assertIn({"type": "system", "subtype": "codex_error", "message": "Model metadata for gpt-t not found"}, stream)
        argv = calls[0]["argv"]
        self.assertEqual(argv[argv.index("--output-schema") + 1], bench.VERDICT_SCHEMA)
        self.assertIn('mcp_servers.jaeger.url="%s"' % url, argv)
        self.assertIn('mcp_servers.jaeger.default_tools_approval_mode="approve"', argv)
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-t")
        self.assertIn(text("system-prompt.txt"), calls[0]["stdin"])
        self.assertFalse(calls[0]["api_key"])
        exec_i = argv.index("exec")
        self.assertNotIn("-c", argv[exec_i + 1:])
        c_indices = [i for i, v in enumerate(argv) if v == "-c"]
        self.assertTrue(c_indices and all(i < exec_i for i in c_indices))

    def test_codex_client_stops_at_max_turns(self):
        out = os.path.join(self.home, "trial")
        os.makedirs(out)
        mcp = os.path.join(out, "mcp.json")
        pathlib.Path(mcp).write_text('{"mcpServers": {}}')
        r = subprocess.run([sys.executable, bench.CODEX_CLIENT, "--model", "m", "--effort", "high", "--max-turns", "1",
                            "--prompt-file", os.path.join(HARNESS_DIR, "prompts", "neutral.txt"),
                            "--system-prompt-file", bench.SYSTEM_PROMPT, "--schema-file", bench.VERDICT_SCHEMA,
                            "--mcp-config", mcp, "--out-dir", out], env=dict(os.environ, **self.env), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        final = bench.read_jsonl(os.path.join(out, "stream.jsonl"))[-1]
        self.assertEqual((final["subtype"], final["num_tool_calls"], final["structured_output"]), ("error_max_turns", 1, None))
        self.assertNotIn("mcp_servers", " ".join(bench.read_jsonl(os.path.join(self.home, "codex-calls.jsonl"))[0]["argv"]))

    def test_codex_answer_must_match_the_verdict_schema(self):
        verdict = {"root_cause_service": "payment", "root_cause_operation": "charge", "mechanism": "invalid token", "cascading": [], "confidence": "high", "evidence_span_ids": [], "abstain": False}
        schema = load(bench.VERDICT_SCHEMA)
        self.assertEqual(codex_client.extract_json("```json\n%s\n```" % json.dumps(verdict), schema), verdict)
        self.assertIsNone(codex_client.extract_json(json.dumps(dict(verdict, confidence="certain")), schema))
        self.assertIsNone(codex_client.extract_json("not json", schema))

    def test_codex_needs_codex_code_mode_host_beside_it(self):
        os.remove(os.path.join(self.binp, "codex-code-mode-host"))
        path = self.binp + os.pathsep + "/usr/bin" + os.pathsep + "/bin"  # no host on the real PATH either
        self.assertEqual(self.run_batch(1, "--dry-run", PATH=path), 1)
        self.assertIn("codex-code-mode-host is not installed beside %s" % self.binp, self.err)
        self.assertIn("install codex-code-mode-host from the same Codex release as codex", self.err)
        self.assertFalse(os.path.exists(self.runs))

    def test_codex_is_not_probed(self):
        self.assertEqual(self.run_batch(1), 0, self.err)
        self.assertIsNone(load(os.path.join(self.batch_dir(), "preflight.json"))["sandbox_probe"])
        self.assertFalse(os.path.exists(os.path.join(self.home, "probe-calls.jsonl")))

    def test_export_sets_no_provider_for_codex(self):
        self.assertEqual(self.run_batch(1, "--no-export"), 0, self.err)
        b = self.batch_dir()  # a codex trial records provider null
        self.assertEqual(self.bench("export", b, PHOENIX_PORT=self.phoenix_port), 0, self.out)
        _, spans = self.spans(Fake.otlp_posts[0][1])
        self.assertFalse([s for s in spans if "provider" in s["attrs"] or "llm.provider" in s["attrs"]])


if __name__ == "__main__":
    unittest.main()
