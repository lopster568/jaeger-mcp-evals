#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""harness/goose_client.py: the Goose-to-stream.jsonl mapping on real Goose v1.57.0 recordings
(data/goose/<case>/stdout.jsonl and goose-stderr.txt, a stub model, 2026-11-21), scored by
score.py; the recipe; the container cleanup.

Run with: python3 -m unittest discover harness/tests
Python 3 stdlib only, no network, no LLM, no docker (a fake docker on PATH where one is needed).
"""
import hashlib
import json
import os
import pathlib
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HARNESS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HARNESS_DIR)
import bench  # noqa: E402
import goose_client  # noqa: E402
import otlp  # noqa: E402
import score  # noqa: E402

DATA = os.path.join(HARNESS_DIR, "tests", "data", "goose")
# The 9 Jaeger tools Goose advertised in the recordings, as the batch's tools.json holds them.
JAEGER_TOOLS = ["get_critical_path", "get_service_dependencies", "get_services", "get_span_details", "get_span_names",
                "get_trace_errors", "get_trace_topology", "read_skill", "search_traces"]
VERDICT = {"root_cause_service": "probe-service", "root_cause_operation": "probe-operation",
           "mechanism": "stub verdict, no investigation", "cascading": [], "confidence": "low",
           "evidence_span_ids": [], "abstain": True}


def recording(case):
    d = os.path.join(DATA, case)
    return goose_client.read_events(os.path.join(d, "stdout.jsonl")), pathlib.Path(d, "goose-stderr.txt").read_text(encoding="utf-8")


def stream_of(case, **kw):
    events, stderr = recording(case)
    return goose_client.to_stream(events, stderr, "claude-sonnet-5-5", "1.57.0", **kw)


def msg(id_, role, *content, created=1795256555):
    return {"type": "message", "message": {"id": id_, "role": role, "created": created, "content": list(content)}}


def request(id_, name, args=None):
    return {"type": "toolRequest", "id": id_, "toolCall": {"status": "success", "value": {"name": name, "arguments": args or {}}}}


def response(id_, text, is_error=False):
    return {"type": "toolResponse", "id": id_, "toolResult": {"status": "success", "value": {
        "content": [{"type": "text", "text": text}], "isError": is_error}}}


class Scored(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.batch = os.path.join(self._tmp.name, "batch-20261121T000000Z")
        os.makedirs(self.batch)
        with open(os.path.join(self.batch, "tools.json"), "w") as f:
            json.dump([{"name": n} for n in JAEGER_TOOLS], f)
        self.n = 0

    def tearDown(self):
        self._tmp.cleanup()

    def score(self, stream, client="goose", tools=True):
        self.n += 1
        d = os.path.join(self.batch, "%d-arm" % self.n)
        os.makedirs(d)
        with open(os.path.join(d, "meta.json"), "w") as f:
            json.dump({"scenario": "paymentFailure", "schema_version": score.SCHEMA_VERSION, "client": client,
                       "mcp_endpoint": ":16686/jaeger/ui/api/ai/mcp/" if tools else None}, f)
        with open(os.path.join(d, "stream.jsonl"), "w") as f:
            f.writelines(json.dumps(e) + "\n" for e in stream)
        return score.score(d)[0]


class TestRecordings(Scored):
    def test_success(self):
        stream = stream_of("success")
        self.assertEqual(stream[0], {"type": "system", "subtype": "init", "model": "claude-sonnet-5-5", "client_version": "1.57.0",
                                     "goose_image": goose_client.IMAGE, "mcp_servers": [{"name": "jaeger"}]})
        r = stream[-1]
        self.assertEqual((r["subtype"], r["structured_output"], r["num_tool_calls"]), ("success", VERDICT, 2))
        # Goose's input 3330 includes cache read 300 and cache write 30.
        self.assertEqual(r["usage"], {"input_tokens": 3000, "cache_read_input_tokens": 300,
                                      "cache_creation_input_tokens": 30, "output_tokens": 21})
        self.assertEqual(r["total_cost_usd"], 0.006315)
        self.assertEqual(json.loads(r["result"]), VERDICT)  # the last assistant text
        s = self.score(stream)
        self.assertEqual((s["sandbox_ok"], s["sandbox_violations"], s["verdict"], s["stop"]), (True, [], "ABSTAIN", "success"))
        self.assertEqual((s["call_sequence"], s["input_tokens_total"], s["output_tokens"], s["cost_usd"]),
                         (["get_services"], 3330, 21, 0.006315))
        self.assertEqual(s["client_version"], "1.57.0")

    def test_timestamps_come_from_created(self):
        stream = stream_of("success")
        stamps = {e.get("timestamp") for e in stream if e["type"] in ("assistant", "user")}
        self.assertEqual(stamps, {"2026-11-21T10:22:35.000Z"})  # created 1795256555
        spans = otlp.trace_spans("k", stream, 1795256554 * 10**9, [])
        self.assertNotIn("timestamp.synthetic", [a["key"] for s in spans for a in s["attributes"]])  # otlp.py reads them

    def test_badtool_is_recorded_not_a_breach(self):
        stream = stream_of("badtool")
        refused = [b for e in stream if e["type"] == "user" for b in e["message"]["content"] if b["tool_use_id"] == "toolu_badtool_0"][0]
        self.assertEqual((refused["is_error"], refused["content"]), (True, "Tool 'jaeger__no_such_tool' is not available."))
        s = self.score(stream)
        self.assertEqual((s["sandbox_ok"], s["sandbox_violations"]), (True, ["attempted_unknown_tool:mcp__jaeger__no_such_tool"]))
        self.assertEqual(s["verdict"], "ABSTAIN")

    def test_badfinal_takes_the_accepted_second_answer(self):
        stream = stream_of("badfinal")
        first = [b for e in stream if e["type"] == "user" for b in e["message"]["content"]][0]
        self.assertTrue(first["is_error"])
        self.assertTrue(first["content"].startswith("-32602: Validation failed"))
        self.assertEqual(stream[-1]["structured_output"], VERDICT)
        s = self.score(stream)
        self.assertEqual((s["sandbox_ok"], s["verdict"], s["stop"]), (True, "ABSTAIN", "success"))

    def test_loop_stops_at_max_turns(self):
        stream = stream_of("loop")
        self.assertEqual((stream[-1]["subtype"], stream[-1]["structured_output"]), ("error_max_turns", None))
        s = self.score(stream)
        self.assertEqual((s["stop"], s["verdict"], s["tool_calls"], s["sandbox_ok"]), ("error_max_turns", "FAIL", 3, True))
        self.assertIn("stopped: error_max_turns", s["client_limits"])

    def test_reject_is_an_execution_error(self):
        stream = stream_of("reject", returncode=1)
        r = stream[-1]
        self.assertEqual((r["subtype"], r["total_cost_usd"]), ("error_during_execution", None))
        self.assertIn("Bad request (400)", r["error"])
        s = self.score(stream)
        self.assertEqual((s["stop"], s["verdict"], s["sandbox_ok"]), ("error_during_execution", "FAIL", True))

    def test_connectfail_scores_invalid(self):
        stream = stream_of("connectfail")
        self.assertEqual(stream[0]["mcp_servers"], [])
        self.assertEqual(stream[-1]["subtype"], "error_during_execution")
        s = self.score(stream)
        self.assertEqual(s["verdict"], "INVALID")
        self.assertIn("mcp_servers:", s["sandbox_violations"])


class TestMapping(Scored):
    def test_text_deltas_join_per_message_id(self):
        events = [msg("m1", "assistant", {"type": "text", "text": "Looking "}),
                  msg("m1", "assistant", {"type": "text", "text": "at traces."}),
                  msg("m2", "assistant", {"type": "text", "text": "Done."})]
        texts = [e["message"]["content"][0]["text"] for e in goose_client.to_stream(events, "", "m", "1")
                 if e["type"] == "assistant"]
        self.assertEqual(texts, ["Looking at traces.", "Done."])

    def test_unparsable_tool_call_writes_no_nameless_tool_use(self):
        events = [msg("m1", "assistant", {"type": "toolRequest", "id": "t1", "toolCall": {"status": "error", "error": "bad json"}})]
        stream = goose_client.to_stream(events, "", "m", "1")
        self.assertFalse([e for e in stream if e["type"] == "assistant"])

    def test_compaction_notice_is_counted(self):
        # Shape from Goose v1.57.0 source (ops_compaction.rs, message.rs); not in a recording.
        notice = {"type": "systemNotification", "notificationType": "inlineMessage",
                  "msg": "Exceeded auto-compact threshold of 102k tokens. Performing auto-compaction..."}
        stream = goose_client.to_stream([msg("m1", "assistant", notice), {"type": "complete"}], "", "m", "1")
        s = self.score(stream)
        self.assertEqual(s["compaction_events"], 1)
        self.assertIn("compacted 1", s["client_limits"])

    def test_accepted_answer_wins_over_the_max_turns_text(self):
        events = [msg("m1", "assistant", request("f1", "recipe__final_output", VERDICT)),
                  msg("u1", "user", response("f1", "Final output successfully collected.")),
                  msg("m2", "assistant", {"type": "text", "text": goose_client.MAX_TURNS_MESSAGE}), {"type": "complete"}]
        r = goose_client.to_stream(events, "", "m", "1")[-1]
        self.assertEqual((r["subtype"], r["structured_output"]), ("success", VERDICT))

    def test_no_complete_and_no_error_is_an_execution_error(self):
        r = goose_client.to_stream([], "", "m", "1", returncode=None)[-1]
        self.assertEqual((r["subtype"], r["error"]), ("error_during_execution", "goose stopped before it finished"))

    def test_an_exit_without_complete_carries_the_last_stderr_line(self):
        stderr = "starting session\nRate limit exceeded: free-models-per-day. Add 10 credits\n\n"
        r = goose_client.to_stream([], stderr, "m", "1", returncode=1)[-1]
        self.assertEqual(r["error"], "goose exited 1 without a complete event: Rate limit exceeded: free-models-per-day. Add 10 credits")
        self.assertEqual(goose_client.to_stream([], "", "m", "1", returncode=1)[-1]["error"], "goose exited 1 without a complete event")

    def test_accepted_answer_without_complete_is_not_a_success(self):
        """A trial stopped after the answer but before Goose's complete event has no usage: never a success."""
        events = [msg("a1", "assistant", request("t1", "recipe__final_output", {"x": 1})),
                  msg("u1", "user", response("t1", "Final output successfully collected."))]
        r = goose_client.to_stream(events, "", "m", "1", returncode=None)[-1]
        self.assertEqual((r["subtype"], r["structured_output"]), ("error_during_execution", {"x": 1}))
        r = goose_client.to_stream(events + [{"type": "complete"}], "", "m", "1", returncode=None)[-1]  # stopped after complete
        self.assertEqual(r["subtype"], "error_during_execution")

    def test_the_wrapper_is_jaegers_copy(self):
        # sha256 of `git show f9046f3dad:scripts/ai-sidecar/goose/goose-without-extensions.sh` in jaegertracing/jaeger
        # (also Jaeger main 8e07bbf4ba, 2026-11-24). A bump of the demo's Goose means updating IMAGE, this sha and
        # re-recording tests/data/goose together.
        with open(goose_client.WRAPPER, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), "541f6a5a6032c83e2c222361162927ba166999369465663481c576e6242eb507")

    def test_docker_argv_mounts_work_by_absolute_path(self):
        argv = goose_client.docker_argv("n", False, os.path.join("runs", "b", "0-a", "goose"), "anthropic", "m", "high", 5)
        self.assertIn("%s:/work:ro" % os.path.abspath(os.path.join("runs", "b", "0-a", "goose")), argv)

    def test_tools_off_arm_lists_no_server(self):
        stream = goose_client.to_stream([{"type": "complete"}], "", "m", "1", with_tools=False)
        self.assertEqual(stream[0]["mcp_servers"], [])
        self.assertTrue(self.score(stream, tools=False)["sandbox_ok"])

    def load_skill_stream(self):
        return goose_client.to_stream([msg("m1", "assistant", request("s1", "load_skill", {"name": "error-root-cause"})),
                                       msg("u1", "user", response("s1", "Skill not found", True)),
                                       msg("m2", "assistant", request("s2", "load_skill", {"name": "x"})),
                                       msg("u2", "user", response("s2", "Skill not found", True)),
                                       {"type": "complete"}], "", "m", "1")

    def test_load_skill_is_allowed_and_counted_for_goose(self):
        s = self.score(self.load_skill_stream())
        self.assertEqual((s["sandbox_ok"], s["sandbox_violations"]), (True, []))
        self.assertIn("load_skill calls 2", s["client_limits"])
        self.assertEqual(s["non_jaeger_tool_calls"], ["load_skill", "load_skill"])

    def test_load_skill_is_still_a_breach_for_cli_and_codex(self):
        for client in ("cli", "codex"):
            s = self.score(self.load_skill_stream(), client=client)
            self.assertEqual((s["verdict"], s["sandbox_violations"][0]), ("INVALID", "executed_unknown_tool:load_skill"), client)


class TestRecipe(unittest.TestCase):
    SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}}

    def test_keys_goose_accepted(self):
        doc = json.loads(goose_client.build_recipe("prompt\n", "system\n", self.SCHEMA, "http://h:16686/jaeger/ui/api/ai/mcp/"))
        self.assertEqual(sorted(doc), ["description", "extensions", "instructions", "prompt", "response", "title", "version"])
        self.assertEqual((doc["prompt"], doc["instructions"], doc["response"]), ("prompt", "system", {"json_schema": self.SCHEMA}))
        self.assertEqual(doc["extensions"], [{"type": "streamable_http", "name": "jaeger",
                                              "uri": "http://h:16686/jaeger/ui/api/ai/mcp/", "timeout": 300}])
        self.assertEqual(json.loads(goose_client.build_recipe("p", "s", self.SCHEMA, None))["extensions"], [])

    def test_template_markers_are_refused(self):
        for marker in ("{{", "{%", "{#"):
            for args in (("x %s y" % marker, "s", self.SCHEMA), ("p", "x %s y" % marker, self.SCHEMA),
                         ("p", "s", {"description": "x %s y" % marker})):
                with self.assertRaises(ValueError, msg=(marker, args)):
                    goose_client.build_recipe(*args, None)

    def test_the_harness_texts_pass(self):
        read = lambda n: pathlib.Path(HARNESS_DIR, n).read_text(encoding="utf-8")
        for p in pathlib.Path(HARNESS_DIR, "prompts").glob("*.txt"):
            goose_client.build_recipe(p.read_text(encoding="utf-8"), read("system-prompt.txt"),
                                      json.loads(read("verdict-schema.json")), "http://h:1/")

    def test_loopback_goes_to_host_docker_internal_on_docker_desktop(self):
        for url in ("http://localhost:16686/p/", "http://127.0.0.2:16686/p/", "http://[::1]:16686/p/"):
            self.assertEqual(goose_client.container_url(url, True), "http://host.docker.internal:16686/p/", url)
            self.assertEqual(goose_client.container_url(url, False), url)
        self.assertEqual(goose_client.container_url("http://10.0.0.5:16686/p/", True), "http://10.0.0.5:16686/p/")

    def test_container_name_is_docker_safe(self):
        self.assertEqual(goose_client.container_name("/r/batch-20261121T000000Z/3-my arm!"), "goose-batch-20261121T000000Z-3-my-arm-")

    def test_pack_publishes_the_recipe_host_free(self):
        with tempfile.TemporaryDirectory() as t:
            os.makedirs(os.path.join(t, "b", "0-a", "goose"))
            pathlib.Path(t, "b", "0-a", "goose", "recipe.yaml").write_text(
                goose_client.build_recipe("p", "s", self.SCHEMA, "http://fixture.example:16686/jaeger/ui/api/ai/mcp/"))
            data = dict(bench.pack_members([("s", os.path.join(t, "b"))]))["s/b/0-a/goose/recipe.yaml"].decode()
        self.assertNotIn("fixture.example", data)
        self.assertEqual(json.loads(data)["extensions"][0]["uri"], ":16686/jaeger/ui/api/ai/mcp/")


FAKE_DOCKER = """#!/bin/bash
echo "$*" >> "$FAKE_LOG"
case "$1" in
  info) echo "Ubuntu 24.04" ;;
  run) exec sleep 30 ;;
esac
"""


class TestCleanup(unittest.TestCase):
    def test_sigterm_removes_the_container(self):
        self.stop_with(signal.SIGTERM)

    def test_sighup_removes_the_container(self):
        self.stop_with(signal.SIGHUP)

    def stop_with(self, sig):
        with tempfile.TemporaryDirectory() as t:
            binp, out, log = os.path.join(t, "bin"), os.path.join(t, "batch-x", "0-a"), os.path.join(t, "docker.log")
            os.makedirs(binp)
            os.makedirs(out)
            p = os.path.join(binp, "docker")
            pathlib.Path(p).write_text(FAKE_DOCKER)
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
            mcp = os.path.join(out, "mcp.json")
            pathlib.Path(mcp).write_text('{"mcpServers": {"jaeger": {"type": "http", "url": "http://127.0.0.1:1/m/"}}}')
            env = dict(os.environ, PATH=binp + os.pathsep + os.environ["PATH"], FAKE_LOG=log, ANTHROPIC_API_KEY="sk-x")
            proc = subprocess.Popen([sys.executable, os.path.join(HARNESS_DIR, "goose_client.py"), "--provider", "anthropic",
                                     "--model", "m", "--effort", "high", "--prompt-file", os.path.join(HARNESS_DIR, "prompts", "neutral.txt"),
                                     "--system-prompt-file", bench.SYSTEM_PROMPT, "--schema-file", bench.VERDICT_SCHEMA,
                                     "--mcp-config", mcp, "--out-dir", os.path.join("batch-x", "0-a")],  # relative, as RUNS_DIR=runs gives
                                    cwd=t, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for _ in range(100):  # until the fake container is "running"
                if os.path.exists(log) and "run --rm" in pathlib.Path(log).read_text():
                    break
                time.sleep(0.1)
            proc.send_signal(sig)
            proc.communicate(timeout=30)
            self.assertEqual(proc.returncode, 1)
            calls = pathlib.Path(log).read_text().splitlines()
            self.assertEqual(calls[-1], "rm -f goose-batch-x-0-a")
            run = next(c for c in calls if c.startswith("run --rm"))
            self.assertIn("-v %s:/work:ro" % os.path.join(os.path.realpath(out), "goose"), run.replace(t, os.path.realpath(t)))
            final = bench.read_jsonl(os.path.join(out, "stream.jsonl"))[-1]
            self.assertEqual((final["subtype"], final["error"]), ("error_during_execution", "goose stopped before it finished"))


class GoosePreflightHost(unittest.TestCase):
    def refusal(self, host, desktop):
        cfg = {"FIXTURE_HOST": host, "JAEGER_UI_PORT": "16686", "JAEGER_BASE_PATH": "/jaeger/ui", "OPENROUTER_API_KEY": "k"}
        a = type("A", (), {"provider": "openrouter"})()
        with mock.patch.object(bench, "goose_docker", return_value=None), \
                mock.patch.object(goose_client, "docker_desktop", return_value=desktop):
            return bench.goose_preflight(a, cfg, mock.Mock())

    def test_a_loopback_alias_is_refused_on_docker_desktop_only(self):
        message, fix = self.refusal("127.0.0.3", True)
        self.assertIn("127.0.0.3", message)
        self.assertIn("FIXTURE_HOST=127.0.0.1", fix)
        self.assertIsNone(self.refusal("127.0.0.3", False))
        for host in ("127.0.0.1", "localhost", "10.0.0.5", "fixture.example.test"):
            self.assertIsNone(self.refusal(host, True), host)


if __name__ == "__main__":
    unittest.main()
