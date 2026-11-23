#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""grade.py's Goose grader against a fake docker on PATH (no container, no model), and the grader identity a
re-score reads from the batch's manifest.

Run with: python3 -m unittest discover harness/tests
"""
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest
from unittest import mock

HARNESS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HARNESS_DIR)
sys.path.insert(0, os.path.join(HARNESS_DIR, "tests"))
import fake_grader  # noqa: E402,F401  (pins the claude grader; tests here patch what they need)
import goose_client  # noqa: E402
import grade  # noqa: E402
import score  # noqa: E402
from test_goose import msg, request, response  # noqa: E402
from test_score import build_run_dir_structured  # noqa: E402

KEY = "sk-grader-secret-123"
# grade.cache_key("the truth", "the answer") at 244cf46, model claude-fable-5-1: the cli key must never move.
CLI_KEY = "3daa0d526ff08ea4b5e437332a1cd3abc89f95e6c9258cbb20d8e458df92f0e2"
FINAL = [msg("m1", "assistant", request("t1", "recipe__final_output", {"label": "correct", "reason": "same cause"})),
         msg("m2", "user", response("t1", "ok")), {"type": "complete", "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.0012}]
DOCKER = """#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
if a[:1] == ["info"]:
    print("Ubuntu"); sys.exit(0)
if a[:2] == ["rm", "-f"]:
    open(os.environ["SHIM_LOG"], "a").write(json.dumps({"rm": a[2]}) + "\\n"); sys.exit(0)
work = next(v.split(":")[0] for v in a if v.endswith(":/work:ro"))
rd = lambda n: open(os.path.join(work, n)).read()
open(os.environ["SHIM_LOG"], "a").write(json.dumps({"argv": a, "recipe": rd("recipe.yaml"), "answer": rd("answer.txt"),
    "key": os.environ.get("ANTHROPIC_API_KEY"), "openrouter": os.environ.get("OPENROUTER_API_KEY")}) + "\\n")
for line in open(os.environ["SHIM_EVENTS"]):
    print(line, end="")
if os.path.exists(os.environ["SHIM_EVENTS"] + ".next"):  # the next call answers differently
    os.replace(os.environ["SHIM_EVENTS"] + ".next", os.environ["SHIM_EVENTS"])
"""


class GooseGrader(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        t = self._tmp.name
        self.batch, self.log, self.events = (os.path.join(t, n) for n in ("batch", "log", "events"))
        os.makedirs(self.batch)
        p = os.path.join(t, "docker")
        pathlib.Path(p).write_text(DOCKER)
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        self.events_are(FINAL)
        env = {"PATH": t + os.pathsep + os.environ["PATH"], "SHIM_LOG": self.log, "SHIM_EVENTS": self.events,
               "ANTHROPIC_API_KEY": KEY}
        for patcher in (mock.patch.dict(os.environ, env), mock.patch.object(grade, "CLIENT", "goose"),
                        mock.patch.object(grade, "PROVIDER", "anthropic"), mock.patch.object(grade, "GRADER_MODEL", "gm-1")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def events_are(self, events):
        pathlib.Path(self.events).write_text("".join(json.dumps(e) + "\n" for e in events))

    def shim_log(self):
        return [json.loads(l) for l in pathlib.Path(self.log).read_text().splitlines()] if os.path.isfile(self.log) else []

    def test_label_is_parsed_cached_and_the_text_is_not_recipe_text(self):
        truth, text = "truth {{ t }}", 'answer {{ x }} {% y %} {# z #} "q" \\  \nline 2'
        first = grade.grade(self.batch, truth, text)
        self.assertEqual((first["label"], first["reason"], first["cost_usd"], first["cached"]), ("correct", "same cause", 0.0012, False))
        self.assertTrue(grade.grade(self.batch, truth, text)["cached"])
        runs = [r for r in self.shim_log() if "argv" in r]
        self.assertEqual(len(runs), 1)
        r = runs[0]
        self.assertEqual(r["argv"][r["argv"].index("--params") + 1], "answer=/work/answer.txt")
        recipe = json.loads(r["recipe"])
        self.assertEqual((recipe["prompt"], recipe["extensions"], recipe["response"]["json_schema"], recipe["parameters"][0]["input_type"]),
                         ("{{ answer }}", [], grade.SCHEMA, "file"))
        self.assertEqual(recipe["instructions"], grade.prompt_text().strip())
        self.assertEqual(r["recipe"].count("{{"), 1)  # only the placeholder: none of the answer's markers is recipe text
        self.assertEqual(json.loads('"' + r["answer"] + '"'), grade.user_prompt(truth, text))  # the recipe's string reads back exactly
        self.assertNotIn("GOOSE_THINKING_EFFORT", " ".join(r["argv"]))
        self.assertIn("GOOSE_MODEL=gm-1", r["argv"])
        row = json.loads(pathlib.Path(self.batch, "grades.jsonl").read_text())
        self.assertEqual((row["client"], row["provider"], row["model"]), ("goose", "anthropic", "gm-1"))
        self.assertEqual([x["rm"] for x in self.shim_log() if "rm" in x], [r["argv"][r["argv"].index("--name") + 1]])

    def test_the_work_dir_is_made_readable_whatever_the_umask(self):
        chmods = []
        real = os.chmod
        with mock.patch.object(grade.os, "chmod", lambda p, m: (chmods.append((os.path.basename(p), m)), real(p, m))):
            grade.grade(self.batch, "t", "a")
        self.assertIn(("goose", 0o755), chmods)

    def test_provider_key_is_by_name_only(self):
        grade.grade(self.batch, "t", "a")
        r = next(r for r in self.shim_log() if "argv" in r)
        self.assertEqual((r["key"], r["openrouter"]), (KEY, None))
        self.assertIn("ANTHROPIC_API_KEY", r["argv"])
        written = json.dumps(r["argv"]) + r["recipe"] + r["answer"] + pathlib.Path(self.batch, "grades.jsonl").read_text()
        self.assertNotIn(KEY, written)

    def test_no_label_raises_and_caches_nothing(self):
        for events, said in (([{"type": "complete"}], ""),
                             ([msg("m1", "assistant", request("t1", "recipe__final_output", {"label": "maybe", "reason": "x"})),
                               msg("m2", "user", response("t1", "ok")), {"type": "complete"}], ""),
                             ([msg("m1", "assistant", {"type": "text", "text": "The cause is a bad token."}), {"type": "complete"}],
                              ": The cause is a bad token."),
                             ([{"type": "error", "error": "Rate limit exceeded: free-models-per-day"}],
                              ": Rate limit exceeded: free-models-per-day")):
            self.events_are(events)
            with self.assertRaises(grade.NoLabel) as e:
                grade.grade(self.batch, "t", "a")
            self.assertEqual(str(e.exception), "grader: the grader returned no structured answer (goose anthropic gm-1, exit 0)" + said)
        self.assertEqual(len([r for r in self.shim_log() if "argv" in r]), 8)  # each asked twice
        self.assertFalse(os.path.exists(os.path.join(self.batch, "grades.jsonl")))

    def test_no_label_then_a_label_is_graded_on_the_second_call(self):
        self.events_are([{"type": "complete"}])
        pathlib.Path(self.events + ".next").write_text("".join(json.dumps(e) + "\n" for e in FINAL))
        g = grade.grade(self.batch, "t", "a")
        self.assertEqual((g["label"], g["attempts"]), ("correct", 2))
        self.assertEqual(len([r for r in self.shim_log() if "argv" in r]), 2)
        self.assertEqual(json.loads(pathlib.Path(self.batch, "grades.jsonl").read_text())["attempts"], 2)

    def test_keys(self):
        who = {"client": "cli", "provider": None, "model": "claude-fable-5-1"}
        self.assertEqual(grade.cache_key("the truth", "the answer", who), CLI_KEY)
        goose = grade.cache_key("the truth", "the answer", {"client": "goose", "provider": "anthropic", "model": "claude-fable-5-1"})
        other = grade.cache_key("the truth", "the answer", {"client": "goose", "provider": "openrouter", "model": "claude-fable-5-1"})
        self.assertEqual(len({CLI_KEY, goose, other}), 3)

    def test_settings(self):
        self.assertEqual(grade.settings({}), ("goose", "", ""))  # the default: goose, no model
        self.assertEqual(grade.settings({"GOOSE_PROVIDER": "openrouter", "GOOSE_MODEL": "g"}), ("goose", "openrouter", "g"))
        self.assertEqual(grade.settings({"GOOSE_PROVIDER": "openrouter", "GRADER_PROVIDER": "anthropic", "GOOSE_MODEL": "g", "GRADER_MODEL": "m"}),
                         ("goose", "anthropic", "m"))
        self.assertEqual(grade.settings({"GRADER_CLIENT": "cli", "GOOSE_MODEL": "g"}), ("claude", "", "claude-fable-5-1"))  # the alias
        self.assertEqual(grade.settings({"GRADER_CLIENT": "claude", "GOOSE_MODEL": "g"}), ("claude", "", "claude-fable-5-1"))
        # given the agent's client, an empty GRADER_CLIENT follows it
        self.assertEqual(grade.settings({"GRADER_CLIENT": "", "GOOSE_MODEL": "g"}, "claude"), ("claude", "", "claude-fable-5-1"))
        self.assertEqual(grade.settings({"GRADER_CLIENT": "", "GOOSE_MODEL": "g"}, "codex"), ("goose", "", "g"))
        self.assertEqual(grade.settings({"GRADER_CLIENT": "goose", "GOOSE_MODEL": "g"}, "claude"), ("goose", "", "g"))

    def write_batch(self, grader, model="claude-fable-5-1"):
        """A batch whose manifest recorded `grader` and whose grades.jsonl holds one row under that grader's key."""
        manifest = {"grader": grader} if grader else {}
        pathlib.Path(self.batch, "manifest.json").write_text(json.dumps(manifest))
        who = {"client": grader.get("client") or "cli", "provider": grader.get("provider"), "model": grader["model"]}
        pathlib.Path(self.batch, "grades.jsonl").write_text(json.dumps(
            {"label": "correct", "reason": "r", "cost_usd": None, "key": grade.cache_key("t", "a", who), "model": grader["model"],
             "prompt_sha256": grade.prompt_sha()}) + "\n")

    def test_rescore_reads_the_manifests_grader_not_the_setting(self):
        old = {"model": "claude-fable-5-1", "prompt_sha256": "x", "cli_version": "2.1.282"}  # no client: written before GRADER_CLIENT
        self.write_batch(old)
        self.assertEqual(grade.batch_grader(self.batch), {"client": "claude", "provider": None, "model": "claude-fable-5-1"})
        hit = grade.grade(self.batch, "t", "a", call=False)  # the setting is goose; the batch is cli
        self.assertEqual((hit["label"], hit["cached"]), ("correct", True))
        self.assertIsNone(grade.grade(self.batch, "t", "another answer", call=False))
        self.assertEqual(self.shim_log(), [])  # never called

    def test_rescore_of_a_goose_batch_under_a_cli_setting(self):
        self.write_batch({"client": "goose", "provider": "openrouter", "model": "or-m"})
        with mock.patch.object(grade, "CLIENT", "cli"), mock.patch.object(grade, "PROVIDER", ""):
            self.assertTrue(grade.grade(self.batch, "t", "a", call=False)["cached"])

    def test_batch_without_a_manifest_or_grader_model_uses_the_setting(self):
        self.assertIsNone(grade.batch_grader(self.batch))
        pathlib.Path(self.batch, "manifest.json").write_text("{}")
        self.assertIsNone(grade.batch_grader(self.batch))
        self.assertIsNone(grade.grade(self.batch, "t", "a", call=False))

    def test_score_of_an_old_batch_is_not_ungraded_under_a_goose_setting(self):
        verdict = {"root_cause_service": "payment", "root_cause_operation": "charge", "mechanism": "invalid token",
                   "cascading": [{"service": "checkout"}], "confidence": "high", "evidence_span_ids": [], "abstain": False}
        d = build_run_dir_structured(self.batch, verdict)
        pathlib.Path(self.batch, "manifest.json").write_text(json.dumps(
            {"grader": {"model": "claude-fable-5-1", "prompt_sha256": "x", "cli_version": "v"}}))
        with mock.patch.object(grade, "CLIENT", "cli"), mock.patch.object(grade, "GRADER_MODEL", "claude-fable-5-1"), fake_grader.graded():
            first = score.score(d, call_grader=True)[0]
        again = score.score(d)[0]  # CLIENT is goose again
        self.assertEqual((first["mechanism_grade"], again["mechanism_grade"], again["mechanism"]), ("correct", "correct", "PASS"))
        self.assertEqual(again["grader_cached"], True)


class TemplateMarkers(unittest.TestCase):
    def test_a_param_prompt_leaves_the_answers_markers_alone_but_not_the_instructions(self):
        schema = {"type": "object"}
        self.assertIn('"{{ answer }}"', goose_client.build_recipe("", "plain instructions", schema, None, param="answer"))
        with self.assertRaises(ValueError):
            goose_client.build_recipe("", "instructions {{ x }}", schema, None, param="answer")
        with self.assertRaises(ValueError):
            goose_client.build_recipe("prompt {{ x }}", "plain", schema, None)

    def test_the_harness_grader_prompt_passes(self):
        goose_client.build_recipe("", grade.prompt_text(), grade.SCHEMA, None, param="answer")


if __name__ == "__main__":
    unittest.main()
