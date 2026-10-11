# A skill change on recommendationCacheFailure

One Gotcha bullet added to the error-root-cause skill shipped in Jaeger 2.20.0, with `search_traces` added to the skill's allowed-tools line, took claude-sonnet-5-5 from 0/10 to 7/10 PASS on one scenario (n=10 per arm, model confirmed on 20 of 20 trials). The protocol is in [docs/USAGE.md](../USAGE.md#protocol).

## The failure

The recommendation service keeps a list in memory that grows on every cache miss. Requests get slower, the process runs out of memory and restarts, and the list starts growing again. While it restarts, the frontend's calls to it fail.

- Scenario: recommendationCacheFailure, version 3
- Failing service: `recommendation`, operation `get_product_list`
- What the agent sees first: frontend spans failing with `14 UNAVAILABLE` or `ECONNREFUSED`, with no server span from recommendation
- Where the cause is: recommendation's own earlier spans, where `get_product_list` gets slower and `demo.product.count` grows
- PASS: names `recommendation` and the growing in-memory list as the mechanism
- PARTIAL: names `recommendation` but stops at the connection error or the restart

Why the stock skill misleads: it says "The deepest error span with no errored children is the most likely root cause." The crashed service has no span for the failed call, so the deepest error is the frontend's failed connection, and the agent stops there.

- Stock skill: locus PASS, mechanism incorrect, verdict PARTIAL in 10 of 10
- Changed skill: locus PASS in 10 of 10, mechanism correct in 7, verdict PASS in 7 and PARTIAL in 3

## The change

One Gotcha bullet in error-root-cause/SKILL.md, and `search_traces` added to its allowed-tools line (6 insertions, 1 deletion):

```diff
-allowed-tools: get_trace_errors get_trace_topology get_span_details
+allowed-tools: get_trace_errors get_trace_topology get_span_details search_traces
```

```diff
 - Multiple independent root causes can exist in a single trace.
+- A client span that failed to connect (connection refused, unavailable) and
+  has no server-side child is not the origin: the callee was down or
+  restarting. Use `search_traces` on the callee for the period before the
+  failure and `get_span_details` on its own spans to find why it went down,
+  such as spans getting slower or attribute values that stand out.
```

Patch: [harness/experiments/images/skill-callee-down.patch](../../harness/experiments/images/skill-callee-down.patch).

## Pre-registration

The threshold, at least 5 of 10 PASS in the change arm, is in the experiment file both batches ran from ([skill-callee-down-cli55.json](../../harness/experiments/skill-callee-down-cli55.json)). Each batch manifest records that file's sha256 (`d191f507...`), the harness commit it ran at (`a1431de`) and `harness_dirty: false`. That commit was made in a private working clone before the first batch started. In this repository the file first appears in a later commit, so the order cannot be checked from this repository's history alone.

## Results

n=10 trials per arm. CI is the Wilson 95% interval on the PASS rate.

| Experiment | Model, client | Baseline PASS | Change PASS | 95% CI (baseline, change) | Verdict |
|---|---|---|---|---|---|
| [skill-callee-down-cli55](../../records/skill-callee-down-cli55/RESULT.md) | claude-sonnet-5-5, Claude Code CLI 2.1.285, effort high | 0/10 (10 PARTIAL: right service, wrong mechanism) | 7/10 (3 PARTIAL) | [0.00, 0.28], [0.40, 0.89] | EXPERIMENT PASS |

The model was confirmed on 20 of 20 trials. In all 10 changed-skill trials the agent opened spans of the recommendation service; in no baseline trial did it ([NOTES.md](../../records/skill-callee-down-cli55/NOTES.md)). Both batches: 0 invalid, 0 leaks.

The changed skill makes each trial longer and costlier: a median of 22.5 turns (range 18 to 27) against 12 (range 11 to 14), a median of 20.5 tool calls against 10, and 2.77 USD against 0.77 USD for the ten trials (cost as reported by the CLI).

## The same pair on the skill text from Jaeger main

The pair was run again with the error-root-cause skill text from Jaeger main as of commit `b3e66888` (the text last changed in #9263) inside the 2.20.0 binary. This is not main's binary: main has since removed the v1 HTTP API, so a build of main could not serve the harness. Experiment file: [skill-callee-down-mainskill.json](../../harness/experiments/skill-callee-down-mainskill.json); image records: [baseline](../../harness/experiments/images/v2200-mainskill.json), [change](../../harness/experiments/images/v2200-mainskill-callee-down.json). The change is the one described above, applied to main's text; its patch is not in this repository.

Rule from the experiment file: at least 5 of 10 PASS in the change arm, and (decision rule in the hypothesis) the baseline at most 2 of 10. Run on 2026-10-06, same model, client, effort, scenario version and prompt as the table above.

| Experiment | Baseline PASS | Change PASS | 95% CI (baseline, change) | Verdict |
|---|---|---|---|---|
| [skill-callee-down-mainskill](../../records/skill-callee-down-mainskill/RESULT.md) | 0/10 (10 PARTIAL) | 6/10 (4 PARTIAL) | [0.00, 0.28], [0.31, 0.83] | EXPERIMENT PASS (change 6 >= 5, baseline 0 <= 2) |

Three fresh passes of the mechanism grader over the same ten answers per arm ([grader-reruns](../../records/skill-callee-down-mainskill/grader-reruns/)), counted as "correct" mechanism:

| | Stored grade | Re-run 1 | Re-run 2 | Re-run 3 |
|---|---|---|---|---|
| Baseline | 0/10 | 0/10 | 0/10 | 0/10 |
| Change | 6/10 | 6/10 | 5/10 | 6/10 |

A re-run grade differs from the stored one in one change-arm answer. The threshold of 5 holds in every pass.

Effort per arm, from `scores.jsonl` of each batch (ten trials; cost as reported by the CLI):

| | Median tool calls | Median turns | Median cost per trial | Cost, ten trials |
|---|---|---|---|---|
| Baseline | 11 (range 10 to 13) | 13 | 0.10 USD | 1.00 USD |
| Change | 18.5 (range 15 to 22) | 20.5 | 0.19 USD | 1.99 USD |

Caveats specific to this pair:

- The binary is 2.20.0, not main. Only the skill text matches main.
- One scenario, the same one the change was written against; the other caveats above apply unchanged.
- The mechanism grader and its re-runs used claude-fable-5-1, as recorded in each score row.

Reproduce the counts and effort figures offline:

```
python3 - <<'PY'
import json, statistics as s
R = "records/skill-callee-down-mainskill/"
for b in ["batch-20261006T115900Z", "batch-20261006T121535Z"]:
    sc = [json.loads(l) for l in open(R + b + "/scores.jsonl")]
    print(b, [x["verdict"] for x in sc].count("PASS"), "PASS;",
          "median tool_calls", s.median(x["tool_calls"] for x in sc),
          "turns", s.median(x["num_turns"] for x in sc),
          "cost", round(s.median(x["cost_usd"] for x in sc), 4), round(sum(x["cost_usd"] for x in sc), 2))
    rr = [json.loads(l) for l in open(R + "grader-reruns/" + b + ".jsonl")]
    print("  re-runs correct per pass:", [sum(r["label"] == "correct" for r in rr if r["pass"] == p) for p in (1, 2, 3)])
PY
```

## What was held fixed

Both arms used the same scenario version (3), prompt (`neutral`), tool set, fixture and grader; only the Jaeger image, and so the skill text, differed. The recorded factors are listed in [docs/RECORD.md](../RECORD.md) and [docs/TUNING.md](../TUNING.md). The arms ran as two separate batches.

## Caveats

- One scenario. The change was written after failures on this scenario were observed, so a held-out scenario is needed before calling it general.
- The baseline is the skill as shipped in 2.20.0. Jaeger's main branch has since rewritten this skill (#9263), so the change has not yet been measured against main.
- The two edits were measured together. Baseline trials already called `search_traces` (10 of 10), so the allowed-tools line did not gate that tool in this client, and the experiment does not show which edit matters.
- The mechanism is graded by one pinned model grader, validated against 21 human-labeled items ([docs/TUNING.md](../TUNING.md)).

## Reproduce

1. `make setup` brings up the fixture ([fixture/FIXTURE.md](../../fixture/FIXTURE.md)).
2. Build the variant image: `git am` the [patch](../../harness/experiments/images/skill-callee-down.patch) onto a Jaeger checkout at v2.20.0, build the image as in FIXTURE.md's "Build a variant" (`fixture/build-variant.sh` applies tool descriptions only, not skill text), and tag it as in [skill-callee-down.json](../../harness/experiments/images/skill-callee-down.json).
3. `python3 harness/bench.py run harness/experiments/skill-callee-down-cli55.json`
4. `python3 harness/judge.py <baseline batch> <variant batch>`; `python3 harness/bench.py verify` re-scores the raw trajectories.
