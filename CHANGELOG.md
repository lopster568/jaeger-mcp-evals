# Scenario changelog

Entries up to the import into this repository (not yet done; it is planned for the import PR) were written in lopster568/jaeger-mcp-evals; its history holds the full log.
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
- Scoring: the prose fallback and `read_skill_called` are removed. A run with no valid structured verdict now scores MISSING and verdict FAIL (it was graded by regex over its text, which could PASS only through five labelled lines the system prompt no longer asks for).
- paymentFailure, adFailure, paymentUnreachable: version 1 unchanged, `legacy_mechanism_rule` and `abstain_regex` removed (read only by the prose fallback), scenario sha256 changes, comparable: yes.
- paymentFailure, adFailure, paymentUnreachable: version 1 unchanged, `pass_rule.service_regex`, `operation_regex` and `cascade_regex` removed (score.py read the first two only when an `_exact` list was absent, which no scenario had, and never read `cascade_regex`), so what PASS means is unchanged; scenario sha256 changes, comparable: yes.
- Fixture: compose.overlay.yaml gains a `phoenix` service under the Compose profile `store` (the trajectory store, off by default), so `fixture_overlay_sha256` changes once; no run behaviour changes. `bench.py export` sends a batch's trajectories to it.
- Prompt `skill`: `read_skill` is now told the file path `error-root-cause/SKILL.md` instead of the skill name, which agents passed as a directory path and got an error, confounding every skill arm. Its prompt sha256 changes; skill-arm batches before this change are not comparable with later ones.

## 2026-09-27

- Fixture: `filter/deflag` in otelcol-config-extras.yml also drops spans named `^feature_flag\.`, because the load generator emits a `feature_flag.evaluate` span that tells the agent a flag was looked up; `fixture_overlay_sha256` changes once. Not yet applied to the fixture (fixture/apply-deflag.sh).
- Scoring: every scores.jsonl row gains `sandbox_ok` and `sandbox_violations`, and a run that fails the sandbox check scores INVALID (docs/RECORD.md). A refused `mcp__jaeger__StructuredOutput` call is no longer counted as a Jaeger call. Comparable: yes for sandbox-clean runs; a run with any violation, a refused call included, is now INVALID where it was PASS, PARTIAL, FAIL or ABSTAIN.
- Leak gate: `leak-words.txt` gains OpenTelemetry Demo, otel demo, opentelemetry-demo and astronomy shop. The recorded prompts noskill, skill, vague and slow name the demo and now fail the gate; they stay unchanged for the records that cite them. New prompt `neutral` is noskill with the system and symptom sentences made generic.
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

## 2026-09-29

- Fixture: memory limits raised for load-generator (1G), ad (600M), quote (80M) and fraud-detection (600M), which thrashed the page cache at their old limits. Batches before this change ran with the old limits.
- Fixture: opensearch, prometheus, grafana, opamp-server, flagd-ui and telemetry-docs are switched off (Compose profile `unused`) and the collector no longer exports to them. The fixture is 22 containers (23 with Phoenix); batches before this change ran 28. `MIN_CONTAINERS` is 19 (was 25). No span content or scenario changes; `fixture_overlay_sha256` changes.
- recommendationCacheFailure: version 3 (protocol v2 grading, per-trial fixture stability check, restart signal gate, restart before run without Jaeger recording it). The API-client records first published with this version were removed on 2026-09-30.

## 2026-10-12

- recommendationCacheFailure: version 3 unchanged, `signal_regex` also matches the v3 attribute form (`"value": {"intValue": "N"}`), scenario sha256 changes, comparable: yes, no scoring change.

## 2026-10-15

- Fixture: the flagd memory limit is raised from the demo's 75M to 200M. At 75M flagd's own executable pages are evicted and re-read from disk until it stops answering; on 2026-10-15 a fresh `make setup` hit this within minutes. GOMEMLIMIT is unchanged; no flag, image or span differs. `fixture_overlay_sha256` changes, and batches before this change ran with the old limit.

## 2026-10-28

- emailMemoryLeak: version none to 1, comparable: no prior version in this repo. Ported from an earlier draft (v1, 2026-10-04) with its two evidence files; what PASS means is unchanged from that draft, and only the notes changed (this repo's bench.py line numbers, and the 2026-10-06 screen that aborted at the signal wait before any trial). Not yet run to a trial on the fixture.
- cartFailure: version none to 1, comparable: no prior version. New scenario: every request to empty a cart fails to connect to its store and the caller discards the error. Written from the 3.0.0 source; evidence traces not yet captured, so readiness fails until they are.

## 2026-11-03

- Tooling: pre-flight records the MCP server's initialize instructions (sha256 in the manifest) and leak-scans them with the tools. An image record with an `instructions` key names the expected file under `harness/experiments/instructions/`, and a variant arm aborts before the flag flips if the fixture serves different text. No scenario or scoring change.

## 2026-11-05

- recommendationCacheFailure: version 3 to 4, comparable: yes. The oracle's `search_traces` call adds `search_depth: 100`, so pre-flight finds the slow pre-restart traces instead of only the newest ten. Pre-flight only; what PASS means is unchanged.
- cartFailure: evidence traces captured on Jaeger main 238c5fa2, so readiness passes. No version change.
- paymentUnreachable: version 2 to 3, comparable: yes. `signal_regex` also matches `lookup badAddress`, the resolver error text on hosts whose DNS answers SERVFAIL (Docker on a cloud VM) instead of NXDOMAIN. Signal wait only; what PASS means is unchanged.

## 2026-11-06

- cartFailure: version 1 to 2, comparable: yes. Adds `signal_operation: POST /oteldemo.CartService/EmptyCart`; v1 could never pass its own signal wait. What PASS means is unchanged, and v1 ran no trial.
- emailMemoryLeak: version 1 to 2, comparable: no. Activation 1000x to 10000x and `signal_wait_s` 900 to 600. At 1000x email was OOM-killed only every 15 to 25 minutes, too rarely for the signal wait; at 10000x it is killed on almost every request with the same dropped connections. What PASS means is unchanged; v1 ran no trial.

## 2026-11-10

- recommendationCacheFailure: version 4 unchanged, comparable: yes. `signal_wait_s` 1800 was typed into a key name with a dated note, so bench.py read the 480 s default; the key is plain now and the note is in `notes`. Pre-flight procedure only, not what PASS means. A test now checks every scenario key.
- Tooling: `judge.py` with one experiment sha256 (the `measure.sh` case) now refuses when the two manifests differ on scenario, scenario version or hash, prompt or system prompt; only a cross-experiment pair was checked before. Overlay and image may still differ.

## 2026-11-13

- Harness: the fixture's bootstrap Jaeger pin moves from 2.20.0 to 2.23.0 so one config (`ai.mcp: {}`) serves every image the fixture runs; `jaeger-config-main.yml` and the `JAEGER_CONFIG_FILE` switch are gone, so `fixture_overlay_sha256` changes from here on. No scenario or scoring change; batches before and after are not comparable on fixture hash.

## 2026-11-17

- Tooling: client drift is now checked by the harness, not by pinning the CLI. Why: on 2026-11-17 the laptop's Claude CLI updated itself from 2.1.285 (every published trial) to 2.1.326, which sends one extra tool, `DeferredToolPlaceholder`, and the harness's own sandbox probe aborted the run; and a trial that hits a turn or budget limit spends money and returns no verdict. Every Claude CLI call (trials, sandbox probe and grader) now gets `ENABLE_TOOL_SEARCH=false` (removes the placeholder on 2.1.326, checked live) and `DISABLE_AUTOUPDATER=1`. Stage 1 prints the CLI version (`claude CLI 2.1.285 accepts the flags the harness passes`). `judge.py` refuses a pair (exit 2) whose arms, or whose trials within an arm, ran on different client versions, using the version each trial observed (`client_version` in scores.jsonl) or, for older batches, the manifest's `client_version_pre`; with neither it judges and says "client version not recorded" on stderr. score.py adds `client_limits` per trial (stopped early, compacted, within 80% of max_budget_usd, API retries) and `bench.py run` and `band` print a "Client limits" block; report only, no verdict or schema version change. Stage 1 also prints the expected cost of the run from the recorded trials of this scenario on this model (median, p90, max) and WARNs, before any spend, when the recorded max is at least half of `run.max_budget_usd`; no turn warning, since the CLI's `num_turns` is not what `--max-turns` counts (40 recorded under 30, ended success). No scenario or scoring change.

## 2026-11-18

- Harness: trials, the sandbox probe and the grader no longer inherit `CLAUDE_CODE_PLUGIN_DIRS`, and a trial whose CLI loaded a non-builtin plugin lists it under Client limits (report only). Why: `--setting-sources ""` does not stop plugins named in that variable; 261 of the 301 trials published that day loaded an operator UI plugin from it (211 of the 251 in records/ now, docs/TUNING.md). It added no tool (each init lists only the verdict tool and the Jaeger tools) and hooked only Bash, UI rendering and two slash commands, none of which a trial has, so no published result changes.

## 2026-11-19

- Harness: the owned agent loop (`harness/agent_loop.py`), its providers (`harness/providers/`) and their tests are removed, and with them `run.client` `api`. Why: Jaeger's principle is "bring your own agent", and a benchmark that maintains its own agent goes against it (jaegertracing/jaeger-ai-evals#2). The Claude Code CLI (`cli`) and Codex (`codex`) clients stay; `mcp_client.py` stays for the `tools/list` capture. No published record used the loop: every batch under `records/` ran on client `cli`. `run.provider` must now be null (every shipped experiment file already has it null, so their sha256 values are unchanged), new manifests no longer hash `agent_loop.py` or the provider files, and `meta.json` keeps `provider` and `agent_loop` as null. `OPENAI_API_KEY` and `OPENAI_BASE_URL` leave `fixture.env.example` and the `pack` and `export` leak scan, since nothing reads them now; trial and grader processes still start without them. No scenario or scoring change.

## 2026-11-21

- Harness: Goose is an agent client, `run.client` `goose` (jaegertracing/jaeger-ai-evals#2): Goose v1.57.0 in the image the Jaeger AI Assistant demo pins, started through the demo's `goose-without-extensions.sh`, one Docker container per trial, writing stream.jsonl in the Claude CLI's shape. `run.provider` is required for goose (`anthropic` or `openrouter`) and stays null for `cli` and `codex`, so the shipped experiment files and their sha256 values are unchanged. `make measure` writes its pair file with client goose, provider and model from `GOOSE_PROVIDER` and `GOOSE_MODEL` in fixture.env, and refuses to start without them; `--client cli` keeps the Claude Code CLI. score.py records Goose's refusal "Tool '<name>' is not available." as an attempted unknown tool, as it does the CLI's, and allows Goose's `load_skill` for client goose only, listing each call under Client limits. `OPENROUTER_API_KEY` joins fixture.env.example and the leak scans; trial and grader processes start without it. New manifests hash `goose_client.py` and the wrapper. No recorded trajectory holds Goose's refusal text or a `load_skill` call, so no published score changes. No scenario or scoring change for `cli` or `codex`.

## 2026-11-22

- emailMemoryLeak: version 2 unchanged, notes shortened, scenario sha256 changes, comparable: yes.
- cartFailure: version 2 unchanged, notes shortened, scenario sha256 changes, comparable: yes.
- recommendationCacheFailure: version 4 unchanged, notes shortened, scenario sha256 changes, comparable: yes.
- Fixture: the overlay's four host ports (16686, 8013, 8016, 16006) bind `${FIXTURE_BIND:-127.0.0.1}`, so 127.0.0.1 unless fixture.env sets FIXTURE_BIND; fixture_overlay_sha256 changes; no change in what the agent sees; comparable: yes. A remote fixture is reached through fixture/forward.sh, or directly with FIXTURE_BIND=0.0.0.0 behind a firewall.
- Harness: the Claude Code CLI client's code (its argv, flag check, sandbox probe, model pin and billing proof) moves from bench.py to `harness/claude_client.py`, beside goose_client.py and codex_client.py, and bench.py reads each client from one table. New manifests hash `claude_client.py` in `file_hashes_sha256`. No record field, score, grade or verify result changes.
- Experiment file: `run.max_budget_usd` becomes optional for `goose` and `codex`, which enforce no budget, and stays required for `cli`. The file `version` stays 2: every existing file keeps the key and loads unchanged, and a present value is still checked. A batch without it records `max_budget_usd` null in its manifest and meta.json, and stage 1's expected-cost line names no cap. No record, score or verify result changes.
- Settings: fixture.env.example puts the REQUIRED Goose lines first and shows every OPTIONAL default commented out (`#KEY=value`), with one short comment per key. harness/config.py and fixture/lib.sh read those commented lines of the example as defaults and accept a ` #` inline comment, so every default is unchanged; a commented line in fixture.env stays a comment. No run behaviour changes.
- Settings: `GOOSE_PROVIDER` and `GOOSE_MODEL` default to `openrouter` and `nvidia/nemotron-3.5-lightning:free`, the defaults of the Jaeger AI Assistant demo's scripts/ai-sidecar/goose/docker-compose.yml (jaeger main 8e07bbf4), so a Goose contributor sets only `OPENROUTER_API_KEY`. The goose grader falls back to the same two. A batch still records the provider and model it ran with; no recorded result changes.

## 2026-11-23

- Docs: README Quick Start is rewritten to run from a fresh machine (clone, setup, smoke, measure, verify), docs/USAGE.md gains the Ubuntu install step, the key pre-flight limit and the Claude Code install line, and fixture.env.example and `make help` name `cli` as the Claude Code CLI. No scenario, scoring or fixture change; comparable: yes.
- Settings: within fixture.env (or the example) the last line for a key wins in `fixture/lib.sh` as it already did in `harness/config.py`; the environment still wins. No run behaviour changes.
- Setup: `make preflight` runs `docker info` and says whether Docker is not running or this user lacks permission (the docker group), names `~/.local/bin` when `claude` is installed but not on PATH, and prints its claude note only when `SMOKE_CLIENT=cli`.
- Setup: a failed `make up` step shows the error as the last lines of its output, not the compose config JSON; `make up` also starts Phoenix when its container exists; Ready waits until the smoke scenario's service has 3 baseline traces, so Ready means `make smoke` can start; `make smoke` names which of GOOSE_PROVIDER and GOOSE_MODEL is wrong. No record, score or verify result changes.
- Harness: `bench.py run` output only. The results table says `trials using each tool: <tool> <k>/<n>` (it read `tools used: <tool> <k>`, which looked like a call count), and "Passes to read" is now "Suspect passes". While the fault is awaited in traces it shows a live "waiting for the fault in traces" line (one plain line a minute off a TTY). A smoke dry run no longer promises a copy to `records/` and a one-cell dry run no longer hints at the seed. The no-baseline-traffic abort first says a just-started fixture needs a few minutes of traffic. The Phoenix project id is looked up up to 3 times before the Phoenix line falls back to the base URL. No record, score or verify result changes.
- Harness: `make measure` checks that Go is installed and that the Goose provider's key is set before any build, warns when the model is a free one, builds with one start line per step and the details in a build.log (shown on failure), asks y/N before committing the pair file, and removes the build directories after a successful build.
- Harness: a failed cell prints the client's own error under its trial row (the result event's `error`, else the last line of stderr.txt), and `bench.py run` exits 4 when every cell that ran failed, so a refused one-trial smoke no longer exits 0. A failed cell still scores FAIL; no score changes. Goose's "exited without a complete event" error now ends with Goose's last stderr line, which can carry a provider's refusal.
- Grader: a call that returns no valid label is made once more; a returned label is never asked again, so no grade changes. New `grades.jsonl` rows carry `attempts` (1 or 2). The Goose grader's failure reads "the grader returned no structured answer (goose <provider> <model>, exit N)" with Goose's error or the model's text.
- Harness: a batch with a trial that failed to score lists each error row and a fix line, and is exported to Phoenix before it exits 7, as is a batch whose cells all failed before it exits 4. A trial's Phoenix root span carries the client's error as its status message.
- Harness: `bench.py verify` skips smoke batches with one count line, and reports a stored row that failed to score as one `ERROR` line (discard the batch) instead of a MISMATCH and an UNGRADED line; it still exits 1 for such a batch. No published batch is a smoke batch or has an error row.
- Harness: any experiment named `smoke-*` is a check, not a record: its batch is not copied to `records/` (its Batch line is still printed), and `harness/experiments/smoke-*.json` is git-ignored except the template.
- Settings: `make smoke CLIENT=codex` (or `SMOKE_CLIENT=codex`) runs the smoke trial on Codex with `gpt-5.6-terra`; a remote-fixture agent machine with a codex smoke and the cli grader no longer needs Docker.
- Harness: `run.client` `claude` names the Claude Code CLI; `cli` is still read as `claude` in experiment files, manifests, trial meta and grades, so the published batches judge, verify and re-score as before. New manifests and trial meta record `claude`; `records/` is not edited and `records/INDEX.md` keeps the recorded value per row. comparable: yes (judge treats `cli` and `claude` as one client).
- Settings: `CLIENT` in fixture.env (default `goose`) picks the agent for `make smoke` and `make measure` and replaces `SMOKE_CLIENT`; a fixture.env that still sets `SMOKE_CLIENT` stops with the new name. `CLAUDE_MODEL` (default `claude-sonnet-5-5`) and `CODEX_MODEL` (default `gpt-5.6-terra`) set their models. comparable: yes (the defaults are the models that were hard-coded).
- Grader: with `GRADER_CLIENT` empty (the new default) the grader follows the agent: `claude` on `claude-fable-5-1`, the published grader, for a Claude agent; `goose` for Goose and Codex. A set `GRADER_CLIENT` wins. comparable: yes with the published records (same grader); no with your own Claude batches graded by the earlier Goose default. `make smoke CLIENT=claude` now spends one claude-fable-5-1 grader call on the login, where the free Goose model graded before.
- Measure: `--client codex` (experimental) and `--client claude` (alias `cli`); `CLIENT` in fixture.env sets the default; `claude` checks ANTHROPIC_API_KEY before any build; an unknown scenario lists the scenarios. comparable: yes.
- Harness: the codex pre-flight requires `codex-code-mode-host` beside `codex` or on PATH (Codex runs no MCP tool without it); the smoke Tools line shows the chosen client, and Codex's size line its own text; the live trial row shows "graded after the restore" instead of UNGRADED. comparable: yes (pre-flight and output only).
- Make: `make measure` takes every option (`CLIENT=`, `N=`, `BASE=`, `DRY=1`, `YES=1`), `make run EXPERIMENT=<name or path> [DRY=1] [FIRST=1]` runs an experiment file, and `DRY=1` on smoke, measure and run is a dry run that spends nothing. A lower-case variable (`client=claude`, `n=3`, `jaeger=`, ...) stops with the upper-case spelling instead of being ignored (a lower-case `client=cli` ran the default Goose smoke). No scenario, scoring or fixture change; comparable: yes.
- Judge: with `fisher_p_max`, a `direction:` line says whether the variant is better, worse, or not significantly different, and `measure` prints it as its last line; RESULT.md names both Jaeger images and commits. The decision rule, the exit code and the EXPERIMENT line are unchanged; published RESULT.md files are not rewritten. comparable: yes.
- Measure: the plan, in a dry run too, says whether Go is installed (a dry run warns, a real run stops), the cost per trial and in total, the time, the grader, and below 10 trials per arm "rank only" with the power table. comparable: yes.
- Setup: `make down` says Jaeger's traces are lost (in-memory store) and Phoenix keeps its own; Phoenix gets `--restart unless-stopped` from `make phoenix`, so it comes back after a reboot like the demo. No overlay change. comparable: yes.
- Docs: README in the usual sections (Quick Start, Commands, Options with every make variable, layout); USAGE gains Stop and restart, Review a result, Where results land and a glossary, and USAGE and FIXTURE spell each invocation as `make ...` or `bash fixture/...`; SCENARIOS says a fault must leave an error text in spans. No run behaviour changes. comparable: yes.
