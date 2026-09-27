# Adding a scenario

How to add or edit a `harness/scenarios/*.json` file. Running batches and reading
results are in docs/USAGE.md.

## The file

Keys, from `harness/scenarios/paymentFailure.json`:

- `flag`: the flagd flag this scenario activates.
- `activation`: `{field, value}` written to `src/flagd/demo.flagd.json` for that flag.
- `version`: required integer, bumped under the changelog rule below.
- `deterministic`: `yes`, `no` or `bucketed`; `no` stays out of the certified pool
  (docs/USAGE.md, Reading results).
- `ground_truth`: `{service, operation, mechanism}`, the answer key in prose.
- `signal_regex`: text in the fault trace and absent from the baseline.
- `pass_rule`: `{service_exact, operation_exact}`, lists of accepted exact strings
  (case-insensitive, whitespace-trimmed equality).
- `expected_mechanism`: the shared mechanism enum value this fault matches.
- `accepted_mechanisms`: optional extra mechanism values that also PASS (an
  equality check in score.py's `evaluate_mechanism_structured`, not a regex).
- `cascade_rule`: `{required_any}`, term groups the verdict's cascade list must match.
- `evidence`: `{ground_truth_trace, baseline_absence}`, paths to the captured traces,
  relative to `harness/scenarios/`.
- `oracle`: required non-empty list of `{tool, arguments}` MCP calls that
  `bench.py oracle` runs in order against the live fixture, proving the served
  tools reach the signal; an argument `"$trace_id"` expands to one call per trace
  id seen in an earlier call's output, and `"$span_id"` to one call per span id that
  earlier calls for the same trace printed (get_span_details takes at most 20 ids).
- `notes`: free text; `score.py` never reads it.

## The evidence traces

Captured by hand into `harness/scenarios/evidence/<scenario>.trace.json` (flag on) and `<scenario>.baseline.json` (flag off); no `bench.py` code writes them.
Flip the flag, confirm over OFREP, wait for fresh traces, then pull one trace of each.
Flag flip: fixture/FIXTURE.md. Wait and confirm sequence: docs/USAGE.md, Run a batch.

## Gates before a pilot

All three must be green before a scenario's first pilot batch:

- `python3 harness/bench.py leak <prompt files>`: no word from
  `harness/leak-words.txt` in any prompt or system prompt the agent sees.
- `python3 harness/bench.py readiness <scenario>`: `version` is a positive
  integer, `oracle` is a non-empty list, and the fault trace matches
  `signal_regex` while the baseline doesn't.
- `python3 harness/bench.py oracle <scenario>` against the live fixture: the
  `oracle` call sequence returns the signal through the served MCP tools.

## Changelog rule

An edit that changes what PASS means or which trajectories are comparable to
earlier runs bumps `version` and gets a line in CHANGELOG.md: date, scenario, old
to new version, one line why, comparable yes or no.
