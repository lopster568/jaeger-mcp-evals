# skill-callee-down-cli55: notes

Scenario: recommendationCacheFailure v3. Frontend calls to the recommendation service fail with
connection errors. The service crashed and restarted, so the failed call has no server span; the cause
is in the service's own earlier spans.

The change is one Gotcha bullet added to error-root-cause/SKILL.md and `search_traces` added to its
allowed-tools line (patch: harness/experiments/images/skill-callee-down.patch, 6 insertions, 1 deletion). The bullet:

    - A client span that failed to connect (connection refused, unavailable) and
      has no server-side child is not the origin: the callee was down or
      restarting. Use `search_traces` on the callee for the period before the
      failure and `get_span_details` on its own spans to find why it went down,
      such as spans getting slower or attribute values that stand out.

Result, n=10 per arm, from scores.jsonl:

- baseline (stock 2.20.0 skill): 0 PASS, 10 PARTIAL
- change: 7 PASS, 3 PARTIAL
- pre-registered threshold: at least 5 PASS. Measured 7: EXPERIMENT PASS (RESULT.md).

Run settings: model claude-sonnet-5-5 requested by full id and reported by every trial
(model_asserted), Claude Code CLI 2.1.285, effort high, 10 trials per arm, 0 invalid, 0 leaks.

Trajectories: all 10 changed-skill trials opened spans of the crashed service with get_span_details;
none of the 10 baseline trials did (counted from the raw stream.jsonl files, which are not in this repo).

Caveats: one scenario, and the change was written after failures on it were observed. The two arms ran
in separate batches, not interleaved. Full list: docs/results/2026-09-recommendation-cache.md.
