#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

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
        "baseline": {"prompt": "neutral", "image": "quay.io/jaegertracing/jaeger:2.20.0", "tools": True},
        "descchange": {"prompt": "neutral", "image": "jaeger-mcp-evals/jaeger:desc-change-abc1234", "tools": True},
    },
    "baseline_arm": "baseline",
    "thresholds": {
        "accuracy_pass_min": 10,
    },
    "status": "CONFIRMED: test",
}


def write_manifest(batch_dir, arm_pin=None, sha256="deadbeef", oracle=None):
    jaeger = {"baseline": ("jaeger:base", "aaaa111"), "descchange": ("jaeger:change", "bbbb222")}.get(arm_pin, ("jaeger:x", None))
    manifest = {"jaeger_image": jaeger[0], "jaeger_commit": jaeger[1], "scenario": "paymentFailure", "scenario_sha256": "s1", "arms": [arm_pin] if arm_pin else ["baseline", "descchange"],
                "experiment": {"name": EXPERIMENT["name"], "file": "x.json", "sha256": sha256, "content": EXPERIMENT}}
    if oracle:
        manifest["preflight"] = {"oracle": oracle}
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
        # all 10 PASS accuracy.
        write_manifest(self.baseline_dir, arm_pin="baseline")
        write_scores(self.baseline_dir, baseline_rows())
        write_manifest(self.variant_dir, arm_pin="descchange")
        write_scores(self.variant_dir, descchange_rows_all_thresholds_met())

        lines, code = judge_mod.judge(self.baseline_dir, self.variant_dir)
        self.assertEqual(code, 0)
        self.assertEqual(lines[-1], "EXPERIMENT PASS")
        self.assertTrue(all("FAIL" not in line for line in lines))

    def test_output_text_is_exact(self):
        write_manifest(self.baseline_dir, arm_pin="baseline")
        write_scores(self.baseline_dir, baseline_rows())
        write_manifest(self.variant_dir, arm_pin="descchange")
        write_scores(self.variant_dir, descchange_rows_all_thresholds_met())
        self.assertEqual(judge_mod.judge(self.baseline_dir, self.variant_dir),
                         (["baseline arm baseline: 10/10 PASS", "arm descchange: 10/10 PASS",
                           "accuracy_pass_min: PASS (measured=10, required>=10)", "EXPERIMENT PASS"], 0))

    def main(self):
        out = io.StringIO()
        argv = ["judge.py", self.baseline_dir, self.variant_dir]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(bench, "RECORDS", os.path.join(self.tmp, "records")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as e:
            judge_mod.main()
        return e.exception.code, out.getvalue()

    def test_result_md_notes_an_oracle_fail_only(self):
        path = os.path.join(self.tmp, "records", EXPERIMENT["name"], "RESULT.md")
        for base, var, want in (("PASS", "PASS", []), ("FAIL", "PASS", ["baseline"]), ("PASS", "FAIL", ["variant"])):
            write_manifest(self.baseline_dir, arm_pin="baseline", oracle=base)
            write_scores(self.baseline_dir, baseline_rows())
            write_manifest(self.variant_dir, arm_pin="descchange", oracle=var)
            write_scores(self.variant_dir, descchange_rows_all_thresholds_met())
            code, out = self.main()
            self.assertEqual(code, 0)  # the verdict is unchanged
            with open(path) as f:
                result = f.read()
            notes = [l for l in result.splitlines() if l.startswith("Note: the oracle failed")]
            self.assertEqual(notes, ["Note: the oracle failed in the %s batch; trials ran on the signal wait alone." % w for w in want])

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
        self.assertIn("- baseline Jaeger: jaeger:base (commit aaaa111)", result)
        self.assertIn("- variant Jaeger: jaeger:change (commit bbbb222)", result)
        self.assertIn("```\n" + out + "```\n", result)  # the screen lines, verbatim
        self.assertIn("\nbaseline arm baseline: 10/10 PASS\narm descchange: 10/10 PASS\n", result)
        with open(os.path.join(self.tmp, "records", "INDEX.md")) as f:
            self.assertIn("| desc-change-sep28 | EXPERIMENT PASS | [RESULT.md](desc-change-sep28/RESULT.md) |", f.read())
        # a re-run overwrites: RESULT.md is the current answer
        write_scores(self.variant_dir, [make_row("descchange", [], 1700, "FAIL") for _ in range(10)])
        self.assertEqual(self.main()[0], 1)
        with open(path) as f:
            self.assertEqual(f.read().count("EXPERIMENT FAIL"), 1)

    def fisher_pair(self, thresholds, base_pass, var_pass, n=10):
        """Both batches written with an experiment carrying only these thresholds."""
        exp = dict(EXPERIMENT, thresholds=thresholds)
        for d, arm, k in ((self.baseline_dir, "baseline", base_pass), (self.variant_dir, "descchange", var_pass)):
            m = {"scenario": "paymentFailure", "scenario_sha256": "s1", "experiment": {"name": exp["name"], "file": "x.json", "sha256": "ab", "content": exp},
                 "arm_pin": {"arm": arm, "expected_image": "x"}}
            with open(os.path.join(d, "manifest.json"), "w") as f:
                json.dump(m, f)
            write_scores(d, [make_row(arm, [], 1, "PASS" if i < k else "FAIL") for i in range(n)])

    def test_fisher_p_max_pass_and_fail(self):
        self.fisher_pair({"fisher_p_max": 0.05}, 0, 7)  # p = 0.0031
        lines, code = judge_mod.judge(self.baseline_dir, self.variant_dir)
        self.assertEqual(code, 0)
        self.assertIn("fisher_p_max: PASS (measured=%s, required<0.05)" % round(bench.fisher_p(0, 7, 10), 4), lines)
        self.fisher_pair({"fisher_p_max": 0.05}, 3, 6)  # p = 0.37
        lines, code = judge_mod.judge(self.baseline_dir, self.variant_dir)
        self.assertEqual(code, 1)
        self.assertIn("fisher_p_max: FAIL (measured=%s, required<0.05)" % round(bench.fisher_p(3, 6, 10), 4), lines)

    def test_direction_line(self):
        for base, var, want in ((3, 9, "direction: variant 9/10 vs baseline 3/10: better"),
                                (9, 3, "direction: variant 3/10 vs baseline 9/10: worse"),
                                (5, 6, "direction: variant 6/10 vs baseline 5/10: no significant difference at n=10")):
            self.fisher_pair({"fisher_p_max": 0.05}, base, var)
            lines, code = judge_mod.judge(self.baseline_dir, self.variant_dir)
            self.assertIn(want, lines)
            self.assertEqual(lines[-1], "EXPERIMENT %s" % ("FAIL" if "no significant" in want else "PASS"))  # the rule stays two-sided
            self.assertEqual(code, 1 if "no significant" in want else 0)
            self.assertEqual(lines.index(want), len(lines) - 2)  # right before the verdict line

    def test_empty_thresholds_refuse(self):
        self.fisher_pair({}, 0, 10)
        lines, code = judge_mod.judge(self.baseline_dir, self.variant_dir)
        self.assertEqual(code, 2)
        self.assertIn("nothing was judged", lines[0])

    def test_refusal_writes_nothing(self):
        write_manifest(self.baseline_dir, arm_pin="baseline", sha256="aaa")
        write_manifest(self.variant_dir, arm_pin="descchange", sha256="bbb")
        self.assertEqual(self.main()[0], 2)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "records")))

    def test_no_variant_rows_fails_the_accuracy_threshold(self):
        write_manifest(self.baseline_dir, arm_pin="baseline")
        write_scores(self.baseline_dir, baseline_rows())
        write_manifest(self.variant_dir, arm_pin="descchange")
        write_scores(self.variant_dir, [])
        lines, code = judge_mod.judge(self.baseline_dir, self.variant_dir)
        self.assertEqual(code, 1)
        self.assertIn("accuracy_pass_min: FAIL (measured=0, required>=10)", lines)
        self.assertEqual(lines[-1], "EXPERIMENT FAIL")

    def _versioned(self, base, var, manifest_pre=None, row_edit=None):
        """A matching pair whose rows carry client_version base / var (None: not recorded)."""
        for d, arm, v, rows in ((self.baseline_dir, "baseline", base, baseline_rows()),
                                (self.variant_dir, "descchange", var, descchange_rows_all_thresholds_met())):
            write_manifest(d, arm_pin=arm)
            if manifest_pre:
                m = json.load(open(os.path.join(d, "manifest.json")))
                json.dump(dict(m, client_version_pre=manifest_pre), open(os.path.join(d, "manifest.json"), "w"))
            write_scores(d, [dict(r, client_version=v) if v else r for r in rows])
        self.err = io.StringIO()
        with contextlib.redirect_stderr(self.err):
            return judge_mod.judge(self.baseline_dir, self.variant_dir)

    def test_same_client_version_judges_as_before(self):
        lines, code = self._versioned("2.1.285", "2.1.285")
        self.assertEqual((code, lines[-1]), (0, "EXPERIMENT PASS"))
        self.assertEqual(self.err.getvalue(), "")

    def test_client_version_differing_between_arms_refuses_naming_both(self):
        lines, code = self._versioned("2.1.285", "2.1.326")
        self.assertEqual(code, 2)
        self.assertEqual(lines, ["judge: refusing to compare: the client version differs (baseline arm ran on 2.1.285; "
                                 "variant arm ran on 2.1.326); keep each pair on one client"])

    def test_row_and_manifest_versions_go_through_one_parse(self):
        rows = [{}, {"client_version": "codex-cli 0.153.2"}]  # a row without its own version falls back to the manifest
        self.assertEqual(judge_mod._versions({"client_version_pre": "codex-cli 0.153.2"}, rows), ["0.153.2", "0.153.2"])

    def test_client_version_differing_within_an_arm_refuses(self):
        write_manifest(self.baseline_dir, arm_pin="baseline")
        write_scores(self.baseline_dir, [dict(r, client_version="2.1.285" if i else "2.1.326") for i, r in enumerate(baseline_rows())])
        write_manifest(self.variant_dir, arm_pin="descchange")
        write_scores(self.variant_dir, [dict(r, client_version="2.1.285") for r in descchange_rows_all_thresholds_met()])
        lines, code = judge_mod.judge(self.baseline_dir, self.variant_dir)
        self.assertEqual(code, 2)
        self.assertIn("baseline arm ran on 2.1.285 and 2.1.326", lines[0])

    def test_unrecorded_version_does_not_refuse_and_is_said_once(self):
        lines, code = self._versioned(None, None)
        self.assertEqual((code, lines[-1]), (0, "EXPERIMENT PASS"))
        self.assertEqual(self.err.getvalue(), "judge: client version not recorded\n")

    def test_manifest_version_stands_in_for_rows_without_one(self):
        lines, code = self._versioned(None, "2.1.326", manifest_pre="2.1.285 (Claude Code)")
        self.assertEqual(code, 2)
        lines, code = self._versioned(None, None, manifest_pre="2.1.285 (Claude Code)")
        self.assertEqual((code, lines[-1]), (0, "EXPERIMENT PASS"))
        self.assertNotIn("judge: client version not recorded", lines)

    def _grounded(self, base_extra, var_extra):
        write_manifest(self.baseline_dir, arm_pin="baseline")
        write_scores(self.baseline_dir, [dict(r, dir="%d-baseline" % i, **base_extra(i)) for i, r in enumerate(baseline_rows())])
        write_manifest(self.variant_dir, arm_pin="descchange")
        write_scores(self.variant_dir, [dict(r, dir="%d-variant" % i, **var_extra(i))
                                        for i, r in enumerate(descchange_rows_all_thresholds_met())])
        return judge_mod.judge(self.baseline_dir, self.variant_dir)

    def test_grounding_fields_absent_prints_nothing(self):
        lines, code = self._grounded(lambda i: {}, lambda i: {})
        self.assertFalse([l for l in lines if l.startswith("grounding:")])

    def test_grounding_clean_says_zero(self):
        clean = lambda i: {"evidence_unseen": [], "tool_result_leak_hits": [], "export_marker_hits": []}
        lines, code = self._grounded(clean, clean)
        self.assertEqual((code, lines[-2], lines[-1]), (0, "grounding: 0 flagged PASS", "EXPERIMENT PASS"))

    def test_grounding_flagged_pass_is_counted_and_listed_verdict_unchanged(self):
        flag = lambda i: {"evidence_unseen": ["x"] if i == 3 else [], "tool_result_leak_hits": [], "export_marker_hits": []}
        flag_v = lambda i: {"evidence_unseen": [], "tool_result_leak_hits": [], "export_marker_hits": ["m"] if i < 2 else []}
        lines, code = self._grounded(flag, flag_v)
        self.assertEqual((code, lines[-1]), (0, "EXPERIMENT PASS"))
        self.assertEqual(lines[-2], "grounding: 3 flagged PASS (baseline 1, variant 2): read them before trusting this result: "
                                    "baseline 3-baseline; variant 0-variant; variant 1-variant")

    def test_grounding_ignores_flagged_rows_that_are_not_pass(self):
        lines, code = self._grounded(lambda i: {}, lambda i: {"evidence_unseen": ["x"] if i == 0 else [], "verdict": "FAIL"} if i == 0 else {"evidence_unseen": []})
        self.assertIn("grounding: 0 flagged PASS", lines)

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

    def test_same_experiment_refuses_when_the_scenario_or_system_prompt_differs(self):
        # the arms may differ in overlay and image, never in what the agent was asked
        for edit in ({"scenario_sha256": "s2"}, {"scenario_version": 2}, {"system_prompt_sha256": "x"}):
            write_manifest(self.baseline_dir, arm_pin="baseline")
            write_scores(self.baseline_dir, baseline_rows())
            write_manifest(self.variant_dir, arm_pin="descchange")
            mp = os.path.join(self.variant_dir, "manifest.json")
            m = json.load(open(mp))
            m.update(edit)
            json.dump(m, open(mp, "w"))
            write_scores(self.variant_dir, descchange_rows_all_thresholds_met())
            lines, code = judge_mod.judge(self.baseline_dir, self.variant_dir)
            self.assertEqual(code, 2, edit)
            self.assertIn("one experiment file but their manifests differ on: " + next(iter(edit)), lines[0])
        m.update(fixture_overlay_sha256="o2", jaeger_image="other")  # overlay and image alone are fine
        for k in edit:
            m.pop(k)
        json.dump(m, open(mp, "w"))
        self.assertEqual(judge_mod.judge(self.baseline_dir, self.variant_dir)[1], 0)

    def _cross_pair(self, variant_edit=None, noimg=None, baseline_edit=None, **kw):
        write_manifest(self.baseline_dir, arm_pin="baseline", sha256="aaa")
        if baseline_edit:
            mp = os.path.join(self.baseline_dir, "manifest.json")
            m = json.load(open(mp))
            baseline_edit(m)
            json.dump(m, open(mp, "w"))
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
            for k, v in {"model_requested": "sonnet", "n_per_arm": 10, "scenario_sha256": "s1",
                              "grader": {"cli_version": "2.1.1", "model": "gm", "prompt_sha256": "g1"}}.items():
                m.setdefault(k, v)
            m.setdefault("file_hashes_sha256", {"prompts/neutral.txt": "p1"})
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

    def test_cross_without_variant_arm_pin_leaves_the_baseline_arm_out(self):
        lines, code = self._cross_pair(lambda m: m.pop("arm_pin"), noimg=("o1", "o1"))
        self.assertEqual(code, 0)
        self.assertTrue(any("descchange" in l for l in lines))
        self.assertFalse(any(l.startswith("arm baseline") for l in lines), lines)

    def test_cross_experiment_model_difference_refused(self):
        lines, code = self._cross_pair(lambda m: m.update(model_requested="opus"), noimg=("o1", "o1"))
        self.assertEqual(code, 2)
        self.assertIn("model_requested ('sonnet' vs 'opus')", lines[0])

    def test_cli_and_claude_judge_as_one_client(self):
        """A pre-2026-11-23 batch (client cli, grader without a client) against a new one (claude, grader claude)."""
        grader = {"cli_version": "2.1.1", "model": "gm", "prompt_sha256": "g1"}
        lines, code = self._cross_pair(lambda m: m.update(client="claude", grader=dict(grader, client="claude")),
                                       baseline_edit=lambda m: m.update(client="cli", grader=dict(grader)), noimg=("o1", "o1"))
        self.assertEqual(code, 0, lines[0])
        self.assertIn("CROSS-EXPERIMENT", lines[0])

    def test_a_different_grader_refuses_and_old_manifests_still_match(self):
        old = {"cli_version": "2.1.1", "model": "gm", "prompt_sha256": "g1"}  # recorded before the goose grader: no client
        for grader, refused in ((dict(old), False), (dict(old, client="cli"), False), (dict(old, client="claude"), False),
                                (dict(old, model="other"), True),
                                (dict(old, client="goose", provider="openrouter"), True)):
            lines, code = self._cross_pair(lambda m, g=grader: m.update(grader=g), noimg=("o1", "o1"))
            self.assertEqual(code, 2 if refused else 0, lines[0])
            if refused:
                self.assertIn("grader (", lines[0])
        # one experiment file, the same arms: the grader is compared there too
        write_manifest(self.baseline_dir, arm_pin="baseline")
        write_scores(self.baseline_dir, baseline_rows())
        write_manifest(self.variant_dir, arm_pin="descchange")
        mp = os.path.join(self.variant_dir, "manifest.json")
        m = json.load(open(mp))
        m["grader"] = dict(old, model="other")
        json.dump(m, open(mp, "w"))
        write_scores(self.variant_dir, descchange_rows_all_thresholds_met())
        lines, code = judge_mod.judge(self.baseline_dir, self.variant_dir)
        self.assertEqual(code, 2)
        self.assertIn("one experiment file but their manifests differ on: grader", lines[0])


if __name__ == "__main__":
    unittest.main()
