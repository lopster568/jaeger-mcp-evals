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
- recommendationCacheFailure: version 3 is the scenario the API-client records use (protocol v2 grading, per-trial fixture stability check, restart signal gate, restart before run without Jaeger recording it). Records: cert-recommendationcache-api (stock skill, 0/10 PASS) and skill-callee-down-api (one skill bullet added, 3/10 PASS, EXPERIMENT FAIL against the pre-registered threshold of 5). Both ran without reasoning; see records/skill-callee-down-api/NOTES.md.

## 2026-09-30

- Records: skill-callee-down-cli55 (claude-sonnet-5-5 pinned by full id, Claude Code CLI 2.1.285, effort high, recommendationCacheFailure v3): stock skill 0/10 PASS, one bullet added 7/10, EXPERIMENT PASS against the pre-registered threshold of 5. Results page: docs/results/2026-09-recommendation-cache.md. No scenario or scoring change.
- Harness: the description gate reads image records whose `descriptions` is null; `reasoning_effort` is passed through on the api client, and a run whose reported model differs from the pinned one is refused.
