# Usage

See README.md, fixture/FIXTURE.md, docs/RECORD.md (recorded keys) and docs/TUNING.md (factors).

## Prerequisites

Python 3.11+, bash, git, Docker with Compose v2 and one agent client
(Clients below), here or on a remote host over ssh (clone this repo there, fixture/FIXTURE.md).
Ports 16686 (Jaeger query) and 8016 (flagd OFREP) must be reachable. `fixture.env` (copy of
`fixture.env.example`) holds the fixture location, `RUNS_DIR`, API keys and Codex command.

> [!WARNING]
> Every trial is one paid agent run: 20 for 10 per arm with two arms.

## Before you start

1. Start the fixture: 22 containers (23 with Phoenix), about 2.9 GB RAM, 6 busy cores. `make setup` runs `make preflight` first; `make up` waits up to 10 minutes, Phoenix one to two more; traces show after a few minutes of load.
   ```bash
   make setup
   ```

## The experiment file

`harness/experiments/<name>.json` is the complete configuration of a batch; copy one for your
own runs. Every key is required except `run.client_version`; unknown keys are refused. Below is `skill-callee-down-cli55.json`,
verbatim except that the long `hypothesis` is elided:

```json
{
  "name": "skill-callee-down-cli55",
  "version": 2,
  "scenario": "recommendationCacheFailure",
  "hypothesis": "...",
  "run": {
    "client": "cli",
    "provider": null,
    "model": "claude-sonnet-5-5",
    "effort": "high",
    "client_version": "2.1.285",
    "max_turns": 30,
    "max_budget_usd": 2,
    "n_per_arm": 10,
    "seed": null
  },
  "arms": {
    "baseline": {"prompt": "neutral", "image": "quay.io/jaegertracing/jaeger:2.20.0", "tools": true},
    "skillchange": {"prompt": "neutral", "image": "jaeger-mcp-evals/jaeger:skill-callee-down-da737d02", "tools": true}
  },
  "baseline_arm": "baseline",
  "thresholds": {"accuracy_pass_min": 5},
  "status": "CONFIRMED v1 2026-09-30: model pinned by full id; threshold pre-registered before any run"
}
```

| Key | Values | Meaning |
|---|---|---|
| `name`, `version`, `scenario`, `hypothesis` | string, `2`, a `harness/scenarios/` name, text | records land in `records/<name>/`; the hypothesis states what the thresholds test |
| `run.client`, `run.provider` | `api` `cli` `codex`; `anthropic` `openai` `null` | Clients below; provider is api only |
| `run.model`, `run.effort` | full id; `low` `medium` `high` `xhigh` `max` | the id is sent unchanged, the api client refuses aliases |
| `run.max_turns`, `run.max_budget_usd` | positive integer, positive number | API calls (api), CLI turns (cli), tool calls (codex); budget has no effect with openai or codex |
| `run.n_per_arm`, `run.seed`, `run.client_version` | integer, integer or `null`, optional string | trials per arm; `null` draws and records a seed; the batch refuses unless the client reports that version |
| `arms.<arm>` | `prompt`, `image`, `tools` | a `harness/prompts/<prompt>.txt` name; the Jaeger image it needs; `false` removes the MCP server |
| `baseline_arm`, `thresholds`, `status` | arm or `null`; object; text | what `judge.py` compares against; see Thresholds; a batch refuses while `status` contains `DRAFT` |

> [!IMPORTANT]
> Any edit changes the file's sha256. Record why in `status`, and note it in CHANGELOG.md if earlier batches stop being comparable.

### Clients

- `cli`: the logged-in `claude`, no API key in its environment.
- `codex`: `codex` (or `CODEX_BIN`) via `harness/codex_client.py`; MCP only, read-only sandbox, stops after `max_turns` tool calls.
- `api`: `harness/agent_loop.py` over the Messages API. Experimental, no batch in `records/`; key and base URL (fixture.env) bill that account and are never recorded.

## Run a batch

1. Dry run, from the repository root.
   ```bash
   python3 harness/bench.py run harness/experiments/<name>.json --dry-run
   ```
   > [!TIP]
   > The dry run is free: a read-only pre-flight that prints the plan and seed, never flips the flag or calls a model.
2. Run (`--first-only` runs one cell): flips the flag and confirms it through OFREP, waits for fresh fault traces, runs the cells shuffled, restores the flag, scores. Output goes to `$RUNS_DIR/<scenario>/batch-<UTC>/`; its records, never trajectories, are copied to `records/<name>/`.
   ```bash
   python3 harness/bench.py run harness/experiments/<name>.json
   ```
3. Verify: re-scores every `stream.jsonl`; fails on a disagreeing verdict, any INVALID or LEAK run, an answer with no cached grade, or a stale `records/INDEX.md` (`run` and `bench.py index` rewrite it).
   ```bash
   python3 harness/bench.py verify
   ```
4. Judge the baseline and variant batches (`$RUNS_DIR` or `records/` copies): PASS or FAIL per threshold, then `EXPERIMENT PASS` or `EXPERIMENT FAIL`; writes `records/<name>/RESULT.md`.
   ```bash
   python3 harness/judge.py <baseline_batch_dir> <variant_batch_dir>
   ```
5. Pack: writes `dist/<name>-trajectories.tar.gz` and `records/<name>/trajectories.sha256`; refuses (exit 1) on a file that would publish the fixture host, ssh user, home directory, key, base URL or session id.
   ```bash
   python3 harness/bench.py pack <name>
   ```

> [!WARNING]
> Step 2 spends money: one agent trial per cell, plus one grader call per new mechanism answer.

`run` exits 0 done; 1 bad file or pre-flight failure; 4 aborted after 3 consecutive cell failures;
6 interrupted or incomplete.

> [!CAUTION]
> Exit 5 means the flag was not confirmed back at its default. Check the fixture before anything else; this code wins over 4 and 6.

A batch aborts before the flag flips on:

| Cause | Detail |
|---|---|
| No arm matches | only arms whose `image` equals `docker inspect jaeger` run; run other images as separate batches |
| Description mismatch | a tools arm on another image must serve every `tools/list` description exactly as `harness/experiments/descriptions/desc-change.json` |
| Failed gate | leak scan, readiness, oracle (docs/SCENARIOS.md) or sandbox probe; `status` has `DRAFT`; wrong client version; mean `bi` of `vmstat 5 3` over 50000 |

## Protocol

Results published in this repo come from one protocol:

- Model: `claude-sonnet-5-5`, requested by that full id in `run.model`. Every trial's `model_asserted` in scores.jsonl is the model its stream reported, and the batch aborts after a cell that reports another one.
- Client: the Claude Code CLI with its built-in tools off (`--tools ""`) and only `mcp__jaeger__*` allowed.
- Effort `high`; max turns as the experiment file states (30 in skill-callee-down-cli55).
- 10 trials per arm (`n_per_arm`); with fewer, `bench.py band` and the results table say rank only.
- The arms, images and thresholds are in the experiment file, committed before the first trial; the manifest records `harness_git_sha` and `harness_dirty`. Arms on different images run as consecutive batches against the same fixture.
- A pair is discarded and re-run, not topped up, when:
  - any trial ran on another model;
  - the fixture changed outside the scenario's declared restarts (the trial scores INVALID);
  - the batch stopped before 10 trials (run exits 4 or 6);
  - the leak check is red (pre-flight aborts, or the trial scores LEAK);
  - a trial has no cached grade (`bench.py verify` fails).

## Thresholds

| Key | Checks, per test arm |
|---|---|
| `tool_used_min`, `tool_used_max` | runs using each named tool |
| `tool_output_chars_median_drop_pct_min` | percentage drop in median `tool_output_chars` against baseline |
| `accuracy_pass_min` | the PASS count |
| exit code | 0 all pass, 1 any fails, 2 refusal unless both manifests carry one experiment sha256 and `arm_pin` blocks matching the argument order |

## Reading results

> [!IMPORTANT]
> Below 10 cells per arm a result is a ranking, not a rate. A rate of 0 or 1 means read the trajectories for a leaky prompt or unfair assertion.

- Every number comes from `bench.py verify`.
- `bench.py band <batch_dir> [--arm A]` (a `$RUNS_DIR` directory; cached grades only) prints the Results table: verdict counts, `err`, `invalid`, `leak`, rate, Wilson 95% CI, medians. `err`, `invalid`, `leak` and abnormal stops count in n, never in PASS. Exit 0 certified, 2 when n is under 10, 3 when the rate is 0 or 1.
- `band` and `verify` flag runs with a compaction event (context numbers not comparable). Scenarios with `deterministic` set to no stay out of the certified pool. `bench.py power <n_per_arm>` gives the smallest significant variant PASS count (Fisher).

## Scoring

`python3 harness/score.py <trial_dir>` scores the final `result` event's `structured_output` (validated against `verdict-schema.json`).

| Field | Rule |
|---|---|
| `locus` | PASS, PARTIAL or FAIL from service and operation against `pass_rule` |
| `mechanism` | free text graded against `mechanism_truth` by the pinned grader (docs/TUNING.md, Mechanism grader): `correct` PASS, `incorrect` FAIL, `unclear` UNCLEAR (not a PASS); UNGRADED when no cached grade exists and the grader may not be called (every path except the end of `run`) |
| `cascade` | every `cascade_rule.required_any` group must match |
| `verdict` | PASS if locus and mechanism both PASS; UNGRADED if locus is PASS and mechanism is ungraded; PARTIAL if locus is PASS or PARTIAL but mechanism is not; ABSTAIN if the agent set `abstain`; else FAIL |
| INVALID | replaces the verdict when `sandbox_ok` is false, or `fixture_ok` is false (a container outside the scenario's `expected_restarts` restarted, was OOM-killed or changed `StartedAt`) |
| LEAK | replaces the verdict when `leak_hits` is not empty: the answer or any assistant text names a flag, `feature_flag` or `flagd` (docs/RECORD.md, scores.jsonl) |
| no valid `structured_output` | schema-invalid answer, max turns or crash: locus, mechanism and cascade are MISSING, verdict FAIL, `verdict_source` null |

## Export to Phoenix

1. Send each trial in the batch's `scores.jsonl` to Phoenix as one OTLP trace. Default endpoint `http://<FIXTURE_HOST>:<PHOENIX_PORT>`, project named after the experiment.
   ```bash
   python3 harness/bench.py export <RUNS_DIR>/<scenario>/<batch-id> [--endpoint URL]
   ```

> [!IMPORTANT]
> A `records/` copy is refused. Span timestamps are synthetic; only a trial's total duration is measured.

## What stays out of git

`stream.jsonl`, `stderr.txt` and the per-cell directories carry session identifiers and local paths.
They stay under `RUNS_DIR` (default `runs/`, gitignored) and are never committed; `records/` holds the
checked-in batch files. To share trajectories, attach the `bench.py pack` tarball to a release.
