# Adding a scenario

How to add or edit a `harness/scenarios/*.json` file. Running batches and reading
results are in docs/USAGE.md.

## Example

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

## The file

Keys, as in the example above:

- `flag`: the flagd flag this scenario activates.
- `activation`: `{field, value}` written to `src/flagd/demo.flagd.json` for that flag.
- `version`: required integer, bumped under the changelog rule below.
- `deterministic`: `yes`, `no` or `bucketed`; `no` stays out of the certified pool
  (docs/USAGE.md, Reading results).
- `ground_truth`: `{service, operation, mechanism}`, the answer key in prose.
- `signal_regex`: text in the fault trace and absent from the baseline.
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
- `notes`: free text; `score.py` never reads it.
- `expected_restarts` (optional): containers the fault is allowed to restart. A restart, OOM kill
  or start-time change in any other container during a trial makes it INVALID.
- `signal_after_restart` (optional): a container name. Trials start only after that container's
  first restart under the fault; the batch aborts if the restart never comes.
- `restart_before_run` (optional): containers that pre-flight restarts, followed by Jaeger, so the
  trace store never holds that restart.

## The evidence traces

Captured by hand into `harness/scenarios/evidence/<scenario>.trace.json` (flag on) and `<scenario>.baseline.json` (flag off); no `bench.py` code writes them.
Flip the flag, confirm over OFREP, wait for fresh traces, then pull one trace of each.
Flag flip: fixture/FIXTURE.md. Wait and confirm sequence: docs/USAGE.md, Run a batch.

## Gates before a pilot

All three must be green before a scenario's first pilot batch:

- `python3 harness/bench.py leak <prompt files>`: no word from
  `harness/leak-words.txt` in any prompt or system prompt the agent sees.
- `python3 harness/bench.py readiness <scenario>`: `version` is a positive
  integer, `mechanism_truth` is set, `oracle` is a non-empty list, and the fault trace matches
  `signal_regex` while the baseline doesn't.
- `python3 harness/bench.py oracle <scenario>` against the live fixture: the
  `oracle` call sequence returns the signal through the served MCP tools.

## Changelog rule

An edit that changes what PASS means or which trajectories are comparable to
earlier runs bumps `version` and gets a line in CHANGELOG.md: date, scenario, old
to new version, one line why, comparable yes or no.
