# Run record contract

`harness/bench.py run` writes the record of each batch; schema 5 (free-text mechanism graded by grade.py; older schemas are shown as "legacy schema, stored scores" and never re-graded).
Every setting comes from the experiment file (docs/USAGE.md, The experiment file), written `run.<key>` below.

## Files

| File | Where | Holds | Copied to `records/` |
|---|---|---|---|
| `meta.json` | trial dir | every setting and source hash of one trial | no |
| `stream.jsonl`, `prompt.txt`, `system-prompt.txt`, `mcp.json`, `stderr.txt`, `exit.txt`, plus `agent_loop.json`, `reasoning.jsonl`, `mcp.jsonl`, `codex-events.jsonl` by client | trial dir | raw client output and the exact inputs; `mcp.json` has the real host | no |
| `tools.json` | batch dir | `tools/list` from pre-flight, descriptions included; absent if no arm has tools | no |
| `manifest.json` | batch dir | batch-level settings and file hashes | yes |
| `preflight.json` | batch dir | pre-flight results: leak, readiness, oracle, sandbox probe | yes |
| `cells.jsonl` | batch dir | one line per cell: order, arm, timings, exit code, `failed`, `interrupted` | yes |
| `scores.jsonl` | batch dir | one scored line per cell | yes |
| `restore.json` | batch dir | flag restore: `restored_utc`, `cp_ok`, `default_variant`, `variant_after_restore`, `default_confirmed` | yes |
| `grades.jsonl` | batch dir | one line per grader call, `{label, reason, key, model, prompt_sha256}`; score.py reads it first | no |
| `RESULT.md` | `records/<experiment>/` | judge.py's answer for a judged pair, naming both batches; a re-run overwrites it | yes |
| `INDEX.md` | `records/` | results table and batch list; regenerated after each run and judge | yes |

## meta.json (50 keys, written before the client starts, rewritten at the end)

| Group | Keys | Source or meaning |
|---|---|---|
| Identity and order | `schema_version`, `run_id`, `batch_id`, `order_index`, `trial_index`, `seed`, `arm` | `run_id` is `<batch_id>/<order_index>-<arm>`; `seed` is `run.seed`, else random |
| Scenario and prompts | `scenario`, `scenario_sha256`, `prompt_name`, `prompt_sha256`, `system_prompt_sha256`, `verdict_schema_sha256` | sha256 of the files under `harness/` |
| Model and client | `client`, `provider`, `model_requested`, `effort`, `max_turns`, `max_budget_usd`, `client_argv`, `history_policy`, `client_version_pre` | `run.*`; argv has prompts as `sha256:`, paths made relative; version taken at batch start |
| Tools served | `mcp_endpoint`, `mcp_config_sha256`, `tools_list_sha256`, `tools_count`, `tool_descriptions_file`, `tool_descriptions_sha256`, `tool_descriptions_check` | endpoint is host-free, null with `tools: false`; check is `compared`, `recorded_only` or `no_tools` |
| System under test | `jaeger_image`, `jaeger_image_id`, `jaeger_commit`, `otel_demo_ref`, `fixture_overlay_sha256`, `system_under_test` | read from the fixture at batch start, not typed; `jaeger_commit` is `unresolved` if unknown; the last key is the block copied into each scores row |
| Fault | `fault`, `flag_names` | `fault` is flag, activation field and value, plus `flip_utc`, `ofrep_confirmed`, `signal_traces_seen`; `flag_names` feeds the leak tripwire |
| Fixture check | `fixture_changes`, `fixture_ok` | container restarts, OOM kills or start-time changes across the trial; `fixture_ok` false when one was not declared |
| Batch checks | `preflight`, `experiment` | `preflight` repeats `preflight.json`; `experiment` is `{name, file, sha256}` |
| Harness | `harness_git_sha`, `harness_dirty`, `score_py_sha256` | dirty means uncommitted changes under `harness` or `fixture` |
| Timing and exit | `started_utc`, `ended_utc`, `wall_time_s`, `exit_code` | `ended_utc` is null until the call returns; 127 if the client could not start |
| Observed | `observed`, `agent_loop` | `observed` is what the client reported (version, model, tools, skills, agents) plus `compaction_events`; `agent_loop` is the api loop's settings block, null for cli |

## manifest.json

Written before the fault flip and rewritten after the leak scan. It repeats the batch-level meta.json keys without the per-trial ones; they are not listed again. Manifest-only keys:

- `order` (the shuffled cell list), `n_per_arm`, `arms`, `file_hashes_sha256` (harness files, scenario, each prompt), `tools_list_path`.
- `arm_pin` (expected and running image) and `tools_description_check` (mode, file, hash), with one arm.
- `grader` (`model`, `prompt_sha256`, `cli_version`) and `scenario_version` (the scenario file's `version`).
- `experiment.content`, the experiment file verbatim; judge.py reads it with `experiment.sha256` and `arm_pin`.

## scores.jsonl

One line per cell: score.py's summary plus `arm`. An unscorable cell is `{dir, error, arm}`.

| Group | Keys |
|---|---|
| What the agent did | `dir`, `scenario_used`, `model_asserted`, `mcp_tools_visible`, `tool_calls`, `call_sequence`, `non_jaeger_tool_calls`, `call_errors`, `read_skill_attempted`, `read_skill_succeeded`, `read_skill_errors`, `steps_to_evidence`, `tool_output_chars` |
| Cost and turns | `input_tokens_total`, `output_tokens`, `num_turns`, `cost_usd`, `duration_s`, `stop`, `effort`, `compaction_events` |
| The answer and its grade | `locus`, `mechanism`, `mechanism_value`, `mechanism_grade`, `mechanism_grade_reason`, `grader_model`, `grader_prompt_sha256`, `grader_cached`, `cascade`, `abstained`, `verdict`, `verdict_source` |
| Validity | `signal_leaked_in_prompt`, `system_under_test`, `sandbox_ok`, `sandbox_violations`, `leak_hits`, `fixture_ok`, `fixture_changes`, `arm` |

- `stop` is the final result event's subtype (`success`, else the client's reason such as `error_max_turns`).
- `leak_hits` lists the tokens found in the answer or any assistant text: the batch's `flag_names`, `feature_flag`, `flagd`. Tokens only, never the text around them.
- `sandbox_violations` strings are `<kind>:<detail>` (`init_tool_extra`, `init_tool_missing`, `mcp_servers`, `executed_unknown_tool`, `attempted_unknown_tool`, `no_init_event`). `attempted_unknown_tool` is recorded but does not clear `sandbox_ok`.

## Verdicts that void a trial

Scoring rules are in docs/USAGE.md, Scoring.

- INVALID: `sandbox_ok` false, or `fixture_ok` false (an undeclared fixture change). Counted apart, never a pass, and `verify` fails.
- LEAK: non-empty `leak_hits`, checked after the sandbox; counted like INVALID, in its own column.
- ERROR and stops: `{dir, error, arm}` rows count as `ERROR=<k>`, every non-`success` `stop` as `stops=`; both stay inside n.
