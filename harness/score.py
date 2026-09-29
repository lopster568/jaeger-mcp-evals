#!/usr/bin/env python3
"""Score one trajectory dir produced by bench.py run. Prints a compact JSON summary.

The final result event carries the agent's answer as a structured_output object
matching harness/verdict-schema.json (root_cause_service, root_cause_operation,
mechanism, cascading, confidence, evidence_span_ids, abstain).
- locus: PASS/PARTIAL/FAIL, from the root-cause service/operation against the
  scenario's pass_rule (exact strings).
- mechanism: the free-text mechanism graded against the scenario's mechanism_truth by
  grade.py (pinned model, cached in <batch>/grades.jsonl): correct PASS, incorrect FAIL,
  unclear UNCLEAR; UNGRADED when no cached label exists and the grader may not be called.
- cascade: PASS/FAIL against cascade_rule.
- abstained: verdict.abstain.
- verdict: PASS only if locus PASS and mechanism PASS; UNGRADED if locus PASS and the
  mechanism is ungraded; PARTIAL if locus PASS or PARTIAL but mechanism is not PASS;
  ABSTAIN if abstained; FAIL otherwise. INVALID, whatever the answer, when the sandbox
  check fails (sandbox_ok false) or the fixture changed under the trial in a way the
  scenario did not declare (fixture_ok false, bench.py's before/after container snapshot);
  else LEAK when the answer or any assistant text names a flag, "feature_flag" or "flagd"
  (leak_hits).
A run with no valid structured_output (schema-invalid answer, max turns, crash)
scores locus, mechanism and cascade MISSING and verdict FAIL; verdict_source is
"structured" or null so the two cases stay apart.
A trial recorded under record schema 4 or older, or with no schema_version (the mechanism
menu), raises Legacy: its
stored scores stand and it is never re-graded.

The scenario comes from the trial's meta.json. score(out_dir) is importable (bench.py
band and verify use it) so the CLI and the aggregate tools compute the same numbers
from the same raw files.
"""
import json
import os
import re
import sys

import fixture_leak
import grade

HARNESS_DIR = os.path.dirname(os.path.abspath(__file__))

# Minimal set of keys a candidate object must have before it counts as a structured verdict.
STRUCTURED_KEYS = ["root_cause_service", "root_cause_operation", "mechanism", "cascading", "abstain"]
SCHEMA_VERSION = 5  # record schema; 5 is the free-text mechanism, 4 and older the menu
LEAK_LITERALS = ["feature_flag", "flagd"]
GRADE_TO_MECHANISM = {"correct": "PASS", "incorrect": "FAIL", "unclear": "UNCLEAR"}


class Legacy(Exception):
    """A trial recorded under an older record schema: report its stored scores, never re-grade."""


def answer_leaks(texts, flag_names):
    """Sorted flag names and LEAK_LITERALS found in texts (case-insensitive, not inside a longer word)."""
    joined = "\n".join(texts)
    return sorted({t for t in list(flag_names) + LEAK_LITERALS
                   if re.search(r"(?<![a-z0-9])" + re.escape(t), joined, re.I)})


def leak_words(harness_dir=None):
    path = os.path.join(harness_dir or HARNESS_DIR, "leak-words.txt")
    words = []
    if not os.path.isfile(path):
        return words
    for line in open(path, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if line:
            words.append(line)
    return words


def leak_present(text, words):
    for w in words:
        if re.search(r"\b" + re.escape(w) + r"\b", text, re.I):
            return True
    return False


def load_scenario(scenario):
    with open(os.path.join(HARNESS_DIR, "scenarios", f"{scenario}.json"), encoding="utf-8") as f:
        return json.load(f)


def compute_verdict(locus, mechanism, abstained):
    if abstained:
        return "ABSTAIN"
    if locus is None:
        return None
    if locus == "PASS" and mechanism in ("PASS", "UNGRADED"):
        return mechanism
    if locus in ("PASS", "PARTIAL"):
        return "PARTIAL"
    return "FAIL"


def extract_structured(final):
    """Return the structured verdict dict carried by the result event, or
    None if it does not look like one.

    The Claude Code CLI's --json-schema flag carries the validated answer in
    a "structured_output" field alongside "result" on the result event
    (every structured run on disk has it).
    """
    if not isinstance(final, dict):
        return None
    candidate = final.get("structured_output")
    if not isinstance(candidate, dict):
        return None
    if not all(k in candidate for k in STRUCTURED_KEYS):
        return None
    if not isinstance(candidate.get("cascading"), list):
        return None
    return candidate


def _exact_match(text, accepted):
    """True iff text, stripped and lowercased, equals one of the accepted
    strings (also stripped and lowercased) exactly - no substring or regex
    matching, so "payment-gateway" never passes against ["payment"]."""
    if not accepted:
        return False
    norm = text.strip().lower()
    return any(norm == str(a).strip().lower() for a in accepted)


def compute_locus_structured(verdict, pass_rule):
    """PASS/PARTIAL/FAIL for the structured path: root_cause_service and
    root_cause_operation against pass_rule.service_exact and
    pass_rule.operation_exact, lists of accepted exact strings."""
    if not pass_rule:
        return None
    service_match = _exact_match(str(verdict.get("root_cause_service") or ""), pass_rule.get("service_exact"))
    operation_match = _exact_match(str(verdict.get("root_cause_operation") or ""), pass_rule.get("operation_exact"))

    if service_match and operation_match:
        return "PASS"
    if service_match:
        return "PARTIAL"
    return "FAIL"


def evaluate_cascade_structured(verdict, cascade_rule):
    """PASS iff any cascading[].service matches cascade_rule. Every
    required_any group needs at least one cascading entry whose service
    matches one of that group's regexes."""
    services = [str(c.get("service", "")) for c in verdict["cascading"] if isinstance(c, dict)]
    if not cascade_rule:
        return "PASS"
    for group in cascade_rule.get("required_any", []):
        if not any(any(re.search(pat, s, re.I) for s in services) for pat in group):
            return "FAIL"
    return "PASS"


def check_abstain_structured(verdict):
    return bool(verdict.get("abstain"))


JAEGER_PREFIX = "mcp__jaeger__"
VERDICT_TOOL = "StructuredOutput"  # the CLI's own tool for the --json-schema answer
# tool_result text for a name the client does not have: the CLI's, then agent_loop.py's
REJECTED = ("No such tool available", "unknown tool: ")


def tools_on(meta):
    """False only for an arm with tools off (bench.py records its mcp_endpoint as null)."""
    return meta.get("mcp_endpoint", "") is not None


def jaeger_names(out_dir, meta, init_tools):
    """The exact mcp__jaeger__ tool names this trial was meant to see: the batch's tools.json
    (the pre-flight tools/list), none for an arm with tools off, else the init event's
    mcp__jaeger__ names, else None (no list anywhere: any mcp__jaeger__ name counts)."""
    if not tools_on(meta):
        return set()
    path = os.path.join(os.path.dirname(os.path.abspath(out_dir)), "tools.json")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return {JAEGER_PREFIX + t["name"] for t in json.load(f)}
    if init_tools is not None:
        return {t for t in init_tools if t.startswith(JAEGER_PREFIX)}
    return None


def sandbox_check(init, all_calls, results_by_id, allowed_jaeger, client, with_tools):
    """Violations of the trial's sandbox, as strings; empty means sandbox_ok. The init event
    must list exactly the Jaeger tools, plus StructuredOutput for the cli client (never for
    api; codex lists no tools), and exactly one MCP server, jaeger (none for an arm with tools
    off). Every tool_use must name an allowed tool; one the client refused (REJECTED) is
    attempted_unknown_tool, any other executed_unknown_tool. codex_client's "error" items are
    not tool calls."""
    if init is None:
        return ["no_init_event"]
    out, verdict_tool = [], {VERDICT_TOOL} if client != "api" else set()
    allowed = (allowed_jaeger or set()) | verdict_tool
    if "tools" in init and allowed_jaeger is not None:
        got = set(init["tools"] or [])
        out += ["init_tool_extra:%s" % t for t in sorted(got - allowed)]
        out += ["init_tool_missing:%s" % t for t in sorted((allowed_jaeger - got) | ({VERDICT_TOOL} - got if client == "cli" else set()))]
    servers = [m.get("name") for m in init.get("mcp_servers") or [] if isinstance(m, dict)]
    if servers != (["jaeger"] if with_tools else []):
        out.append("mcp_servers:%s" % ",".join(map(str, servers)))
    for c in all_calls:
        name = c["name"]
        if client == "codex" and name == "error":
            continue
        ok = name in allowed if allowed_jaeger is not None else name in verdict_tool or is_jaeger_name(name)
        if not ok:
            r = results_by_id.get(c["id"]) or {}
            kind = "attempted_unknown_tool" if r.get("rejected") else "executed_unknown_tool"
            out.append("%s:%s" % (kind, name))
    return out


def is_jaeger_name(name):
    """A Jaeger MCP tool name when no exact list is known: the prefix, and never the verdict tool's
    name under it (mcp__jaeger__StructuredOutput is a model's invention, refused by the CLI)."""
    return name.startswith(JAEGER_PREFIX) and name != JAEGER_PREFIX + VERDICT_TOOL


def is_compaction_event(event, raw_line):
    """Conservative compaction detector.

    CLI 2.1.282's bundled SDK schema describes the stream message as
    {"type": "system", "subtype": "compact_boundary", "compact_metadata":
    {"trigger": "manual"|"auto", "pre_tokens": ...}}, but no captured run has
    ever contained one, so the shape is unconfirmed. Counted: any system
    event whose subtype contains "compact", and any event whose raw JSON text
    contains the quoted token "compact_boundary". Not counted: the init
    event's slash_commands list, which names "compact" and "autocompact" in
    every run."""
    if event.get("type") == "system" and "compact" in str(event.get("subtype") or "").lower():
        return True
    return '"compact_boundary"' in raw_line


def score(out_dir, call_grader=False, scenario=None):
    """Read the raw files in out_dir and return (summary_dict, final_answer_text).
    scenario: the scenario dict to score against (verify passes the one at the batch's git sha);
    default is the current file.
    call_grader: on a grade cache miss, call the grader (bench.py run only); otherwise the
    mechanism of a miss is UNGRADED.

    Tool-call metrics (tool_calls, call_sequence, call_errors,
    steps_to_evidence, tool_output_chars) are computed over calls to the
    batch's Jaeger tools only (jaeger_names). The CLI delivers the
    structured verdict through an internal tool (e.g. "StructuredOutput")
    that is not a Jaeger MCP tool and must not be counted as investigation
    work; tool_result blocks are matched back to their tool_use by
    tool_use_id so a non-Jaeger tool's result is excluded too. The other
    tool names seen are still reported, in "non_jaeger_tool_calls", so
    nothing is hidden.
    """
    with open(os.path.join(out_dir, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    if meta.get("schema_version", 0) < SCHEMA_VERSION:
        raise Legacy("legacy schema, stored scores")
    scenario_name = meta["scenario"]
    scenario = scenario or load_scenario(scenario_name)
    signal = re.compile(scenario["signal_regex"], re.I)
    raw_lines = [l for l in open(os.path.join(out_dir, "stream.jsonl")) if l.strip()]
    events = [json.loads(l) for l in raw_lines]
    compaction_events = sum(1 for e, l in zip(events, raw_lines) if is_compaction_event(e, l))
    all_calls, results_by_id, final, model, tools, init, texts = [], {}, None, None, [], None, []
    for e in events:
        if e.get("type") == "system" and e.get("subtype") == "init":
            init = init or e
            model = e.get("model")
            tools = [t for t in e.get("tools", []) if t.startswith("mcp__")]
        if e.get("type") == "assistant":
            for b in e["message"].get("content", []):
                if b.get("type") == "text":
                    texts.append(str(b.get("text") or ""))
                if b.get("type") == "tool_use":
                    all_calls.append({"id": b.get("id"), "name": b["name"], "input": b.get("input")})
        if e.get("type") == "user":
            for b in e["message"].get("content", []):
                if b.get("type") == "tool_result":
                    c = b.get("content")
                    text = c if isinstance(c, str) else "".join(x.get("text", "") for x in (c or []) if isinstance(x, dict))
                    results_by_id[b.get("tool_use_id")] = {
                        "is_error": bool(b.get("is_error")),
                        "chars": len(text),
                        "signal": bool(signal.search(text)),
                        "rejected": bool(b.get("is_error")) and any(m in text for m in REJECTED),
                    }
        if e.get("type") == "result":
            final = e

    allowed_jaeger = jaeger_names(out_dir, meta, init.get("tools") if init and "tools" in init else None)
    is_jaeger = (lambda n: n in allowed_jaeger) if allowed_jaeger is not None else is_jaeger_name
    calls = [c for c in all_calls if is_jaeger(c["name"])]
    non_jaeger_tool_calls = [c["name"] for c in all_calls if not is_jaeger(c["name"])]
    sandbox_violations = sandbox_check(init, all_calls, results_by_id, allowed_jaeger, meta.get("client"), tools_on(meta))
    results = [results_by_id[c["id"]] for c in calls if c["id"] in results_by_id]

    # read_skill directory-path failure: a read_skill call can be
    # attempted and still come back is_error (or a near-empty result) when the
    # agent names a directory instead of a skill file. Scoring only whether it
    # was ever *attempted* hid that failure inside a 20/20 "called" number and
    # confounded every skill arm. attempted/succeeded/errors are computed
    # directly off each read_skill call's own result, keyed by tool_use_id
    # (not off results_by_id filtered to Jaeger calls, since read_skill is a
    # non-Jaeger tool).
    read_skill_calls = [c for c in all_calls if c["name"].endswith("read_skill")]
    read_skill_results = [results_by_id[c["id"]] for c in read_skill_calls if c["id"] in results_by_id]
    read_skill_attempted = len(read_skill_calls) > 0
    read_skill_succeeded = any((not r["is_error"]) and r["chars"] > 200 for r in read_skill_results)
    read_skill_errors = sum(1 for r in read_skill_results if r["is_error"])

    steps_to_evidence = next((i + 1 for i, r in enumerate(results) if r["signal"]), None)
    u = (final or {}).get("usage", {})

    structured_verdict = extract_structured(final)
    g = None
    if structured_verdict is not None:
        locus = compute_locus_structured(structured_verdict, scenario.get("pass_rule"))
        cascade = evaluate_cascade_structured(structured_verdict, scenario.get("cascade_rule"))
        abstained = check_abstain_structured(structured_verdict)
        mechanism_value = str(structured_verdict.get("mechanism"))
        if not abstained:
            g = grade.grade(os.path.dirname(os.path.abspath(out_dir)), scenario["mechanism_truth"], mechanism_value,
                            call=call_grader)
        mechanism = None if abstained else GRADE_TO_MECHANISM[g["label"]] if g else "UNGRADED"
    else:
        locus = mechanism = cascade = "MISSING"
        abstained, mechanism_value = False, None
    final_text = str((final or {}).get("result", "<none>"))
    leak_hits = answer_leaks(texts + [final_text, json.dumps((final or {}).get("structured_output"))],
                             meta.get("flag_names") or fixture_leak.FLAGS)
    # A call the client refused shows the sandbox held; it is recorded, not disqualifying.
    fixture_changes = meta.get("fixture_changes") or []
    fixture_ok = meta.get("fixture_ok", True)
    breached = [v for v in sandbox_violations if not v.startswith("attempted_unknown_tool:")]
    # fixture_ok false scores INVALID the same way a sandbox breach does (below), without
    # folding it into sandbox_ok/sandbox_violations, which stay about the agent's own conduct.
    verdict = "INVALID" if breached or not fixture_ok else "LEAK" if leak_hits else compute_verdict(locus, mechanism, abstained)

    prompt_path = os.path.join(out_dir, "prompt.txt")
    signal_leaked_in_prompt = False
    if os.path.isfile(prompt_path):
        prompt_text = open(prompt_path, encoding="utf-8", errors="replace").read()
        signal_leaked_in_prompt = leak_present(prompt_text, leak_words())

    summary = {
        "dir": os.path.basename(out_dir),
        "scenario_used": scenario_name,
        "model_asserted": model,
        "mcp_tools_visible": len(tools) if model else None,
        "tool_calls": len(calls),
        "call_sequence": [c["name"].replace("mcp__jaeger__", "") for c in calls],
        "non_jaeger_tool_calls": non_jaeger_tool_calls,
        "call_errors": sum(r["is_error"] for r in results),
        "read_skill_attempted": read_skill_attempted,
        "read_skill_succeeded": read_skill_succeeded,
        "read_skill_errors": read_skill_errors,
        "steps_to_evidence": steps_to_evidence,
        "tool_output_chars": sum(r["chars"] for r in results),
        "input_tokens_total": u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0),
        "output_tokens": u.get("output_tokens"),
        "num_turns": (final or {}).get("num_turns"),
        "cost_usd": (final or {}).get("total_cost_usd"),
        "duration_s": round(((final or {}).get("duration_ms") or 0) / 1000, 1),
        "stop": (final or {}).get("subtype"),
        "locus": locus,
        "mechanism": mechanism,
        "mechanism_value": mechanism_value,
        "mechanism_grade": g and g["label"],
        "mechanism_grade_reason": g and g["reason"],
        "grader_model": g and g["model"],
        "grader_prompt_sha256": g and g["prompt_sha256"],
        "grader_cached": g and g["cached"],
        "cascade": cascade,
        "abstained": abstained,
        "verdict": verdict,
        "verdict_source": "structured" if structured_verdict is not None else None,
        "signal_leaked_in_prompt": signal_leaked_in_prompt,
        "system_under_test": meta.get("system_under_test"),
        "effort": meta.get("effort"),
        "compaction_events": compaction_events,
        "sandbox_ok": not breached,
        "sandbox_violations": sandbox_violations,
        "leak_hits": leak_hits,
        "fixture_ok": fixture_ok,
        "fixture_changes": fixture_changes,
    }
    return summary, final_text


def main():
    out = sys.argv[1]
    summary, final_text = score(out)
    print(json.dumps(summary, indent=1))
    print("\n=== FINAL ANSWER ===\n" + final_text)


if __name__ == "__main__":
    main()
