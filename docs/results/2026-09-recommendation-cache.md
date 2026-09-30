# One skill bullet on recommendationCacheFailure

A one-bullet addition to Jaeger's error-root-cause skill took claude-sonnet-5-5 from 0/10 to 7/10 PASS on one scenario, and took claude-sonnet-5 without reasoning from 0/10 to 3/10.

## Scenario and why the stock skill misleads

recommendationCacheFailure v3: a list inside the recommendation service grows with every cache miss, requests slow down, and the process runs out of memory and restarts. What the agent sees are frontend calls to recommendation failing with `14 UNAVAILABLE` connection errors.
The stock skill says "The deepest error span with no errored children is the most likely root cause." Here the crashed callee has no span for the failed call, so the deepest error is the caller's failed connection, and the agent stops there (PARTIAL: right service, wrong cause).
The cause is in the recommendation service's own earlier spans, where `get_product_list` gets slower and `demo.product.count` grows.

## The change

One bullet in error-root-cause/SKILL.md, plus `search_traces` in its allowed-tools line:

    - A client span that failed to connect (connection refused, unavailable) and
      has no server-side child is not the origin: the callee was down or
      restarting. Use `search_traces` on the callee for the period before the
      failure and `get_span_details` on its own spans to find why it went down,
      such as spans getting slower or attribute values that stand out.

Patch: [harness/experiments/images/skill-callee-down.patch](../../harness/experiments/images/skill-callee-down.patch).

## Pre-registration

The threshold, at least 5 of 10 PASS in the change arm, was fixed in the committed experiment file before the run ([skill-callee-down-cli55.json](../../harness/experiments/skill-callee-down-cli55.json), [skill-callee-down-api.json](../../harness/experiments/skill-callee-down-api.json)).

## Results

n=10 trials per arm. CI is the Wilson 95% interval on the PASS rate.

| Experiment | Model, client | Baseline PASS | Change PASS | 95% CI (baseline, change) | Verdict |
|---|---|---|---|---|---|
| [skill-callee-down-cli55](../../records/skill-callee-down-cli55/RESULT.md) | claude-sonnet-5-5, Claude Code CLI 2.1.285, effort high | 0/10 (10 PARTIAL) | 7/10 (3 PARTIAL) | [0.00, 0.28], [0.40, 0.89] | EXPERIMENT PASS |
| [skill-callee-down-api](../../records/skill-callee-down-api/RESULT.md) | claude-sonnet-5, harness agent loop, no reasoning | 0/10 (9 PARTIAL, 1 FAIL) | 3/10 (5 PARTIAL, 2 FAIL) | [0.00, 0.28], [0.11, 0.60] | EXPERIMENT FAIL |

In the CLI experiment all 10 changed-skill trials opened spans of the recommendation service; no baseline trial did ([NOTES.md](../../records/skill-callee-down-cli55/NOTES.md)). Both CLI batches: 0 invalid, 0 leaks.

## What was held fixed

Within each experiment both arms used the same scenario version (3), prompt (`neutral`), tool set, fixture and grader; only the Jaeger image, and so the skill text, differed. The recorded factors are listed in [docs/RECORD.md](../RECORD.md) and [docs/TUNING.md](../TUNING.md). The two experiments differ in model, client and reasoning setting, so their rows are not a like-for-like comparison. The CLI arms ran as two separate batches.

## Caveats

- One scenario. The bullet was written after failures on this scenario were observed, so a held-out scenario is needed before calling it general.
- The effect is much weaker on claude-sonnet-5 without reasoning: 3/10 misses the threshold.
- The mechanism is graded by one pinned model grader, validated against 21 human-labeled items ([docs/TUNING.md](../TUNING.md)).

## Reproduce

1. `make setup` brings up the fixture ([fixture/FIXTURE.md](../../fixture/FIXTURE.md)).
2. Build the variant image: `git am` the [patch](../../harness/experiments/images/skill-callee-down.patch) onto a Jaeger checkout at v2.20.0, build the image as in FIXTURE.md's "Build a variant" (`fixture/build-variant.sh` applies tool descriptions only, not skill text), and tag it as in [skill-callee-down.json](../../harness/experiments/images/skill-callee-down.json).
3. `python3 harness/bench.py run harness/experiments/skill-callee-down-cli55.json`
4. `python3 harness/judge.py <baseline batch> <variant batch>`; `python3 harness/bench.py verify` re-scores the raw trajectories.
