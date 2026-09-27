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

## 2026-09-29

- Fixture: memory limits raised for load-generator (1G), ad (600M), quote (80M) and fraud-detection (600M), which thrashed the page cache at their old limits. Batches before this change ran with the old limits.
- Fixture: opensearch, prometheus, grafana, opamp-server, flagd-ui and telemetry-docs are switched off (Compose profile `unused`) and the collector no longer exports to them. The fixture is 22 containers (23 with Phoenix); batches before this change ran 28. `MIN_CONTAINERS` is 19 (was 25). No span content or scenario changes; `fixture_overlay_sha256` changes.
