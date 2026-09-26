# desc-change-sep28: notes

Hand-written. RESULT.md is rewritten by `harness/judge.py` on every judge run, so notes live here.
Every number below is computed from the two batches' scores.jsonl and manifest.json in this folder.

## Verdict

The pre-registered verdict stands: EXPERIMENT FAIL (RESULT.md). Three thresholds passed; `get_critical_path`
was used in 3 variant runs against a limit of 2.

## Per-arm results

Scenario paymentFailure, client cli, model requested `sonnet` (observed claude-sonnet-5), CLI 2.1.283, effort
xhigh, prompt `noskill` in both arms, 10 runs per arm.

| | baseline | descchange |
|---|---|---|
| Jaeger image | quay.io/jaegertracing/jaeger:2.20.0 | jaeger-mcp-evals/jaeger:desc-change-sep28-4c355981 |
| accuracy (PASS) | 10/10 | 10/10 |
| runs using get_trace_topology | 6/10 | 10/10 |
| runs using get_critical_path | 4/10 | 3/10 |
| median tool output chars | 38579 | 34458.5 (10.7% lower) |
| read_skill attempted | 10/10 | 10/10 |
| read_skill succeeded | 10/10 | 10/10 |
| read_skill as the first call | 8/10 | 0/10 |

## What was measured

The `noskill` prompt never names a skill, and the system prompt does not mention one. Both arms still read the
skill through `read_skill` in every run. The hypothesis is about agents working from tool descriptions alone; this experiment measured the
description change on agents that were already following the skill.

## Two batches

A batch runs only the arms whose image the fixture is serving, so each arm ran as its own batch:

- descchange: `batch-20260926T140333Z`, pinned 2026-09-26T14:03:33Z, cells 14:06:09Z to 14:10:32Z
- baseline: `batch-20260926T141252Z`, pinned 2026-09-26T14:12:52Z, cells 14:13:59Z to 14:18:25Z

Order within each batch is shuffled; order across arms is not, since the variant ran first. `fixture_overlay_sha256`
differs between the two manifests. It hashes `.env.override`, which sets the Jaeger image, so a difference
is expected; the hashed files were not compared line by line.

## Effort

Both batches ran at effort xhigh. Earlier pilot runs that set no effort opened differently, with
search_traces and get_services instead of read_skill. Two probes checked this, with the noskill prompt,
--max-turns 2, stock Jaeger 2.20.0 and no fault:

- records/probes/effort-cli-2026-09-26: CLI 2.1.278 and 2.1.283, effort unset vs xhigh, 5 runs each.
  read_skill was the first call in 0/5 unset and 5/5 at xhigh, on both versions.
- records/probes/effort-level-2026-09-26: CLI 2.1.283, 5 runs each. read_skill was the first call in 0/5
  at medium and 0/5 at high.

Effort xhigh, not the CLI version, puts read_skill in the first turn. The probes stop after two turns, so they
do not show whether a run below xhigh reads the skill later.

## Limits

- One fault, n=10 per arm. This tests the description change on paymentFailure only and says nothing about
  other faults.
- get_trace_topology went from 6/10 to 10/10. Two-sided Fisher exact p = 0.087, not significant at 0.05
  (`python3 harness/bench.py power 10`: with 6 of 10 in the baseline, only a variant count of 0 would be
  significant). get_critical_path 4/10 vs 3/10 gives p = 1.0.
- Both arms scored 10/10 on accuracy, so band reports code 3 for each: a rate of 1 calls for reading the
  trajectories before drawing anything from it.

## Next

A re-run at effort high will be a new version of the experiment, pre-registered separately. This version and
its records stay as they are.
