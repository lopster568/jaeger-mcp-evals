# Usage

README.md has the quick start, fixture/FIXTURE.md the fixture. docs/RECORD.md lists every key a
run records; docs/TUNING.md maps each factor that can change a result to its key.

## Prerequisites

Python 3.11 or newer (standard library only), bash, git, and one agent client (below). Every trial is
one paid agent run; 10 per arm with two arms is 20. Docker and Docker Compose on this machine
(or on a remote host reachable over ssh, see fixture/FIXTURE.md) run the OpenTelemetry Demo
3.0.0 with the overlay in `fixture/`; its ports 16686 (Jaeger query) and 8016 (flagd OFREP)
must be reachable from you.

`fixture.env` (copied from `fixture.env.example`, gitignored) holds only where the fixture
runs, where trajectories go (`RUNS_DIR`), the API keys and the Codex command. An environment
variable of the same name wins over the file. Nothing in it changes what a run does.

## Before you start

`make preflight` (run first by `make setup` and `make smoke`) checks docker, Docker Compose v2, git, curl,
Python 3.11 and that the fixture ports 16686, 8013, 8016 and 16006 are free. It skips the port check
once a container named `jaeger` exists, so re-running `make setup` works.

The fixture starts 22 containers (23 with Phoenix). About 2.9 GB RAM in use a few minutes after start and
6 cores busy under load, measured on a 6-core 24 GB host. The first `make setup` pulls a large set of images, so allow for the
download and for disk. `make up` waits up to 10 minutes for the fixture to pass pre-flight and Phoenix
takes one to two minutes more; Jaeger needs a few minutes of load-generator traffic before it shows traces.

The `make` targets run where the demo runs. With a remote fixture host (`FIXTURE_SSH_USER` in fixture.env)
clone this repo on that host and run `make setup` there; `bench.py` then drives it over ssh
(fixture/FIXTURE.md, Remote host).

`make down` stops the containers and keeps their data. `make clean` removes the containers and network;
Jaeger's in-memory traces are lost, and volumes and pulled images stay.

## The experiment file

`harness/experiments/<name>.json` is the complete configuration of a batch. Every key is
required except `run.client_version`; an unknown or missing key is refused before anything
touches the fixture.

For your own runs, copy an experiment file under a new name: records are grouped by experiment
name, and `desc-change-sep28` is pre-registered.

```json
{
  "name": "desc-change-sep28", "version": 2, "scenario": "paymentFailure",
  "hypothesis": "A tool-description change moves an error investigation from get_critical_path to get_trace_topology ...",
  "run": {"client": "cli", "provider": null, "model": "sonnet", "effort": "xhigh",
          "max_turns": 30, "max_budget_usd": 2, "n_per_arm": 10, "seed": null},
  "arms": {
    "baseline":   {"prompt": "noskill", "image": "quay.io/jaegertracing/jaeger:2.20.0", "tools": true},
    "descchange": {"prompt": "noskill", "image": "jaeger-mcp-evals/jaeger:desc-change-sep28-4c355981", "tools": true}
  },
  "baseline_arm": "baseline",
  "thresholds": {"tool_used_min": {"get_trace_topology": 8}, "tool_used_max": {"get_critical_path": 2},
                 "tool_output_chars_median_drop_pct_min": 10, "accuracy_pass_min": 10},
  "status": "CONFIRMED v2 ..."
}
```

| Key | Value |
|---|---|
| `name` | the experiment's name; records land in `records/<name>/` |
| `version` | `2` |
| `scenario` | a `harness/scenarios/<scenario>.json` name |
| `hypothesis` | what the thresholds test, in one sentence |
| `run.client` | `api` (`harness/agent_loop.py` over the Messages API; experimental), `cli` (the `claude` CLI) or `codex` (the OpenAI Codex CLI) |
| `run.provider` | api only: `anthropic` or `openai` (any OpenAI-compatible chat completions endpoint); `null` for cli and codex |
| `run.model` | the model id, sent unchanged; the api client refuses aliases |
| `run.effort` | `low`, `medium`, `high`, `xhigh` or `max` |
| `run.max_turns` | positive integer; API calls for api, CLI turns for cli, tool calls for codex |
| `run.max_budget_usd` | positive number; no effect with provider openai or client codex (cost unknown) |
| `run.n_per_arm` | trials per arm |
| `run.seed` | shuffle seed, or `null` to draw one and record it |
| `run.client_version` | optional, e.g. `"2.1.283"`: the batch refuses before touching the fixture unless the client reports this version (the first numeric token of `client_version_pre`); the result is `client_version` in preflight.json |
| `arms.<arm>.prompt` | a `harness/prompts/<prompt>.txt` name |
| `arms.<arm>.image` | the Jaeger image this arm must run against |
| `arms.<arm>.tools` | `false` gives the arm no MCP server, to test answers from memory |
| `baseline_arm` | the arm `judge.py` compares against, or `null` |
| `thresholds` | `judge.py` thresholds (below); `{}` for none |
| `status` | free text; a batch refuses while it contains `DRAFT` |

The api client is experimental: it has run one trial outside this repository and has no batch
under `records/`. It needs `ANTHROPIC_API_KEY` (provider anthropic) or `OPENAI_API_KEY` and
`OPENAI_BASE_URL` (provider openai) in fixture.env, bills the account behind that key and never
writes it or the base URL into a record (`api_base_url` records only `set`). The cli
client runs the logged-in `claude` with no API key in its environment. The codex client runs
`codex` (or the command in `CODEX_BIN`) through `harness/codex_client.py`: MCP only, read-only
sandbox, stopped after `max_turns` tool calls.

Changing any setting means editing the file, which changes its sha256. Record the change in
`status`, and add a line to CHANGELOG.md if earlier batches stop being comparable.

## Run a batch

Run everything from the repository root.

1. Bring the fixture up and check it per fixture/FIXTURE.md (`make up` does both on this machine).
2. `python3 harness/bench.py soak paymentFailure` samples load, unhealthy containers and the
   root-cause service's trace rate 30 times, 60 s apart, into
   `$RUNS_DIR/paymentFailure/soak-<UTC>.log`. Exit 0 pass, 5 after two bad samples in a row.
3. `python3 harness/bench.py run harness/experiments/<name>.json --dry-run` runs the read-only
   pre-flight (it does read the fixture: ssh or local shell, `docker inspect`, HTTP to Jaeger
   and OFREP, the MCP `tools/list` and description check, the leak scan of the rendered prompts
   and served tool descriptions, and the sandbox probe) and prints what a
   real batch would do, including the seed it drew. It writes nothing, never flips the flag, never
   calls a model.
4. `python3 harness/bench.py run harness/experiments/<name>.json` runs pre-flight, the flag flip
   confirmed through OFREP, a wait for fresh fault traces, a second leak scan and the oracle
   under the fault, the cells in shuffled order, a guaranteed restore of the pristine flag
   file, scoring (with one grader call per new mechanism answer, docs/TUNING.md, Mechanism
   grader) and a band per arm. `--first-only` runs one cell. It writes
   `$RUNS_DIR/<scenario>/batch-<UTC>/` (`batch.log`, `preflight.json`, `manifest.json`,
   `tools.json`, `cells.jsonl`, `restore.json`, `scores.jsonl`, `grades.jsonl`, one `<order_index>-<arm>/`
   directory per cell), then copies `manifest.json`, `preflight.json`, `cells.jsonl`,
   `scores.jsonl` and `restore.json` to `records/<name>/batch-<UTC>/`. The copy also happens
   after an interrupt or an abort once the flag is restored. Trajectories are never copied.
   Exit 0 done, 1 bad experiment file, pre-flight or setup failure (nothing ran), 4 aborted after
   3 consecutive cell failures, 5 flag not confirmed back at its default after the restore
   (check the fixture by hand; this code wins over the others), 6 interrupted or fewer cells
   ran than planned. SIGINT, SIGTERM and SIGHUP stop the cells and are ignored during the
   restore.

   Everything `run` prints goes to stderr, in sections: a one-line header, Pre-flight and
   Fault (one `ok` line per check), Trials (one line per cell as it ends: verdict, MCP tool
   calls, wall time; a nonzero exit shows as ERROR with its code; a correct locus shows as
   UNGRADED until the grader runs after the restore; on a terminal a `running...`
   line counts the seconds), Restore, and the Results table. A failed check or an abort
   prints that step's full detail under it. `batch.log` keeps every screen line plus all the
   detail (readiness checks, full hashes, polls, the per-arm key=value summary and band
   lines), never colour. Colour appears only on a terminal; `NO_COLOR=1` turns it off,
   `FORCE_COLOR=1` turns it on. `--dry-run` prints the same header and pre-flight, then the
   planned order, fixture commands and files.
5. `run` rewrites `records/INDEX.md` after copying the records (`python3 harness/bench.py index`
   does the same by hand); commit it with the new records. `python3 harness/bench.py verify` re-scores every `stream.jsonl` under `$RUNS_DIR`,
   fails on a stored verdict that disagrees, on any INVALID or LEAK run, or on a mechanism answer
   with no cached grade in the batch's `grades.jsonl` (verify never calls the grader), writes
   nothing, and exits 1 if `records/INDEX.md` differs from what `index` would generate. A trial
   recorded under record schema 4 or older (the mechanism menu) is not re-scored: its stored
   verdict is printed with "legacy schema, stored scores".
6. `python3 harness/judge.py <baseline_batch_dir> <variant_batch_dir>` prints PASS or FAIL per
   threshold, then `EXPERIMENT PASS` or `EXPERIMENT FAIL`. Either the `$RUNS_DIR` batch
   directories or their copies under `records/` work. A judged pair (exit 0 or 1) is also
   written to `records/<name>/RESULT.md`, overwriting the previous answer, and
   `records/INDEX.md` is regenerated; a refusal (exit 2) writes nothing.
7. After both arms are judged, `python3 harness/bench.py pack <name>` writes
   `dist/<name>-trajectories.tar.gz` (every batch of the experiment, from `$RUNS_DIR`) and
   `records/<name>/trajectories.sha256`. It refuses (exit 1, nothing written) when any file
   would publish the fixture host, ssh user, home directory, an API key or base URL, or a
   session identifier, and prints each file with the key it matched. Commit RESULT.md,
   INDEX.md and trajectories.sha256, then optionally attach the tarball to a GitHub release of
   that commit; no release is published yet. docs/RECORD.md lists what the tarball holds.

The variant image `jaeger-mcp-evals/jaeger:desc-change-sep28-4c355981`
(`harness/experiments/images/desc-change-sep28.json`) was built from a local Jaeger commit that
is not published. `fixture/build-variant.sh` rebuilds it: it applies the descriptions file on a
branch cut from v2.20.0 and commits, so a rebuild carries a new commit hash.

A batch runs only the arms whose `image` equals `docker inspect jaeger` on the fixture; none
matching is a refusal. Arms on different images therefore run as separate batches: switch the
image (fixture/FIXTURE.md) and run the same file again.

For each non-baseline arm with tools whose image differs from the baseline's, pre-flight compares
every served `tools/list` description by exact string with
`harness/experiments/descriptions/desc-change.json` (a same-image arm is only recorded). Any difference
aborts before the flag flips. An arm with `tools: false` gets an empty `mcpServers`; when no arm
in the batch has tools, the tools/list capture, description check and oracle are skipped.

Pre-flight gates run alone: `bench.py leak <file>...` (no leak word in any prompt) and
`bench.py readiness <scenario>` (positive integer `version`, a `mechanism_truth`, captured fault trace matches the
signal, captured baseline does not). A scenario is ready only when `readiness` passes.

`bench.py oracle <scenario>` runs the scenario's `oracle` list of MCP tool calls with no agent
and prints PASS at the first call whose output matches `signal_regex`, else FAIL (exit 1). An
argument `"$trace_id"` repeats the call per trace id seen so far. It needs the fault on, so
`run` calls it after the flip and aborts on FAIL. search_traces looks back one hour, so a
standalone PASS can come from an earlier fault window.

## Thresholds

`judge.py` exits 2 unless both manifests carry the same experiment sha256 and their `arm_pin`
blocks match the argument order. Per test arm it checks runs using each tool
(`tool_used_min`, `tool_used_max`), the percentage drop in median `tool_output_chars` against
baseline (`tool_output_chars_median_drop_pct_min`), and the PASS count (`accuracy_pass_min`).
Exit 0 if all pass, else 1.

## Reading results

- Every number comes from `bench.py verify`. Below 10 cells per arm a result is a ranking,
  not a rate; a rate of 0 or 1 means read the trajectories for a leaky prompt or unfair
  assertion.
- `bench.py band <batch_dir> [--arm A]` (a `$RUNS_DIR` batch directory: it re-scores the
  trajectories) prints the same Results table as `run`: per arm PASS/n, PARTIAL, FAIL,
  ABSTAIN, `err` (unscorable cells), `invalid` (sandbox check failed), `leak` (the answer names a
  flag), pass rate, Wilson 95% CI, median tool calls and median
  tool output chars; under each row `tools used` (runs using each tool at least once,
  excluding `read_skill`), then, when present, median steps to evidence, `stops` (abnormal
  endings by stop value, `no_result` for a stream with no result event), read_skill counts
  and call errors. `err`, `invalid`, `leak` and stops count in n, never in PASS. band reads cached
  grades only and never calls the grader. Exit 0 certified, 2 when n is
  under 10, 3 when the rate is 0 or 1.
- `band` and `verify` flag runs with a compaction event (context numbers not comparable).
  Scenarios with deterministic set to no stay out of the certified pool.
- `bench.py power <n_per_arm>` prints, per baseline PASS count, the smallest variant PASS
  count significant at two-sided p < 0.05 by Fisher's exact test, for any PASS/FAIL count
  including `tool_used_*`. Use the certified N, not the planned N.

## Scoring

`python3 harness/score.py <trial_dir>` scores from raw files. Tool metrics count only calls
to the batch's Jaeger tools (the names in its `tools.json`, prefixed `mcp__jaeger__`): `tool_calls`, `call_sequence`, `call_errors` (`is_error` results),
`steps_to_evidence` (first result matching `signal_regex`) and `tool_output_chars`. The
CLI's own verdict tool is listed separately and never counted as investigation work.

`score.py` reads the `structured_output` object on the stream's final `result` event, which
the client validates against `verdict-schema.json`.

- `locus`: PASS, PARTIAL or FAIL from service and operation against `pass_rule`.
- `mechanism`: the agent's free-text mechanism, graded against the scenario's `mechanism_truth`
  by the pinned grader (docs/TUNING.md, Mechanism grader): `correct` PASS, `incorrect` FAIL,
  `unclear` UNCLEAR (not a PASS); UNGRADED when no cached grade exists and the grader may not be
  called (every path except the end of `run`).
- `cascade`: every `cascade_rule.required_any` group must match.
- `verdict`: PASS if locus and mechanism both PASS; UNGRADED if locus is PASS and the mechanism
  is ungraded; PARTIAL if locus is PASS or PARTIAL but mechanism is not; ABSTAIN if the agent
  set `abstain`; else FAIL. INVALID replaces all of these when `sandbox_ok` is false, and LEAK
  when `leak_hits` is not empty (the answer or any assistant text names a flag, `feature_flag` or
  `flagd`; docs/RECORD.md, scores.jsonl).
- No valid `structured_output` (schema-invalid answer, max turns, crash): locus, mechanism and
  cascade are MISSING, the verdict is FAIL and `verdict_source` is null.
- Skill arms report `read_skill_attempted` and `read_skill_succeeded`; an error result is not
  a read.

`fixture_ok` (meta.json and scores.jsonl) is the same kind of check as `sandbox_ok`, over the
fixture instead of the agent: `run` snapshots every container's `RestartCount`, `OOMKilled` and
`StartedAt` (`docker inspect`) before and after each trial, and records the ones that changed as
`fixture_changes`. A change to a container not named in the scenario's optional
`expected_restarts` list makes `fixture_ok` false and the trial INVALID, exactly the path
`sandbox_ok` false takes. A scenario whose fault ends in a restart (a memory leak that gets
OOM-killed, say) can also set `signal_after_restart` to that container's name: once the
signal_regex poll confirms the fault, `run` polls the container's `RestartCount` every
`TRACE_POLL_SLEEP` (same poll budget as the signal wait) until it rises, records
`fault.restart_seen_utc`, and only then starts trials; it aborts if the restart never comes.
A scenario can set `restart_before_run` (container names): pre-flight restarts them, waits until each
is running again, then restarts Jaeger and waits for its query API, so Jaeger's store never holds the
restart; the baseline-traffic check then polls (TRACE_POLL_MAX x TRACE_POLL_SLEEP) while the empty store
fills. `preflight.json` records `restarted_before_run`. `verify` scores each batch against its scenario file
at the batch's `harness_git_sha` (current file if git cannot resolve it).
Pre-flight also runs `vmstat 5 3` and `cat /proc/loadavg` once (`preflight.json`, `thrash`) and
aborts before the flag is touched if the mean of `bi` over the non-first samples is over 50000
(the host is thrashing); the load average is recorded only, never a gate.

## Export to Phoenix

`python3 harness/bench.py export <RUNS_DIR>/<scenario>/<batch-id> [--endpoint URL]` sends every
trial listed in the batch's `scores.jsonl` to Phoenix (fixture/FIXTURE.md, Trajectory store) as
one OTLP protobuf POST to `<URL>/v1/traces`; the default URL is
`http://<FIXTURE_HOST>:<PHOENIX_PORT>`. It needs the trial directories, so a `records/` copy is
refused. Each trial is one trace: an AGENT root span, one TOOL span per tool call and a final LLM
span with the answer. The root carries `experiment`, `batch_id`, `scenario`, `arm`, `trial_index`,
`model`, `client` and `verdict` (from `scores.jsonl`), also as one `metadata` JSON, and the
Phoenix project is the experiment name (`openinference.project.name`). Tool outputs and the
answer are cut to 8 KiB. Trace and span ids are derived from `<scenario>/<batch-id>/<trial>`, so
exporting a batch twice sends the same ids again. The command reads the batch and writes
nothing; it refuses (exit 1, nothing sent) on the same values `pack` refuses, and exits 1 with
the error when Phoenix does not accept the request.

Span timestamps are synthetic and must not be read as timings. stream.jsonl has no per-event
times, so spans start at `started_utc` and split the run's `duration_ms` evenly
(`timestamp.synthetic=true`); only the trial's total duration is measured.

Checked on 2026-09-26 against a live Phoenix of the digest pinned in fixture/compose.overlay.yaml:
each experiment name becomes one Phoenix project; OTLP protobuf is accepted and OTLP JSON is
refused with 415; exporting a batch twice leaves the span count unchanged; the data survives a
container restart (named volume).

## What stays out of git

`stream.jsonl`, `stderr.txt` and the per-cell directories carry session identifiers and local
paths, so they stay under `RUNS_DIR` (default `runs/`, gitignored; point it elsewhere in
fixture.env). The batch-level records under `records/` are the checked-in record of what ran;
the trajectories can be attached to a release with `bench.py pack` (step 7) and are never committed.
