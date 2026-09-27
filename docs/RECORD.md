# Run record contract

What `harness/bench.py run` writes, key by key. Schema 4. Every run setting comes from the
experiment file (docs/USAGE.md, The experiment file), written below as `run.<key>`.

## Per-run record: `<trial>/meta.json` (schema 4)

Written before the agent client starts, then rewritten with `ended_utc`, `wall_time_s`,
`exit_code`, `observed` and `agent_loop`. Batch-level values repeat in every trial so one file
answers "what could have changed this result". The 47 keys, in write order:

| # | Key | Value and source |
|---|---|---|
| 1 | schema_version | constant 4 |
| 2 | run_id | `<batch_id>/<order_index>-<arm>`, also the cell directory name |
| 3 | client | `run.client`: `api`, `cli` or `codex` |
| 4 | provider | `run.provider`: `anthropic` or `openai` for api, null for cli and codex |
| 5 | batch_id | `batch-<UTC>` |
| 6 | order_index | position in the shuffled cell order |
| 7 | trial_index | trial index within its arm |
| 8 | seed | shuffle seed: `run.seed`, else drawn at random |
| 9 | scenario | the experiment file's `scenario` |
| 10 | scenario_sha256 | sha256 of `harness/scenarios/<scenario>.json` |
| 11 | arm | the experiment's arm key |
| 12 | prompt_name | prompt file name without `.txt`; copied to `prompt.txt` |
| 13 | prompt_sha256 | sha256 of that prompt file |
| 14 | system_prompt_sha256 | sha256 of `harness/system-prompt.txt`; copied to `system-prompt.txt` |
| 15 | verdict_schema_sha256 | sha256 of `harness/verdict-schema.json` |
| 16 | model_requested | `run.model` |
| 17 | effort | `run.effort` |
| 18 | max_turns | `run.max_turns` |
| 19 | max_budget_usd | `run.max_budget_usd` as a float |
| 20 | client_argv | argv of the client subprocess: `claude ...` with prompt and system prompt replaced by `sha256:<hex>`, or `python3 harness/agent_loop.py ...` or `python3 harness/codex_client.py ...` (file paths, so nothing to replace; the exact `codex exec` argv is the init event's `codex_argv`). Recorded with no absolute path: a path inside the trial directory is trial-relative (`mcp.json`, `.` for the trial itself), elsewhere under the batch directory batch-relative, else repo-relative (`harness/agent_loop.py`, `harness/prompts/noskill.txt`), and outside the repository its basename (the interpreter, e.g. `python3.14`). The subprocess itself gets the absolute paths |
| 21 | history_policy | cli `{"harness_truncation": "none", "cli_autocompact": "default"}`; api `{"harness_truncation": "none", "cli_autocompact": "n/a"}` (the loop never compacts); codex `{"harness_truncation": "none", "cli_autocompact": "unknown"}` |
| 22 | client_version_pre | at batch start: `claude --version` (cli), `codex --version` (codex) or `agent_loop.py --version` (api: the loop version), or null |
| 23 | mcp_endpoint | host-free: `:<JAEGER_UI_PORT><JAEGER_BASE_PATH>/api/ai/mcp/`; null for an arm with `tools: false`. `mcp.json` (not a record file) carries the real `http://<FIXTURE_HOST>:...` URL the client connects to |
| 24 | mcp_config_sha256 | sha256 of the cell's `mcp.json` |
| 25 | tools_list_sha256 | sha256 of the batch's `tools.json` (MCP `tools/list`, descriptions included); null for an arm with `tools: false` |
| 26 | tools_count | number of tools in it; null for an arm with `tools: false` |
| 27 | tool_descriptions_file | pre-registered wording file for a non-baseline arm, repo-relative, else null |
| 28 | tool_descriptions_sha256 | sha256 of that file, else null |
| 29 | tool_descriptions_check | `compared` (served wording matched the file), `recorded_only` (no file for this arm), `no_tools` (an arm with `tools: false`) |
| 30 | jaeger_image | `docker inspect jaeger` `.Config.Image` at batch start (observed, not typed) |
| 31 | jaeger_image_id | `docker inspect jaeger` `.Image` |
| 32 | jaeger_commit | `branch_commit` of the `harness/experiments/images/*.json` record whose `tag` equals the image, else `KNOWN_COMMITS[image or tag]`, else `unresolved` |
| 33 | otel_demo_ref | `git rev-parse HEAD` of `FIXTURE_DEMO_DIR` on the fixture (local, or over ssh), else `unresolved` |
| 34 | fixture_overlay_sha256 | sha256 of `sha256sum overlay/* .env.override` output in `FIXTURE_DEMO_DIR` on the fixture (mounted overlay plus pristine flag file), else null |
| 35 | fault | `{flag, activation_field, activation_value}` from the scenario, plus `flip_utc`, `ofrep_confirmed`, `signal_traces_seen` from the flip step |
| 36 | preflight | same object as `preflight.json` |
| 37 | experiment | `{name, file, sha256}` of the experiment file (`file` repo-relative) |
| 38 | harness_git_sha | `git rev-parse HEAD` of this repository |
| 39 | harness_dirty | true if `git status --porcelain -- harness fixture` was non-empty |
| 40 | score_py_sha256 | sha256 of `harness/score.py` |
| 41 | started_utc | cell start |
| 42 | ended_utc | cell end (null until the call returns) |
| 43 | wall_time_s | wall time of the client call |
| 44 | exit_code | the client's exit status, 127 if it could not start; also `exit=<n>` in `exit.txt`. The api loop exits 0 for any model-driven stop (max turns included) and 1 only when the trial could not run |
| 45 | observed | from the init event: `client_version` (`claude_code_version`, the loop's `loop_version`, or `codex --version` for codex), `model`, `tools`, `skills` and `agents` (the CLI's own lists; null for api); plus `compaction_events` from score.py's detector |
| 46 | agent_loop | api: the loop's settings block from `agent_loop.json` (loop version, API version header, model and `response_models`, thinking, effort, temperature, max_turns, max_tokens, cache_control, price table, request ids, `result_subtype`); with provider openai also `api_base_url` (`set` when OPENAI_BASE_URL was non-empty, else empty; never the URL, which pack treats as a secret) and `response_format_supported` (false once the endpoint rejected `response_format` and the run went on without it), `thinking` and `effort` are `n/a`, and the price table and cost are null; null for cli |
| 47 | system_under_test | summary block that score.py copies into each scores.jsonl row: `jaeger_image`, `jaeger_image_digest`, `jaeger_commit`, `otel_demo_ref`, `agent_client`, `harness_git_sha` (`-dirty` suffix when dirty), `python_version` (`platform.python_version()` of the interpreter running bench.py), `pinned_at_utc` |

Other cell files: `prompt.txt`, `system-prompt.txt`, `mcp.json` (not a record file, so it
carries the real, host-including URL the client connects to:
`{"mcpServers": {"jaeger": {"type": "http", "url": "http://<FIXTURE_HOST>:..."}}}`, or
`{"mcpServers": {}}` for an arm with `tools: false`), `stream.jsonl` (CLI stream-json stdout, or the
api loop's writing of the same shape), `stderr.txt`, `exit.txt`. The api loop adds
`agent_loop.json`, `reasoning.jsonl` (summarized thinking per turn), `tools.json` (its own
`tools/list`), `mcp.jsonl` (raw MCP exchanges) and `output-schema-sent.json`. The codex
wrapper adds `codex-events.jsonl` (codex's own JSONL events). Its `stream.jsonl` init event records
the MCP url host-free (`:<port><path>`, in `mcp_servers[].url` and in the `codex_argv` `-c
mcp_servers.jaeger.url` pair), the schema file repo-relative, and `codex_bin` as its basename;
codex itself is started with the real URL and paths.

## Per-batch records: `<RUNS_DIR>/<scenario>/batch-<UTC>/`

`--dry-run` writes none of these. `batch.log` tees the batch's stderr.
`tools.json` is the `tools/list` array from pre-flight (absent when no arm in the batch has tools).
At batch end, and after the restore when a batch is interrupted or aborted, `manifest.json`,
`preflight.json`, `cells.jsonl`, `scores.jsonl` and `restore.json` (those that exist) are copied
unchanged to `records/<experiment name>/batch-<UTC>/` in the repository. Nothing else is copied.
The manifest's `mcp_endpoint` is host-free (port and path only) so this copy is safe
to publish; the real `FIXTURE_HOST` only ever appears in `mcp.json`, which is never copied.

After the copy, `records/INDEX.md` is regenerated (the same code as `bench.py index`), so
`bench.py verify` passes straight after a run.

## Per-experiment records: `records/<experiment name>/`

- `RESULT.md`, written by `judge.py` for a judged pair (exit 0 or 1; a refusal writes nothing):
  a header with the experiment name, its sha256, the baseline and variant batch ids and the UTC
  time, then judge.py's printed lines verbatim in a code block. A re-run overwrites it, so it is
  the current answer and its header names exactly which two batches it compared. judge.py then
  regenerates `records/INDEX.md`, whose Results table lists each RESULT.md with its
  `EXPERIMENT PASS` or `EXPERIMENT FAIL` line.
- `trajectories.sha256`, written by `bench.py pack <experiment>`: one `sha256sum` line for
  `dist/<experiment>-trajectories.tar.gz`, so a downloaded release asset checks with
  `sha256sum -c trajectories.sha256` run beside it.

`bench.py pack` finds every batch recorded under `records/<experiment>/`, reads the full batch
directories from `$RUNS_DIR/<scenario>/<batch-id>/` and writes one gzipped tar with entries
`<scenario>/<batch-id>/<path>` (dist/ is gitignored). Two files differ from `RUNS_DIR`: each
`mcp.json` url is rewritten to its host-free `:<port><path>` form, so `mcp_config_sha256` in
meta.json no longer matches it, and `batch.log` (the operator log, which names the ssh target,
fixture URLs and absolute paths) is left out. Every other file goes in unchanged, after a scan
for the values of `FIXTURE_HOST` (skipped when it is `localhost`, `127.0.0.1` or `::1`), `FIXTURE_SSH_USER`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
`OPENAI_BASE_URL` (from fixture.env), the home directory path, `claude.ai/code/session` and
`Claude-Session:`. Any hit refuses the pack (exit 1): each file and the key it matched are printed,
never the value, and nothing is written. The tarball is deterministic (sorted names, mtime 0,
uid and gid 0, no gzip name or time), so packing the same batches twice gives the same sha256.

`preflight.json`, written when the batch directory exists and again after the
leak scan and oracle under the fault: `leak`, `readiness`, `containers`,
`baseline_traces`, `fixture_leak_baseline`, `fixture_leak_under_fault`, `oracle`,
`client`, `client_version` (PASS, FAIL or a count; null if not run; `oracle` stays null when no
arm has tools, `client` unless `run.client` is api, `client_version` unless the experiment sets
`run.client_version`), `sandbox_probe` (null unless `run.client` is cli).

`sandbox_probe` is `{result, tools}`: `result` PASS or FAIL, `tools` the tool names each arm's
CLI sent, keyed by arm. Before the flag is touched, and in `--dry-run` too since it is
read-only and free, pre-flight runs each arm's exact trial argv (same `claude_argv`,
an `mcp.json` pointing at the real Jaeger MCP endpoint, the trial's environment without API
keys, a temporary working directory, plus `--no-session-persistence`) with
`ANTHROPIC_BASE_URL` set to a server on 127.0.0.1 that keeps the first POST body in memory and
answers 400, so no model is called. It fails when the request's `tools` are not exactly
`StructuredOutput` plus that arm's Jaeger tools, when any tool has a `type` (a server-side
tool), or when its system or messages text contains `CLAUDE.md`, `# CLAUDE`, `auto-memory` or
`MEMORY.md`; any failure aborts the batch. The request itself is never written anywhere: on a
claude.ai login it carries the account's email.

`manifest.json`, written before the fault flip and rewritten after the leak scan
under the fault; in this order:

- `scenario`, `batch_id`, `n_per_arm`, `arms` (the arms that ran: those pinned to the running image), `seed`, `order`
  (list of `{arm, trial_index, order_index}`), `file_hashes_sha256` (bench.py,
  tools.py, fixture_leak.py, score.py, system-prompt.txt, verdict-schema.json,
  agent_loop.py, mcp_client.py, codex_client.py, leak-words.txt, judge.py, providers/*.py,
  the scenario file, each arm's prompt), `system_under_test` (as in meta.json),
  `tools_list_sha256`, `tools_list_path` (`tools.json`, relative to the batch directory).
- With one arm: `arm_pin` (`arm`, `expected_image`, `sut_jaeger_image_at_run`) and
  `tools_description_check` (`mode`, `descriptions_file`, `descriptions_sha256`).
- Then keys shared with meta.json: `schema_version`, `client`, `provider`, `scenario_sha256`,
  `scenario_version` (manifest only: the scenario file's `version`, bump rule in
  docs/SCENARIOS.md, Changelog rule), `system_prompt_sha256`,
  `verdict_schema_sha256`, `model_requested`, `effort`, `max_turns`, `max_budget_usd`,
  `history_policy`, `client_version_pre`, `mcp_endpoint`, `tools_count`,
  `jaeger_image`, `jaeger_image_id`, `jaeger_commit`, `otel_demo_ref`,
  `fixture_overlay_sha256`, `fault` (without the flip fields), `preflight`,
  `experiment`, `harness_git_sha`, `harness_dirty`, `score_py_sha256`. In the manifest,
  `experiment` also holds `content`: the experiment file verbatim. judge.py reads
  `experiment.sha256`, `experiment.content` and `arm_pin`.

`cells.jsonl`, one line per cell that ran: `order_index`, `arm`, `trial_index`,
`model`, `out_dir` (the cell directory name, `<order_index>-<arm>`, inside the batch directory),
`trial_exit_code`, `wall_time_s`, `started_utc`, `ended_utc`, `failed` (exit code
not 0). A cell stopped by a signal still gets a line, with `failed` and
`interrupted` true and null timings, so every cell directory has a line.

`restore.json`: `restored_utc`, `cp_ok`, `default_variant`,
`variant_after_restore`, `default_confirmed`.

`scores.jsonl`, one line per cell: score.py's summary plus `arm`. Keys in order:
`dir`, `scenario_used`, `model_asserted`, `mcp_tools_visible`, `tool_calls`,
`call_sequence`, `non_jaeger_tool_calls`, `call_errors`, `read_skill_attempted`,
`read_skill_succeeded`, `read_skill_errors`, `steps_to_evidence`, `tool_output_chars`,
`input_tokens_total`, `output_tokens`, `num_turns`, `cost_usd`, `duration_s`,
`stop`, `locus`, `mechanism`, `mechanism_value`, `cascade`, `abstained`, `verdict`,
`verdict_source`, `signal_leaked_in_prompt`, `system_under_test`, `effort`,
`compaction_events`, `sandbox_ok`, `sandbox_violations`, `arm`. An unscorable cell is
`{dir, error, arm}`.

`sandbox_ok` is true when `sandbox_violations` is empty. The allowed Jaeger tools are the
batch's `tools.json` names prefixed `mcp__jaeger__` (none for an arm with `tools: false`; with
no `tools.json`, the init event's `mcp__jaeger__` names). Each violation is a string
`<kind>:<detail>`: `init_tool_extra` and `init_tool_missing` when the init event's `tools` is
not exactly those names plus `StructuredOutput` (required for cli, refused for api; codex
lists none); `mcp_servers` when the init event does not name exactly one server, `jaeger`
(none for an arm with `tools: false`); `executed_unknown_tool` for a tool_use outside the
allowed set; `attempted_unknown_tool` for one the client refused (`No such tool available`
from the CLI, `unknown tool:` from the api loop); `no_init_event`. codex_client's `error`
items are not tool calls. A refused name such as `mcp__jaeger__StructuredOutput` is never a
Jaeger call. When `sandbox_ok` is false the verdict is INVALID, whatever the answer: `band`,
the Results table and INDEX count it apart (like `err`), judge.py prints a WARNING line and
never counts it as a PASS, and `bench.py verify` fails.

`stop` is the `subtype` of the stream's final `result` event (`success`, else the
client's reason such as `error_max_turns`; null with no result event). `band` and
the batch summary count these apart from wrong answers: `ERROR=<k>` for
`{dir, error, arm}` rows, `stops=` for every non-`success` stop (null as
`no_result`). Both stay inside n. `num_turns` and `non_jaeger_tool_calls` do not compare
across clients: the CLI counts tool_use blocks plus one and lists its `StructuredOutput`
tool; the api loop counts API calls and has no verdict tool.
