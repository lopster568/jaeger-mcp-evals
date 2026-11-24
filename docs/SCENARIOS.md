# Scenarios

What each scenario tests, how to choose one, and how to add or edit a `harness/scenarios/*.json`
file. Running batches and reading results are in docs/USAGE.md.

## The scenarios

Baseline is the PASS count out of 10 trials on Jaeger main image main-d22c65b9, with the Claude Code CLI and claude-sonnet-5-5, run 2026-11-05/06 (`records/v1-main-d22c65b9-<scenario>/`, baseline arm). A measure needs no recorded baseline, because its own baseline arm is the comparison. The "can show" columns below hold for these recorded Claude baselines only: on another client or model the scenario's baseline may differ.

paymentFailure (deterministic: yes, baseline 10/10). Payment rejects every charge with an "invalid token" error. The error span, message and exception event sit on the leaf span; the checkout and frontend errors are cascading symptoms. It is the easiest scenario and the sanity floor.

paymentUnreachable (deterministic: yes, baseline 10/10). Checkout cannot resolve payment's address, so checkout's own client span fails and payment never has a span. The right answer is the caller (checkout), not the service named in the failing call. The resolver error text differs by host DNS (Docker Desktop or a GCP VM); the span is the same.

cartFailure (deterministic: yes, baseline 8/10). Cart's EmptyCart fails to reach its store while AddItem and GetCart work. Checkout discards the error and PlaceOrder returns OK, so the failure sits under an OK parent. Slow requests and frontend-proxy timeouts upstream are symptoms. Cart is restarted before a run.

emailMemoryLeak (deterministic: bucketed, baseline 9/10). Email grows memory on every request until it is OOM-killed and restarted. In-flight calls end on checkout's client span with EOF or connection reset, and no email server span exists for them. Memory is not in trace data; naming the crash and restart is enough. Restarts are expected during trials.

recommendationCacheFailure (deterministic: bucketed, baseline 0/10, all PARTIAL). Recommendation's own spans never set ERROR status. The signal is a span attribute (`demo.product.count`) that climbs past 1,000,000 and is visible only through get_span_details. The frontend slowdown appears only late. The service OOM-restarts in a cycle, and connection errors during restarts are symptoms. A batch waits for the first restart. Bucketed: the direction is certain, the rate depends on load.

adFailure (deterministic: no, baseline 6/10). Ad fails about 1 in 10 GetAds requests at random by design, even with the flag fully on, with a transient UNAVAILABLE status that the frontend passes up. It is noisy by construction and not a good target for measuring a change.

## Choosing a scenario

Pick the scenario whose trace pattern your change is about:

| Your change is about | Run | At N=10 it can show |
|---|---|---|
| reading span attributes beyond error status (`get_span_details`) | recommendationCacheFailure | better only |
| an error on the leaf span, with its message and exception event | paymentFailure | worse only |
| a failing client span whose server span never exists, so the caller is the cause | paymentUnreachable | worse only |
| a service that crashes and restarts, seen as dropped connections in its callers | emailMemoryLeak | worse only |
| an error that the caller swallows under an OK parent | cartFailure | worse only |

- To check that you broke nothing, run the scenario nearest to what you touched.
- A scenario at 6/10 or above can only catch a regression at N=10.
- adFailure is noisy by design; do not use it to measure a change.
- Raising `N` costs proportionally more trials.

What a measure can show at the default N=10, using the harness's two-sided Fisher exact test (`bench.fisher_p`) at 0.05:

| Baseline | Better | Worse |
|---|---|---|
| 0/10 | the change at 5/10 or more (p 0.0325) | not possible |
| 2/10 | the change at 8/10 or more (p 0.023; 7/10 gives 0.0698) | not possible |
| 6/10 | not possible (6 against 10 gives p 0.0867) | 0/10 |
| 8/10 | not possible (8 against 10 gives p 0.4737) | 2/10 or below |
| 9/10 | not possible | 3/10 or below |
| 10/10 | not possible | 5/10 or below |

At larger N: base 12/20 needs 19/20 to show better; base 16/20 cannot show better; base 18/30 needs 26/30; base 24/30 needs 30/30.

Both `make measure` records so far are on recommendationCacheFailure: `records/measure-recommendationCacheFailure-6aee67e4` (0/10 to 10/10) and `records/measure-recommendationCacheFailure-b6ad85f8` (2/10 to 10/10), both EXPERIMENT PASS (see their RESULT.md).

## Adding a scenario

### Example

[harness/scenarios/paymentFailure.json](../harness/scenarios/paymentFailure.json), the simplest scenario: the payment service rejects every charge, and the error span carries the message.

```json
{
 "flag": "paymentFailure",
 "version": 2,
 "activation": {
  "field": "defaultVariant",
  "value": "100%"
 },
 "deterministic": "yes",
 "ground_truth": {
  "service": "payment",
  "operation": "charge",
  "mechanism": "On every charge, payment/charge.js (lines 39-47 at tag 3.0.0) sets span attribute demo.user_context.loyalty_level=gold and throws Payment request failed. Invalid token. demo.user_context.loyalty_level=gold. checkout's PlaceOrder then returns gRPC Internal failed to charge card, and the frontend fails the checkout request; those are cascading symptoms, not the cause."
 },
 "mechanism_truth": "Every request is rejected by the called service's own application logic, which throws an error saying the credential or token presented is not valid; the errors seen in its callers are only a consequence.",
 "signal_regex": "Invalid token",
 "oracle": [{"tool": "search_traces", "arguments": {"service_name": "checkout", "with_errors": true}}, {"tool": "get_trace_errors", "arguments": {"trace_id": "$trace_id"}}],
 "pass_rule": {
  "service_exact": ["payment"],
  "operation_exact": ["charge", "oteldemo.PaymentService/Charge", "grpc.oteldemo.PaymentService/Charge"]
 },
 "cascade_rule": {
  "required_any": [
   ["checkout"]
  ]
 },
 "evidence": {
  "ground_truth_trace": "evidence/paymentFailure.trace.json",
  "baseline_absence": "evidence/paymentFailure.baseline.json"
 },
 "notes": "Easiest mechanism-tier scenario: the leaf error span carries the message, the attribute, and an exception event. Kept as the sanity floor. demo.user_context.loyalty_level is set on every charge span at baseline (platinum/gold/silver observed with the flag off), so neither the attribute nor the value gold discriminates; the signal is the ERROR status with message 'Payment request failed. Invalid token' and the exception event."
}
```

### The file

Keys, as in the example above:

- `flag`: the flagd flag this scenario activates.
- `activation`: `{field, value}` written to `src/flagd/demo.flagd.json` for that flag.
- `version`: required integer, bumped under the changelog rule below.
- `deterministic`: `yes`, `no` or `bucketed`; recorded and shown in
  `records/INDEX.md`, not used by `band` (docs/USAGE.md, Reading results).
- `ground_truth`: `{service, operation, mechanism}`, the answer key in prose.
- `signal_regex`: text in the fault trace and absent from the baseline. A fault must leave an error text in the spans, so a latency-only flag cannot be a scenario.
- `pass_rule`: `{service_exact, operation_exact}`, lists of accepted exact strings
  (case-insensitive, whitespace-trimmed equality).
- `mechanism_truth`: one plain sentence saying what fails and why, with no flag, service,
  file or code names. The grader (harness/grade.py) labels the agent's free-text mechanism
  against it, so rewording it changes what PASS means: bump `version`.
- `cascade_rule`: `{required_any}`, term groups the verdict's cascade list must match.
- `evidence`: `{ground_truth_trace, baseline_absence}`, paths to the captured traces,
  relative to `harness/scenarios/`.
- `oracle`: required non-empty list of `{tool, arguments}` MCP calls that
  `bench.py oracle` runs in order against the live fixture, proving the served
  tools reach the signal; an argument `"$trace_id"` expands to one call per trace
  id seen in an earlier call's output, and `"$span_id"` to one call per span id that
  earlier calls for the same trace printed (get_span_details takes at most 20 ids).
  The first call is the search; its arguments are compiled to a find_traces statement when the served tool takes only `sql`.
  In a batch run's pre-flight an oracle failure is a warning with a prefilled issue link, not an abort:
  the signal wait has already proved the fault is in the store, and the result is recorded as
  `preflight.oracle` (`PASS` or `FAIL`, with `oracle_message`) in the manifest; judge.py notes a FAIL in RESULT.md.
  The standalone `bench.py oracle` still exits 1.
- `notes`: free text; `score.py` never reads it.
- `expected_restarts` (optional): containers the fault is allowed to restart. A restart, OOM kill
  or start-time change in any other container during a trial makes it INVALID.
- `signal_after_restart` (optional): a container name. Trials start only after that container's
  first restart under the fault; the batch aborts if the restart never comes.
- `restart_before_run` (optional): containers that pre-flight restarts, followed by Jaeger, so the
  trace store never holds that restart.
- `signal_operation` (optional): a span name. The signal wait searches only traces with that operation,
  for a fault on a call too rare to appear among the newest 50 traces of `ground_truth.service`.

### The evidence traces

Captured by hand into `harness/scenarios/evidence/<scenario>.trace.json` (flag on) and `<scenario>.baseline.json` (flag off); no `bench.py` code writes them.
Flip the flag, confirm over OFREP, wait for fresh traces, then pull one trace of each.
Flag flip: fixture/FIXTURE.md. Wait and confirm sequence: docs/USAGE.md, Run a batch.

### Gates before a pilot

All three must be green before a scenario's first pilot batch:

- `python3 harness/bench.py leak <prompt files>`: no word from
  `harness/leak-words.txt` in any prompt or system prompt the agent sees.
- `python3 harness/bench.py readiness <scenario>`: `version` is a positive
  integer, `mechanism_truth` is set, `oracle` is a non-empty list, and the fault trace matches
  `signal_regex` while the baseline doesn't.
- `python3 harness/bench.py oracle <scenario>` against the live fixture: the
  `oracle` call sequence returns the signal through the served MCP tools.

### Changelog rule

An edit that changes what PASS means or which trajectories are comparable to
earlier runs bumps `version` and gets a line in CHANGELOG.md: date, scenario, old
to new version, one line why, comparable yes or no.
In jaegertracing/jaeger-ai-evals, `verify` scores batches made before the import against the current scenario file (their `harness_git_sha` is not in this history), so a bump with comparable: no makes verify fail on their trajectories, which is expected.
