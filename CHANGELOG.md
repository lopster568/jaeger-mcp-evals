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

## 2026-11-10

- recommendationCacheFailure: version 4 unchanged, comparable: yes. `signal_wait_s` 1800 was typed into a key name with a dated note, so bench.py read the 480 s default; the key is plain now and the note is in `notes`. Pre-flight procedure only, not what PASS means. A test now checks every scenario key.
- Tooling: `bench.py run` exits 7 when a trial failed to score (grader timeout, invalid label); it exited 0, `measure.sh` went on to judge, and the failed trial counted in n and never as PASS.
- Tooling: `judge.py` with one experiment sha256 (the `measure.sh` case) now refuses when the two manifests differ on scenario, scenario version or hash, prompt or system prompt; only a cross-experiment pair was checked before. Overlay and image may still differ.
- Tooling: `judge.py` on a cross-experiment pair with no `arm_pin` on the variant batch no longer judges the variant experiment's `baseline_arm` as a test arm.
- Tooling: bench.py confirms the restore with the same bounded OFREP poll the flip uses (5 reads, 2 s apart) instead of one read, so a slow flagd reload or one failed request no longer exits 5 and discards a paid pair.
- Tooling: a dead stderr (Ctrl-C under `make measure` killing tee, an ssh drop) no longer stops the harness; the screen write swallows OSError and the flag restore still runs. batch.log is unaffected.
- Tooling: a cell running longer than `CELL_TIMEOUT_S` (30 minutes; the longest of 780 recorded cells took 236 s) is killed and recorded as failed with exit 124, so a hung agent cannot hold the fault flag on and the 3-consecutive-failures abort applies.

## 2026-11-13

- Tooling: FIXTURE_HOST_ONLY=1 in fixture.env makes preflight, `make setup` and `make up` work on a fixture host with no claude CLI (the agent-side checks still run where the agent runs, and `make smoke` ignores the flag). No scenario or scoring change.
- Tooling: `make up` retries a failed `docker compose pull` up to 4 times with a 10 s, 20 s, 30 s backoff, one line per failed attempt, before it fails. No scenario or scoring change.
- Tooling: `fixture/forward.sh SSH_TARGET` keeps the ssh port forwards to a remote fixture alive (restart loop, ExitOnForwardFailure, keepalives, UTC-stamped restart log); `--check` probes each forwarded port once for gating a batch. No scenario or scoring change.
- Docs only: results page and README now lead with the merged #10068 pair on main 5319091a. No scenario or scoring change.
- Tooling: `make up` keeps retrying a failed pull while each attempt completes an image, and gives up after 4 attempts in a row that complete none (a live run through Cloud NAT needed more than 4 attempts while still progressing).
- Tooling: preflight on the agent machine of a remote fixture (`FIXTURE_SSH_USER` set) no longer requires Docker or checks the demo's ports; those belong to the fixture host, whose own `make up` checks them.
- Harness: the fixture's bootstrap Jaeger pin moves from 2.20.0 to 2.23.0 so one config (`ai.mcp: {}`) serves every image the fixture runs; `jaeger-config-main.yml` and the `JAEGER_CONFIG_FILE` switch are gone, so `fixture_overlay_sha256` changes from here on. No scenario or scoring change; batches before and after are not comparable on fixture hash.
- Records: measure-recommendationCacheFailure-b6ad85f8, `make measure` end to end on the 2.23.0 bootstrap fixture: Jaeger main b6ad85f8 with and without #10068, 2/10 to 10/10, p 0.0007, EXPERIMENT PASS; results page section added. No scenario or scoring change.

## 2026-11-17

- Tooling: score.py records `evidence_unseen` (cited span ids that appear in no tool result), `tool_result_leak_hits` (flag names in tool results) and `export_marker_hits` (harness export markers in tool results) per trial, and the grader row carries `cost_usd` when the CLI result reports it. `bench.py run` and `bench.py band` print a "Passes to read" block under the Results table. Report only: no verdict, schema version or `verify` comparison changes. `judge.py` prints one `grounding:` line (so it lands in RESULT.md) when any compared row has those fields: `grounding: N flagged PASS (baseline a, variant b): read them before trusting this result` with each arm and trial dir, or `grounding: 0 flagged PASS`; rows without those fields (published records) print nothing, and verdict and exit code are unchanged. No scenario or scoring change.
- Tooling: bench.py's pre-flight, fault and restore output is numbered stages (`[1/6] Agent and grader` to `[6/6] Evidence`, `[1/4]` to `[4/4]` on a dry run), each with the reason it exists; check lines are reworded (`host busy: N disk blocks/s read (limit 50000)`, `demo reachable`, `evidence reached in N calls`), the main stops carry a `fix:` line, a failed evidence check is a `WARN` that says it is the harness's own check, and a failed restore prints the restore command. `make up` reads the new wording. batch.log words, preflight.json, records and the `Batch` line are unchanged. No scenario or scoring change.
- Tooling: `bench.py run` now sends the finished batch to Phoenix by itself and prints `Phoenix <url>/projects/<id>   project <experiment>: one trace per trial` after the `Batch` and `Records` lines (the id from Phoenix's `/v1/projects`; the base URL and `open the project, one trace per trial` if the lookup fails); `--no-export` skips it. Why: `make setup` started Phoenix but no run sent anything to it or said where to look, and export was a manual `bench.py export <batch_dir>` nobody was told about (measure.sh ended at judge.py); Yuri asked on 2026-10-06 for traces per eval run so one can investigate. If Phoenix is down or export refuses (a leak word in a span), the run prints one WARN with the batch directory and the manual command; the exit code and the `Batch` line are unchanged, and a dry run or an aborted run never exports. `measure.sh` runs both arms with `--no-export` and exports both batches after judge.py, a failure only a warning. Old trajectories must never reach the agent, whose only route is trajectories landing in the demo's Jaeger, so `export` now refuses an endpoint on the fixture's `JAEGER_UI_PORT` or `OFREP_PORT` or on 4317 or 4318 (the demo collector), and stage 3 of the pre-flight lists Jaeger's services through the MCP tool `get_services` and stops with a `fix:` line if `jaeger-mcp-evals` (the export's service name) is in the store; `preflight.json` gains `store_clean` (PASS, FAIL, or null when the list could not be read, which is a WARN so older images still run). The export guard's refusal reads each phrase once, and `make up` does not retry the store check ("harness trajectories in Jaeger's store"), since waiting cannot fix it. No scenario or scoring change.
- Tooling: client drift is now checked by the harness, not by pinning the CLI. Why: on 2026-11-17 the laptop's Claude CLI updated itself from 2.1.285 (every published trial) to 2.1.326, which sends one extra tool, `DeferredToolPlaceholder`, and the harness's own sandbox probe aborted the run; and a trial that hits a turn or budget limit spends money and returns no verdict. Every Claude CLI call (trials, sandbox probe and grader) now gets `ENABLE_TOOL_SEARCH=false` (removes the placeholder on 2.1.326, checked live) and `DISABLE_AUTOUPDATER=1`. Stage 1 prints the CLI version (`claude CLI 2.1.285 accepts the flags the harness passes`). `judge.py` refuses a pair (exit 2) whose arms, or whose trials within an arm, ran on different client versions, using the version each trial observed (`client_version` in scores.jsonl) or, for older batches, the manifest's `client_version_pre`; with neither it judges and says "client version not recorded" on stderr. score.py adds `client_limits` per trial (stopped early, compacted, within 80% of max_budget_usd, API retries) and `bench.py run` and `band` print a "Client limits" block; report only, no verdict or schema version change. Stage 1 also prints the expected cost of the run from the recorded trials of this scenario on this model (median, p90, max) and WARNs, before any spend, when the recorded max is at least half of `run.max_budget_usd`; no turn warning, since the CLI's `num_turns` is not what `--max-turns` counts (40 recorded under 30, ended success). No scenario or scoring change.
- Tooling: fixes before push, from the 2026-11-18 review. `make measure` no longer writes `run.client_version` into the pair files it generates (it pinned 2.1.285, so a contributor on a newer CLI aborted at stage 1 and the printed fix led to "exists and differs"); an existing pair file's pin is ignored in that comparison and kept, as the exact-re-run pin. The key stays optional; the abort's fix line now reads "remove run.client_version from the experiment file to run on your CLI, or install <version> for an exact re-run". The sandbox-probe abort and the rendered-leak, baseline-traffic and OFREP aborts now print a `fix:` line (the probe's names the unexpected tool and the CLI version). Stage 1 prints one cost line, `expected cost about X USD per arm of n trials (recorded max Y per trial, cap Z)`, X being n times the recorded median. The informational "find_traces takes SQL only" note and its sniffing of the oracle output are removed. No scenario or scoring change.

## 2026-11-18

- Harness: trials, the sandbox probe and the grader no longer inherit `CLAUDE_CODE_PLUGIN_DIRS`, and a trial whose CLI loaded a non-builtin plugin lists it under Client limits (report only). Why: `--setting-sources ""` does not stop plugins named in that variable; 261 of the 301 published trials loaded an operator UI plugin from it. It added no tool (each init lists only the verdict tool and the Jaeger tools) and hooked only Bash, UI rendering and two slash commands, none of which a trial has, so no published result changes.
- Tooling: `make setup` prints `local docker not running (remote fixture)` instead of `docker The, compose The` when `docker` reports no server version and `FIXTURE_SSH_USER` is set (Docker absent, a WSL shim or a stopped daemon; a missing compose plugin alone reads `compose ?`) (the parsing dates from 2026-10-19). No scenario or scoring change.
- Tooling: the Phoenix export carries cost, real per-call timing and the prompt. Why: Phoenix showed 0 USD (it prices an LLM span only from `llm.model_name` and the token counts with cache read and write, taken now from the result's `modelUsage`, one LLM span per model), tool spans were an even split of the trial's duration (now the CLI's event timestamps, the even split with `timestamp.synthetic=true` only when a stream has none), and the trace list showed `--` (the root now has the prompt and the answer). No scenario or scoring change.

## 2026-11-19

- Harness: the owned agent loop (`harness/agent_loop.py`), its providers (`harness/providers/`) and their tests are removed, and with them `run.client` `api`. Why: Jaeger's principle is "bring your own agent", and a benchmark that maintains its own agent goes against it (jaegertracing/jaeger-ai-evals#2). The Claude Code CLI (`cli`) and Codex (`codex`) clients stay; `mcp_client.py` stays for the `tools/list` capture. No published record used the loop: every batch under `records/` ran on client `cli`. `run.provider` must now be null (every shipped experiment file already has it null, so their sha256 values are unchanged), new manifests no longer hash `agent_loop.py` or the provider files, and `meta.json` keeps `provider` and `agent_loop` as null. `OPENAI_API_KEY` and `OPENAI_BASE_URL` leave `fixture.env.example` and the `pack` and `export` leak scan, since nothing reads them now; trial and grader processes still start without them. No scenario or scoring change.
- Records: removed from the tree records/root-cause-2026-10-30, records/first-move-2026-10-28, and the one-arm diagnostic records skill-callee-down-main238-cfg, skill-callee-down-v2200-isolate, skill-skip-main238 and skill-skip-main238-svcreq-r2, with the four experiment files of the same names under harness/experiments/. Nothing referred to them and they carried local paths; they remain in history at 9cf1a61. The root-cause finding moved to [docs/results/2026-10-root-cause.md](docs/results/2026-10-root-cause.md); records/INDEX.md is regenerated. No scenario or scoring change.
- Harness: the grader model and its command are settings, `GRADER_MODEL` and `GRADER_BIN` in `fixture.env.example` (jaegertracing/jaeger-ai-evals#2). Defaults are unchanged (`claude-fable-5-1`, `claude`), so existing grades, cache keys and records are unaffected. No scenario or scoring change.
- Harness: the 2026-09-28 tool-description pair (experiment `desc-change-sep28`, its image record and `descriptions/desc-change.json`) and its build path (`fixture/build-variant.sh`, `fixture/apply-descriptions.py`, `JAEGER_SRC`) are removed; it has no record, and `make measure` builds from the contributor's checkout. An image record without a `descriptions` key no longer defaults to a descriptions file, so the check runs only when the key names one. No scenario or scoring change.
- Harness: the experiment files release-v2200-vs-main-adFailure, -cartFailure and -paymentUnreachable are removed (registered 2026-11-04, never run; a 2.20.0 build no longer boots on this fixture), and so are the prompts `slow` and `vague` (no experiment used them). No scenario or scoring change.
- Tooling: `measure.sh` signs off (`git commit -s`) the pre-registration commit it makes in the harness clone, which the target repo's DCO check requires.

## 2026-11-21

- Tooling: every code and config file (`*.py`, `*.sh`, `*.yml`, `*.yaml`, the Makefile, `fixture.env.example`, `fixture/env.override`) starts with the Apache-2.0 license header (jaegertracing/jaeger-ai-evals#2); the prompt and word-list `.txt` files and the JSON files are excluded, so their sha256 values are unchanged. Comments in those files no longer carry dates or round-by-round history. The files `bench.py` hashes into new manifests change, and so does `fixture_overlay_sha256`: the next `make setup` re-copies the overlay (old copies kept as `.bak`), and `judge` refuses to compare batches of different experiment files across this change. No scenario or scoring change.
