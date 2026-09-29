#!/usr/bin/env python3
"""Unit tests for harness/judge.py (threshold judging from the experiment file in each manifest).

Run with: python3 -m unittest discover harness/tests
Python 3 stdlib only, no network, no LLM, no ssh, no docker.
"""
import json
import os
import shutil
import sys
import tempfile
import contextlib
import io
import unittest
from unittest import mock

HARNESS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, HARNESS_DIR)
import bench  # noqa: E402
import judge as judge_mod  # noqa: E402


EXPERIMENT = {
    "name": "desc-change-sep28",
    "version": 2,
    "scenario": "paymentFailure",
    "hypothesis": "test hypothesis",
    "run": {"client": "cli", "provider": None, "model": "sonnet", "effort": "xhigh", "max_turns": 30,
            "max_budget_usd": 2, "n_per_arm": 10, "seed": None},
    "arms": {
        "baseline": {"prompt": "noskill", "image": "quay.io/jaegertracing/jaeger:2.20.0", "tools": True},
        "descchange": {"prompt": "noskill", "image": "jaeger-mcp-evals/jaeger:desc-change-abc1234", "tools": True},
    },
    "baseline_arm": "baseline",
    "thresholds": {
        "tool_used_min": {"get_trace_topology": 8},
        "tool_used_max": {"get_critical_path": 2},
        "tool_output_chars_median_drop_pct_min": 10,
        "accuracy_pass_min": 10,
    },
    "status": "CONFIRMED: test",
}


def write_manifest(batch_dir, arm_pin=None, sha256="deadbeef"):
    manifest = {"scenario": "paymentFailure", "arms": [arm_pin] if arm_pin else ["baseline", "descchange"],
                "experiment": {"name": EXPERIMENT["name"], "file": "x.json", "sha256": sha256, "content": EXPERIMENT}}
    if arm_pin is not None:
        manifest["arm_pin"] = {"arm": arm_pin, "expected_image": "irrelevant-for-tests"}
    with open(os.path.join(batch_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f)


def make_row(arm, call_sequence, tool_output_chars, verdict):
    return {
        "arm": arm,
        "call_sequence": call_sequence,
        "tool_output_chars": tool_output_chars,
        "verdict": verdict,
    }


def write_scores(batch_dir, rows):
    with open(os.path.join(batch_dir, "scores.jsonl"), "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def baseline_rows(n=10, chars=2000):
    return [make_row("baseline", ["get_critical_path"], chars, "PASS") for _ in range(n)]


def descchange_rows_all_thresholds_met(chars=1700):
    # 8/10 use get_trace_topology, 2/10 use get_critical_path, all PASS.
    rows = [make_row("descchange", ["get_trace_topology"], chars, "PASS") for _ in range(8)]
    rows += [make_row("descchange", ["get_critical_path"], chars, "PASS") for _ in range(2)]
    return rows


class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.baseline_dir = os.path.join(self.tmp, "baseline-batch")
        self.variant_dir = os.path.join(self.tmp, "variant-batch")
        os.makedirs(self.baseline_dir)
        os.makedirs(self.variant_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_all_thresholds_met_passes(self):
        # median chars drop from 2000 to 1700 (15% >= 10% required), 8/10
        # get_trace_topology, 2/10 get_critical_path, all 10 PASS accuracy.
        write_manifest(self.baseline_dir, arm_pin="baseline")
        write_scores(self.baseline_dir, baseline_rows())
        write_manifest(self.variant_dir, arm_pin="descchange")
        write_scores(self.variant_dir, descchange_rows_all_thresholds_met())

        lines, code = judge_mod.judge(self.baseline_dir, self.variant_dir)
        self.assertEqual(code, 0)
        self.assertEqual(lines[-1], "EXPERIMENT PASS")
        self.assertTrue(all("FAIL" not in line for line in lines))

    def test_output_text_is_exact(self):
        for chars, lines_want, code_want in (
            (2000, ["tool_used_min[get_trace_topology]: PASS (measured=8, required>=8)",
                    "tool_used_max[get_critical_path]: PASS (measured=2, required<=2)",
                    "tool_output_chars_median_drop_pct_min: PASS (measured=15.0, required>=10)",
                    "accuracy_pass_min: PASS (measured=10, required>=10)",
                    "EXPERIMENT PASS"], 0),
            (0, ["tool_used_min[get_trace_topology]: PASS (measured=8, required>=8)",
                 "tool_used_max[get_critical_path]: PASS (measured=2, required<=2)",
                 "tool_output_chars_median_drop_pct_min: FAIL (measured=N/A (no baseline data), required>=10)",
                 "accuracy_pass_min: PASS (measured=10, required>=10)",
                 "EXPERIMENT FAIL"], 1)):
            write_manifest(self.baseline_dir, arm_pin="baseline")
            write_scores(self.baseline_dir, baseline_rows(chars=chars))
            write_manifest(self.variant_dir, arm_pin="descchange")
            write_scores(self.variant_dir, descchange_rows_all_thresholds_met())
            self.assertEqual(judge_mod.judge(self.baseline_dir, self.variant_dir), (lines_want, code_want))

    def test_topology_below_threshold_fails_that_line(self):
        # Only 7/10 descchange runs use get_trace_topology (threshold needs 8).
        rows = [make_row("descchange", ["get_trace_topology"], 1700, "PASS") for _ in range(7)]
        rows += [make_row("descchange", [], 1700, "PASS") for _ in range(3)]
        write_manifest(self.baseline_dir, arm_pin="baseline")
        write_scores(self.baseline_dir, baseline_rows())
        write_manifest(self.variant_dir, arm_pin="descchange")
        write_scores(self.variant_dir, rows)

        lines, code = judge_mod.judge(self.baseline_dir, self.variant_dir)
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1], "EXPERIMENT FAIL")
        topology_lines = [l for l in lines if l.startswith("tool_used_min[get_trace_topology]")]
        self.assertEqual(len(topology_lines), 1)
        self.assertIn("FAIL", topology_lines[0])
        self.assertIn("measured=7", topology_lines[0])
        # the other thresholds still pass individually
        self.assertTrue(any(l.startswith("tool_used_max") and "PASS" in l for l in lines))
        self.assertTrue(any(l.startswith("accuracy_pass_min") and "PASS" in l for l in lines))

    def main(self):
        out = io.StringIO()
        argv = ["judge.py", self.baseline_dir, self.variant_dir]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(bench, "RECORDS", os.path.join(self.tmp, "records")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as e:
            judge_mod.main()
        return e.exception.code, out.getvalue()

    def test_main_writes_result_md_and_index(self):
        write_manifest(self.baseline_dir, arm_pin="baseline")
        write_scores(self.baseline_dir, baseline_rows())
        write_manifest(self.variant_dir, arm_pin="descchange")
        write_scores(self.variant_dir, descchange_rows_all_thresholds_met())
        code, out = self.main()
        self.assertEqual(code, 0)
        path = os.path.join(self.tmp, "records", EXPERIMENT["name"], "RESULT.md")
        with open(path) as f:
            result = f.read()
        self.assertIn("- experiment sha256: `deadbeef`", result)
        self.assertIn("- baseline batch: `baseline-batch`", result)
        self.assertIn("- variant batch: `variant-batch`", result)
        self.assertIn("```\n" + out + "```\n", result)  # the screen lines, verbatim
        with open(os.path.join(self.tmp, "records", "INDEX.md")) as f:
            self.assertIn("| desc-change-sep28 | EXPERIMENT PASS | [RESULT.md](desc-change-sep28/RESULT.md) |", f.read())
        # a re-run overwrites: RESULT.md is the current answer
        write_scores(self.variant_dir, [make_row("descchange", [], 1700, "FAIL") for _ in range(10)])
        self.assertEqual(self.main()[0], 1)
        with open(path) as f:
            self.assertEqual(f.read().count("EXPERIMENT FAIL"), 1)

    def test_refusal_writes_nothing(self):
        write_manifest(self.baseline_dir, arm_pin="baseline", sha256="aaa")
        write_manifest(self.variant_dir, arm_pin="descchange", sha256="bbb")
        self.assertEqual(self.main()[0], 2)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "records")))

    def test_no_variant_rows_fails_the_chars_threshold(self):
        write_manifest(self.baseline_dir, arm_pin="baseline")
        write_scores(self.baseline_dir, baseline_rows())
        write_manifest(self.variant_dir, arm_pin="descchange")
        write_scores(self.variant_dir, [])
        lines, code = judge_mod.judge(self.baseline_dir, self.variant_dir)
        self.assertEqual(code, 1)
        self.assertIn("tool_output_chars_median_drop_pct_min: FAIL (measured=N/A (no variant data), required>=10)", lines)
        self.assertEqual(lines[-1], "EXPERIMENT FAIL")

    def test_mismatched_experiment_sha256_refuses(self):
        # Same thresholds, different sha256 - as if the experiment file were
        # edited between the baseline run and the variant run.
        write_manifest(self.baseline_dir, arm_pin="baseline", sha256="aaaa")
        write_scores(self.baseline_dir, baseline_rows())
        write_manifest(self.variant_dir, arm_pin="descchange", sha256="bbbb")
        write_scores(self.variant_dir, descchange_rows_all_thresholds_met())

        lines, code = judge_mod.judge(self.baseline_dir, self.variant_dir)
        self.assertEqual(code, 2)
        self.assertIn("different", lines[0])
        self.assertIn("refusing to compare", lines[0])

    def test_swapped_batch_dirs_refuses(self):
        # baseline_batch_dir is actually pinned to the variant arm, and vice
        # versa - the two arguments were swapped by the caller.
        write_manifest(self.baseline_dir, arm_pin="descchange")
        write_scores(self.baseline_dir, descchange_rows_all_thresholds_met())
        write_manifest(self.variant_dir, arm_pin="baseline")
        write_scores(self.variant_dir, baseline_rows())

        lines, code = judge_mod.judge(self.baseline_dir, self.variant_dir)
        self.assertEqual(code, 2)
        self.assertIn("wrong batch directory", lines[0])

    def _cross_pair(self, variant_edit=None, noimg=None, **kw):
        write_manifest(self.baseline_dir, arm_pin="baseline", sha256="aaa")
        write_scores(self.baseline_dir, baseline_rows())
        write_manifest(self.variant_dir, arm_pin="descchange", sha256="bbb")
        if variant_edit:
            mp = os.path.join(self.variant_dir, "manifest.json")
            m = json.load(open(mp))
            variant_edit(m)
            json.dump(m, open(mp, "w"))
        write_scores(self.variant_dir, descchange_rows_all_thresholds_met())
        for d in (self.baseline_dir, self.variant_dir):  # shared fields the cross check reads
            mp = os.path.join(d, "manifest.json")
            m = json.load(open(mp))
            for k, v in {"model_requested": "sonnet", "n_per_arm": 10, "scenario_sha256": "s1"}.items():
                m.setdefault(k, v)
            m.setdefault("file_hashes_sha256", {"prompts/noskill.txt": "p1"})
            if noimg:
                m["fixture_overlay_sha256_sans_image"] = noimg[d == self.variant_dir]
            json.dump(m, open(mp, "w"))
        return judge_mod.judge(self.baseline_dir, self.variant_dir, **kw)

    def test_cross_experiment_matching_pair_accepted(self):
        lines, code = self._cross_pair(noimg=("o1", "o1"))
        self.assertEqual(code, 0)
        self.assertIn("CROSS-EXPERIMENT", lines[0])
        self.assertIn("model_requested", lines[0])

    def test_cross_overlay_sans_image_differs_refused(self):
        lines, code = self._cross_pair(noimg=("o1", "o2"))
        self.assertEqual(code, 2)
        self.assertIn("fixture_overlay_sha256_sans_image ('o1' vs 'o2')", lines[0])

    def test_cross_missing_overlay_field_refused_without_flag(self):
        lines, code = self._cross_pair()
        self.assertEqual(code, 2)
        self.assertIn("--overlay-checked", lines[0])

    def test_cross_missing_overlay_field_accepted_with_flag_and_lands_in_result(self):
        lines, code = self._cross_pair(overlay_checked="reproduced by hand")
        self.assertEqual(code, 0)
        self.assertIn("fixture overlay compared by hand, not by manifest: reproduced by hand", lines)
        self.assertNotIn("fixture_overlay_sha256", lines[0])
        rec = os.path.join(self.tmp, "records")
        with mock.patch.object(bench, "write_index"):
            path = judge_mod.write_result(lines, self.baseline_dir, self.variant_dir, records=rec)
        self.assertIn("compared by hand, not by manifest: reproduced by hand", open(path).read())

    def test_cross_experiment_model_difference_refused(self):
        lines, code = self._cross_pair(lambda m: m.update(model_requested="opus"), noimg=("o1", "o1"))
        self.assertEqual(code, 2)
        self.assertIn("model_requested ('sonnet' vs 'opus')", lines[0])


if __name__ == "__main__":
    unittest.main()
