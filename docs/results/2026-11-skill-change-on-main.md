# The callee-down skill change on Jaeger main, six scenarios

Two Gotcha bullets in the built-in skills took claude-sonnet-5-5 from 0/10 to 7/10 PASS on recommendationCacheFailure on images built from Jaeger main d22c65b9 (two-sided Fisher p = 0.003, n=10 per arm, model confirmed on 20 of 20 trials). On the other five scenarios no arm has a lower PASS count with the change. The protocol is in [docs/USAGE.md](../USAGE.md#protocol).

## The change

The bullet measured on 2.20.0 in [2026-09-recommendation-cache.md](2026-09-recommendation-cache.md), cherry-picked onto main, plus the same rule as a Gotchas bullet in the root SKILL.md (14 insertions, 1 deletion, two files):

```diff
+- A client span that failed to connect (connection refused, unavailable) and
+  has no server-side child is not the origin: the callee was down or
+  restarting. Use `search_traces` on the callee for the period before the
+  failure and `get_span_details` on its own spans to find why it went down,
+  such as spans getting slower or attribute values that stand out.
```

Patch: [main-d22c65b9-v1.patch](../../harness/experiments/images/main-d22c65b9-v1.patch). Image record: [main-d22c65b9-v1.json](../../harness/experiments/images/main-d22c65b9-v1.json).

The rule is in the root SKILL.md too because on main the agent reads the root skill in every run but does not open error-root-cause/SKILL.md (0/10 in [batch-20261103T102634Z](../../records/skill-callee-down-main-d22c65b9/batch-20261103T102634Z/)), so a bullet only in the sub-skill is not seen. In the twelve batches below the sub-skill was opened in 0 of 60 baseline trials and 4 of 60 change trials, none of them on recommendationCacheFailure.

## Pre-registration

The rule, two-sided Fisher p < 0.05 on PASS at n=10 per arm, is written in each committed experiment file before its runs (for example [v1-main-d22c65b9-recommendationCacheFailure.json](../../harness/experiments/v1-main-d22c65b9-recommendationCacheFailure.json)). The experiment files were confirmed before judge.py had a Fisher threshold (`fisher_p_max`, added 2026-11-06), so their thresholds block is empty, judge.py refuses them, and these pairs have no RESULT.md. The p values below are `bench.fisher_p`, computed from each batch's scores.jsonl.

## Results

n=10 trials per arm. CI is the Wilson 95% interval on the PASS rate. Calls are median tool calls per trial; cost is agent cost for the ten trials.

| Scenario (version, class) | Stock main PASS | Change PASS | 95% CI (stock, change) | p | Calls (stock, change) | Cost USD (stock, change) |
|---|---|---|---|---|---|---|
| recommendationCacheFailure (4, bucketed) | [0/10](../../records/v1-main-d22c65b9-recommendationCacheFailure/batch-20261105T160047Z/) (10 PARTIAL) | [7/10](../../records/v1-main-d22c65b9-recommendationCacheFailure/batch-20261105T161201Z/) (3 PARTIAL) | [0.00, 0.28], [0.40, 0.89] | 0.003 | 8, 23 | 0.56, 2.35 |
| paymentFailure (2, yes) | [10/10](../../records/v1-main-d22c65b9-paymentFailure/batch-20261105T200520Z/) | [10/10](../../records/v1-main-d22c65b9-paymentFailure/batch-20261105T201539Z/) | [0.72, 1.00], [0.72, 1.00] | 1 | 6, 10 | 0.74, 1.27 |
| paymentUnreachable (3, yes) | [10/10](../../records/v1-main-d22c65b9-paymentUnreachable/batch-20261105T163450Z/) | [10/10](../../records/v1-main-d22c65b9-paymentUnreachable/batch-20261105T164448Z/) | [0.72, 1.00], [0.72, 1.00] | 1 | 4.5, 6.5 | 0.42, 0.51 |
| cartFailure (2, yes) | [8/10](../../records/v1-main-d22c65b9-cartFailure/batch-20261105T191715Z/) (2 FAIL) | [9/10](../../records/v1-main-d22c65b9-cartFailure/batch-20261105T194259Z/) (1 FAIL) | [0.49, 0.94], [0.60, 0.98] | 1 | 24, 18.5 | 2.04, 1.92 |
| adFailure (2, stochastic) | [6/10](../../records/v1-main-d22c65b9-adFailure/batch-20261105T165512Z/) (4 PARTIAL) | [10/10](../../records/v1-main-d22c65b9-adFailure/batch-20261105T170641Z/) | [0.31, 0.83], [0.72, 1.00] | 0.09 | 5.5, 8 | 0.46, 0.84 |
| emailMemoryLeak (2, bucketed) | [9/10](../../records/v1-main-d22c65b9-emailMemoryLeak/batch-20261106T034017Z/) (1 PARTIAL) | [10/10](../../records/v1-main-d22c65b9-emailMemoryLeak/batch-20261106T035241Z/) | [0.60, 0.98], [0.72, 1.00] | 1 | 8.5, 18 | 0.54, 1.57 |

recommendationCacheFailure passes its rule. The other five are a regression check: at n=10 they cannot show the change is harmless, only that no PASS count went down, and the adFailure difference is not significant. All 120 trials report claude-sonnet-5-5 on Claude Code CLI 2.1.285 at effort high, with the fixture check and the sandbox check passing and no leak hits. `bench.py verify` re-scores every trial to its stored verdict.

The change arm makes more tool calls on five of six scenarios. On emailMemoryLeak, 25 of the change arm's calls returned an error, almost all of them `search_traces` SQL the server rejected (wrong column names, missing time bounds), each retried.

## What was held fixed

Per scenario, both arms share the scenario version, the neutral prompt, the full tool set, the fixture host and the grader (claude-fable-5-1); the only difference is the Jaeger image, `jaeger-mcp-evals/jaeger:main-d22c65b9` against `jaeger-mcp-evals/jaeger:main-d22c65b9-v1-403dc025`. The MCP instructions and tool schemas are identical across arms (pre-flight checks both). Each arm is its own batch, baseline first, with Jaeger recreated between them. All twelve batches ran on one host on 2026-11-05 and 2026-11-06. The recorded factors are listed in [docs/RECORD.md](../RECORD.md) and [docs/TUNING.md](../TUNING.md).

## Caveats

- Scenario fixes found on this host before any trial counted: paymentUnreachable v3 (the resolver error text differs by host), cartFailure v2 (the signal wait polls the rare EmptyCart operation), emailMemoryLeak v2 (10000x; at 1000x email crashed too rarely for the signal wait). Aborted batches are listed in [records/INDEX.md](../../records/INDEX.md) and count toward no result.
- Mechanism grades are from one grader pass; they were not re-graded as the 2.20.0 pair was.

## Reproduce

1. `make setup` brings up the fixture ([fixture/FIXTURE.md](../../fixture/FIXTURE.md)).
2. Build both images from a Jaeger checkout at d22c65b9, with and without the patch above, as the two image records describe ([main-d22c65b9.json](../../harness/experiments/images/main-d22c65b9.json), [main-d22c65b9-v1.json](../../harness/experiments/images/main-d22c65b9-v1.json)).
3. With the fixture on each image in turn: `python3 harness/bench.py run harness/experiments/v1-main-d22c65b9-<scenario>.json`
4. `python3 harness/bench.py verify` re-scores the raw trajectories; `bench.fisher_p(<stock PASS>, <change PASS>, 10)` gives p.
