# Scenario changelog

Log a scenario's `version` bump here the same sitting (bump rule: docs/SCENARIOS.md, Changelog rule).

## 2026-09-25

- paymentFailure: version none to 1, comparable: yes. First version tag, no scoring change.
- adFailure: version none to 1, comparable: yes. First version tag, no scoring change.
- paymentUnreachable: version none to 1, comparable: yes. First version tag, no scoring change.

## 2026-09-26

- Fixture: overlay comments in env.override, compose.overlay.yaml and otelcol-config-extras.yml neutralised (names removed), no functional change.
- paymentFailure: version 1 unchanged, evidence paths moved into harness/scenarios/evidence/, scenario sha256 changes, comparable: yes.
- adFailure: version 1 unchanged, evidence paths moved into harness/scenarios/evidence/, scenario sha256 changes, comparable: yes.
- paymentUnreachable: version 1 unchanged, evidence paths moved into harness/scenarios/evidence/, scenario sha256 changes, comparable: yes.
- Run settings: `bench.py batch` and its flags are replaced by `bench.py run <experiment-file>`; client, provider, model, effort, max turns, budget, trials per arm and seed live in the experiment file (version 2), and `BENCH_*_DEFAULT` in fixture.env and `bench.py config` are gone. Record schema 4: the manifest carries the experiment file and its sha256 under `experiment`, `out_dir` is the cell directory name, and the batch records are copied to `records/`. Records written before this change are not read by this code.
- Scoring: the prose fallback and `read_skill_called` are removed. A run with no valid structured verdict now scores MISSING and verdict FAIL (it was graded by regex over its text, which could PASS only through five labelled lines the system prompt no longer asks for).
- paymentFailure, adFailure, paymentUnreachable: version 1 unchanged, `legacy_mechanism_rule` and `abstain_regex` removed (read only by the prose fallback), scenario sha256 changes, comparable: yes.
- desc-change-sep28: experiment file version 2, run settings moved into it (client cli, model sonnet, effort xhigh, max turns 30, budget 2, 10 per arm); hypothesis, arms and thresholds unchanged.
- paymentFailure, adFailure, paymentUnreachable: version 1 unchanged, `pass_rule.service_regex`, `operation_regex` and `cascade_regex` removed (score.py read the first two only when an `_exact` list was absent, which no scenario had, and never read `cascade_regex`), so what PASS means is unchanged; scenario sha256 changes, comparable: yes.
- Fixture: compose.overlay.yaml gains a `phoenix` service under the Compose profile `store` (the trajectory store, off by default), so `fixture_overlay_sha256` changes once; no run behaviour changes. `bench.py export` sends a batch's trajectories to it.
- desc-change-sep28: experiment file version 2 ran at effort xhigh; batches recorded without an effort value are not comparable with it.
- Prompt `skill`: `read_skill` is now told the file path `error-root-cause/SKILL.md` instead of the skill name, which agents passed as a directory path and got an error, confounding every skill arm. Its prompt sha256 changes; skill-arm batches before this change are not comparable with later ones.

## 2026-09-27

- Fixture: `filter/deflag` in otelcol-config-extras.yml also drops spans named `^feature_flag\.`, because the load generator emits a `feature_flag.evaluate` span that tells the agent a flag was looked up; `fixture_overlay_sha256` changes once. Not yet applied to the fixture (fixture/apply-deflag.sh).
- Scoring: every scores.jsonl row gains `sandbox_ok` and `sandbox_violations`, and a run that fails the sandbox check scores INVALID (docs/RECORD.md). A refused `mcp__jaeger__StructuredOutput` call is no longer counted as a Jaeger call. Comparable: yes for sandbox-clean runs; a run with any violation, a refused call included, is now INVALID where it was PASS, PARTIAL, FAIL or ABSTAIN.
- Pre-flight: the cli client gets a sandbox probe, recorded as `sandbox_probe` in preflight.json; a failed probe aborts before the flag flip.
- Leak gate: `leak-words.txt` gains OpenTelemetry Demo, otel demo, opentelemetry-demo and astronomy shop. The recorded prompts noskill, skill, vague and slow name the demo and now fail the gate; they stay unchanged for the records that cite them. New prompt `neutral` is noskill with the system and symptom sentences made generic.
- screen-recommendationcache: its one arm is now `neutral` on prompt `neutral`; batches run on the `slow` prompt are not comparable with it.
- Protocol v2, mechanism: `verdict-schema.json` drops the mechanism menu; `mechanism` is free text ("how the failure happens, as evidenced by span data") and `mechanism_detail` is gone. `system-prompt.txt` loses the menu and its definitions; abstain is `abstain=true` alone. Its sha256 and the schema sha256 change.
- Protocol v2, grading: `harness/grade.py` labels the free-text mechanism against the scenario's new `mechanism_truth` with the pinned model claude-fable-5-1 and `harness/grader-prompt.txt`, through the sandboxed Claude Code CLI, cached per batch in `grades.jsonl`. Mechanism PASS iff the label is `correct`; `unclear` is not a PASS (PARTIAL with a correct locus). `verify` never calls the grader and fails on a cache miss.
- Protocol v2, leak tripwire: a run whose answer or assistant text names a flag, `feature_flag` or `flagd` scores LEAK (`leak_hits`), counted apart in band, INDEX and judge like INVALID, and fails `verify`.
- Protocol v2, pre-flight: `leak_rendered` scans each arm's prompt and the system prompt as rendered into the argv and the captured `tools/list` text (the stock phrase `error flag` exempt); a hit aborts before the flag is touched.
- Record schema 5: meta.json and the manifest gain `flag_names`; the manifest gains `grader` (model, prompt sha256, CLI version); scores.jsonl gains `mechanism_grade`, `mechanism_grade_reason`, `grader_model`, `grader_prompt_sha256`, `grader_cached`, `leak_hits`; INDEX gains LEAK and `record schema` columns. Batches recorded under schema 4 or older are reported with their stored scores ("legacy schema, stored scores") and never re-graded.
- paymentFailure: version 1 to 2, comparable: no. PASS now needs the graded free-text mechanism, not the menu value `invalid_token`; `expected_mechanism` removed, `mechanism_truth` added.
- adFailure: version 1 to 2, comparable: no. Same change; `expected_mechanism` and `accepted_mechanisms` removed.
- paymentUnreachable: version 1 to 2, comparable: no. Same change; `expected_mechanism` and `accepted_mechanisms` (three menu values) removed.
- recommendationCacheFailure: version 1 to 2, comparable: no. Same change; `expected_mechanism` removed.

## 2026-09-28

- Fixture stability: `run_cell` snapshots every container's RestartCount, OOMKilled and StartedAt
  (`docker inspect`) before and after each trial; a change to a container not in the scenario's
  new optional `expected_restarts` makes `fixture_ok` false and scores the trial INVALID, the
  same path `sandbox_ok` false takes (docs/USAGE.md, Scoring). Pre-flight also runs `vmstat 5 3`
  and `cat /proc/loadavg` once and aborts before the flag is touched if the host is thrashing
  (mean `bi` over the non-first samples over 50000); load average is recorded only.
- Signal gate: a scenario can set `signal_after_restart` (a container name) so a batch waits for
  that container's first restart after the fault is confirmed live, before any trial starts,
  bounded by the same poll budget as the signal wait.
- recommendationCacheFailure: version 2 to 3, comparable: no. `mechanism_truth` and
  `ground_truth.mechanism` revised to state the OOM-restart cycle plainly (the v2 claim "no
  request returns an error" held only until the first OOM); `expected_restarts: ["recommendation"]`
  and `signal_after_restart: "recommendation"` added, so a batch now waits for the recommendation
  container's first restart before running trials. batch-20260928T101313Z and
  batch-20260928T172256Z (scenario v2) are not comparable: the restart fell inside their trials.
- screen-recommendationcache: status text updated to say it runs scenario v3; run settings and
  the experiment file's own `version` (fixed at 2, the experiment-file schema) are unchanged.

## 2026-09-29

- Fixture: memory limits raised for load-generator (1G), ad (600M), quote (80M) and fraud-detection (600M), which thrashed the page cache at their old limits. Batches before this change ran with the old limits.
- Fixture: opensearch, prometheus, grafana, opamp-server, flagd-ui and telemetry-docs are switched off (Compose profile `unused`) and the collector no longer exports to them. The fixture is 22 containers (23 with Phoenix); batches before this change ran 28. `MIN_CONTAINERS` is 19 (was 25). No span content or scenario changes; `fixture_overlay_sha256` changes.
- recommendationCacheFailure: version 3 (protocol v2 grading, per-trial fixture stability check, restart signal gate, restart before run without Jaeger recording it). The API-client records first published with this version were removed on 2026-09-30 (see below).

## 2026-09-30

- Records: skill-callee-down-cli55 (claude-sonnet-5-5 pinned by full id, Claude Code CLI 2.1.285, effort high, recommendationCacheFailure v3): stock skill 0/10 PASS, one Gotcha bullet and `search_traces` in allowed-tools 7/10, EXPERIMENT PASS against the pre-registered threshold of 5. Results page: docs/results/2026-09-recommendation-cache.md. No scenario or scoring change.
- Records: the repo publishes one protocol only (claude-sonnet-5-5 by full id, Claude Code CLI, effort high, 10 trials per arm; docs/USAGE.md, Protocol). Batches recorded on an alias or the API client are removed. Harness: the container snapshot is scoped to the demo's compose project, and INVALID warnings name the failed check. No scenario or scoring change.
- Harness: the description gate reads image records whose `descriptions` is null; `reasoning_effort` is passed through on the api client, and a run whose reported model differs from the pinned one is refused.

## 2026-10-06

- Corrections, no scenario, scoring or record change: the 2026-09-29 entry cited two API-client records that were removed on 2026-09-30, and that citation is gone. Results page (docs/results/2026-09-recommendation-cache.md): the pre-registration section says what this repository's history can and cannot show, the cost and turn difference between the arms is stated, and one caveat is added.

## 2026-10-11

- Records: skill-callee-down-mainskill (same model, client and effort, recommendationCacheFailure v3, 2.20.0 binary with the error-root-cause skill text from Jaeger main): that skill 0/10 PASS, with the change 6/10, EXPERIMENT PASS against the pre-registered threshold of 5. Results page: docs/results/2026-09-recommendation-cache.md. No scenario or scoring change.

## 2026-10-12

- Harness: traces are read from `/api/v3/traces` (and `/api/v3/services` in fixture_leak), which Jaeger 2.20.0 and later serve; the v1 `/api/traces` was removed in 2.21.0. No scenario scoring change.
- recommendationCacheFailure: version 3 unchanged, `signal_regex` also matches the v3 attribute form (`"value": {"intValue": "N"}`), scenario sha256 changes, comparable: yes, no scoring change.

## 2026-10-15

- Fixture: the flagd memory limit is raised from the demo's 75M to 200M. At 75M flagd's own executable pages are evicted and re-read from disk until it stops answering; on 2026-10-15 a fresh `make setup` hit this within minutes. GOMEMLIMIT is unchanged; no flag, image or span differs. `fixture_overlay_sha256` changes, and batches before this change ran with the old limit.

## 2026-10-19

- Tooling: `make` targets print one line per step with live progress, in the same visual language as bench.py (colour off on a pipe, with NO_COLOR or TERM=dumb); the setup login check ignores ANTHROPIC_API_KEY, as the trials do. No scenario, experiment or scoring change, so every earlier batch stays comparable.
- Records: a `smoke-paymentfailure` batch (make smoke) is no longer copied into records/ and no longer rewrites records/INDEX.md; it checks that the fixture works and is not a result. No scenario or scoring change.

## 2026-10-28

- emailMemoryLeak: version none to 1, comparable: no prior version in this repo. Ported from the private screening clone where v1 was drafted on 2026-10-04, with its two evidence files; what PASS means is unchanged from that draft, and only the notes changed (this repo's bench.py line numbers, and the 2026-10-06 screen that aborted at the signal wait before any trial). Not yet run to a trial on the fixture.
- cartFailure: version none to 1, comparable: no prior version. New scenario: every request to empty a cart fails to connect to its store and the caller discards the error. Written from the 3.0.0 source; evidence traces not yet captured, so readiness fails until they are.

## 2026-11-03

- Tooling: pre-flight records the MCP server's initialize instructions (sha256 in the manifest) and leak-scans them with the tools. An image record with an `instructions` key names the expected file under `harness/experiments/instructions/`, and a variant arm aborts before the flag flips if the fixture serves different text. No scenario or scoring change.

## 2026-11-05

- recommendationCacheFailure: version 3 to 4, comparable: yes. The oracle's `search_traces` call adds `search_depth: 100`, so pre-flight finds the slow pre-restart traces instead of only the newest ten. Pre-flight only; what PASS means is unchanged.
- cartFailure: evidence traces captured on Jaeger main 238c5fa2, so readiness passes. No version change.
- paymentUnreachable: version 2 to 3, comparable: yes. `signal_regex` also matches `lookup badAddress`, the resolver error text on hosts whose DNS answers SERVFAIL (Docker on a cloud VM) instead of NXDOMAIN. Signal wait only; what PASS means is unchanged.

## 2026-11-06

- Tooling: optional scenario key `signal_operation` narrows the signal wait's trace search to one span name, for a fault on an operation too rare to show up among the newest 50 traces of its service. No scoring change.
- cartFailure: version 1 to 2, comparable: yes. Adds `signal_operation: POST /oteldemo.CartService/EmptyCart`; v1 could never pass its own signal wait. What PASS means is unchanged, and v1 ran no trial.
- emailMemoryLeak: version 1 to 2, comparable: no. Activation 1000x to 10000x and `signal_wait_s` 900 to 600. At 1000x email was OOM-killed only every 15 to 25 minutes, too rarely for the signal wait; at 10000x it is killed on almost every request with the same dropped connections. What PASS means is unchanged; v1 ran no trial.
- Tooling: `harness/measure.sh` (`make measure`) builds a Jaeger checkout's HEAD and base, runs one scenario on each and judges the pair; `fixture/build-image.sh` and `fixture/swap-jaeger.sh` are its steps, and `build-variant.sh` now calls `build-image.sh`. `judge.py` gains the threshold `fisher_p_max` and refuses (exit 2) an experiment whose `thresholds` block is empty, which it used to pass vacuously. No scenario or scoring change, comparable: yes.

## 2026-11-07

- Tooling: Jaeger main renamed the `search_traces` tool to `find_traces` (jaeger#10073, same arguments). When a fixture serves `find_traces` and not `search_traces`, the scenario oracles' `search_traces` calls run as `find_traces`; older images are unchanged. No scenario version change: the oracle is a pre-flight check and what PASS means is unchanged.
- Tooling: `measure.sh` reads bench.py's Batch line from stderr (it stopped after every baseline arm) and sets `"descriptions": null` on both image records (the variant arm aborted at pre-flight against desc-change.json). First end-to-end `make measure` run: records/measure-recommendationCacheFailure-6aee67e4, jaeger#10068 on main 5319091a, 0/10 to 10/10 PASS. No scenario version change.

## 2026-11-09

- Tooling: an oracle failure in a batch's pre-flight no longer aborts the batch. It logs a warning with a prefilled issue link and trials run on the signal wait alone; the manifest's `preflight.oracle_message` records the error and judge.py adds a note line to RESULT.md. `bench.py oracle` still exits 1. No scenario version change.
- Tooling: the oracle's first call (the search) is compiled to a `find_traces` statement when the served tool takes only `sql` (jaeger#10083); older images get the arguments unchanged. Scenario files are untouched, so no scenario version change: the oracle is a pre-flight check and what PASS means is unchanged.
