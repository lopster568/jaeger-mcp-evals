# <What changed> on <scenario>

<One sentence: the change, the scenario, baseline PASS count to change PASS count, n per arm, model confirmed on how many trials.> The protocol is in [docs/USAGE.md](../USAGE.md#protocol).

## The failure

<Two or three sentences: what breaks in the service and what that does to its callers.>

- Scenario: <name>, version <n>
- Failing service: `<service>`, operation `<operation>`
- What the agent sees first: <the symptom spans and their error text>
- Where the cause is: <the spans and attributes that show it>
- PASS: <what the answer must name>
- PARTIAL: <what a right-service, wrong-mechanism answer looks like>

Why the baseline misleads: <the instruction or tool output that sends the agent the wrong way, quoted>.

- Baseline: locus <PASS/PARTIAL/FAIL>, mechanism <correct/incorrect>, verdict <VERDICT> in <k> of <n>
- Change: locus <...> in <k> of <n>, mechanism correct in <k>, verdict PASS in <k> and PARTIAL in <k>

## The change

<One line: which file, how many insertions and deletions.>

```diff
-<removed line>
+<added line>
```

Patch: [<path>](../../harness/experiments/images/<name>.patch).

## Pre-registration

The threshold, <rule>, was fixed in the committed experiment file before the run ([<experiment>.json](../../harness/experiments/<experiment>.json)).

## Results

n=<n> trials per arm. CI is the Wilson 95% interval on the PASS rate.

| Experiment | Model, client | Baseline PASS | Change PASS | 95% CI (baseline, change) | Verdict |
|---|---|---|---|---|---|
| [<experiment>](../../records/<experiment>/RESULT.md) | <model>, <client and version>, effort <effort> | <k>/<n> (<breakdown>) | <k>/<n> (<breakdown>) | [<lo>, <hi>], [<lo>, <hi>] | <EXPERIMENT PASS or FAIL> |

<Model confirmed on k of n trials. Invalid and leak counts per batch. One sentence on what the agent did differently, counted from the trajectories ([NOTES.md](../../records/<experiment>/NOTES.md)).>

## What was held fixed

<Scenario version, prompt, tool set, fixture and grader shared by both arms; the one thing that differed; whether the arms ran as separate batches.> The recorded factors are listed in [docs/RECORD.md](../RECORD.md) and [docs/TUNING.md](../TUNING.md).

## Caveats

- <How many scenarios, and whether the change was written after seeing failures on them.>
- <Which Jaeger version the baseline is, and what has changed upstream since.>
- <Grader: which model, how it was validated.>

## Reproduce

1. `make setup` brings up the fixture ([fixture/FIXTURE.md](../../fixture/FIXTURE.md)).
2. <How to build the variant image and which image record names its tag.>
3. `python3 harness/bench.py run harness/experiments/<experiment>.json`
4. `python3 harness/judge.py <baseline batch> <variant batch>`; `python3 harness/bench.py verify` re-scores the raw trajectories.
