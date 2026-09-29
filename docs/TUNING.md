# Tuning factors

Every factor that can change a run's result, where it is set, and which record key holds it (key details in docs/RECORD.md).
Tune a run by editing its experiment file, `harness/experiments/<name>.json` (docs/USAGE.md, The experiment file). No flag or fixture.env key changes a run, and the manifest records the file and its sha256, so two batches that ran the same file ran the same settings.

| Factor | Where it is set | Where it is recorded |
|---|---|---|
| Client and provider | `run.client` (api, cli, codex); `run.provider` (api only: anthropic, openai) | `client`, `provider`; `client_argv` |
| Model | `run.model` (the api client refuses aliases; the CLI accepts one such as `sonnet`) | Pin: the api client sends one tiny probe in pre-flight and refuses the batch if the endpoint answers as another model; the cli aborts after a cell whose stream reports a model other than a full-id `run.model` (an alias is warned, not pinned). `model_requested`; `observed.model` from the init event; `model_asserted` in scores.jsonl |
| Effort | `run.effort` (low, medium, high, xhigh, max); it changes which tools the agent calls, not only how long it thinks: with the CLI, xhigh puts `read_skill` in the first turn and unset, medium and high do not (records/probes/) | `effort` in meta.json, manifest.json and scores.jsonl; the stream carries no effort value, so it is the request, not an observation |
| Thinking | api anthropic: fixed at adaptive with summaries; api openai: the effort is sent as `reasoning_effort` and the signed thinking blocks are echoed back each turn; cli and codex: not controllable | api: `agent_loop.thinking` and `reasoning.jsonl`; cli: none visible. `stream.jsonl` carries thinking blocks with a signature and empty text (46 blocks, all empty, in the 20 desc-change-sep28 runs) |
| Temperature | Not settable: the model rejects it, the CLI has no flag, the loop refuses to send it | Nothing to record |
| Prompt | `arms.<arm>.prompt`, a `harness/prompts/<name>.txt` file | `prompt_name`, `prompt_sha256`; trial `prompt.txt`; manifest `file_hashes_sha256` |
| System prompt | `harness/system-prompt.txt` | `system_prompt_sha256`; trial `system-prompt.txt` |
| Tools on or off | `arms.<arm>.tools` | `mcp_endpoint`, `tool_descriptions_check` (`no_tools`) |
| Tool descriptions | The fixture's Jaeger image, `arms.<arm>.image`; a variant image bakes in a wording file (fixture/build-variant.sh), checked against `desc-change.json` for a non-baseline arm on its own image | `tools_list_sha256`, `tools_count`, `tool_descriptions_file`, `tool_descriptions_sha256`, `tool_descriptions_check`; batch `tools.json` |
| Skills | Served by Jaeger through `read_skill`; the `skill` prompt says to read one, `noskill` does not | `read_skill_attempted`, `read_skill_succeeded`, `read_skill_errors` in scores.jsonl |
| Max turns | `run.max_turns` (API calls for api, CLI turns for cli, tool calls for codex) | `max_turns`; `client_argv` |
| Budget | `run.max_budget_usd` (no effect with provider openai or client codex) | `max_budget_usd`; `cost_usd` in scores.jsonl |
| History and compaction | Harness truncates nothing; the api loop never compacts; CLI autocompact runs at its default and cannot be switched off | `history_policy`; `observed.compaction_events`; `compaction_events` in scores.jsonl, flagged by `band` and `verify` |
| Jaeger image and commit | `arms.<arm>.image`, served by `JAEGERTRACING_IMAGE` in `.env.override` (fixture/env.override) | `jaeger_image`, `jaeger_image_id` (`docker inspect jaeger` at batch start), `jaeger_commit`, `arm_pin` |
| OTel Demo ref and overlay | The fixture's demo checkout and the overlay files in `fixture/` | `otel_demo_ref`, `fixture_overlay_sha256` |
| Mechanism grader | `GRADER_MODEL` in `harness/grade.py` and the prompt `harness/grader-prompt.txt`; the scenario's `mechanism_truth` | manifest `grader` (`model`, `prompt_sha256`, `cli_version`); per row `mechanism_grade`, `mechanism_grade_reason`, `grader_model`, `grader_prompt_sha256`, `grader_cached`; `grades.jsonl` |
| Scenario and fault activation | `scenario`, naming `harness/scenarios/<name>.json` (`flag`, `activation`) | `scenario`, `scenario_sha256`, `scenario_version`, `fault` (flag, activation, flip time, OFREP confirmation, fault traces seen) |
| Client version | The installed `claude` or `codex`, which can self-update between batches; for api, the loop's code. Optional `run.client_version` pins it: a batch refuses to start on any other version | `client_version_pre`, `observed.client_version`; `client_version` in preflight.json; manifest `file_hashes_sha256` |
| Trials, seed and cell order | `run.n_per_arm`; `run.seed` (null draws one), which shuffles all cells of the arms in one batch together | `n_per_arm`, `seed`, `order_index`, `trial_index`; manifest `seed` and `order` |

Numbers do not compare across clients or providers: `num_turns` and cost are counted differently by each.

With the cli client on a claude.ai login the CLI adds the account's email to the model's context (seen in the sandbox probe's captured request, 2026-09-27); it is the same in every arm, and API-key auth avoids it.

## Mechanism grader

The agent states the mechanism in its own words; `harness/grade.py` labels it `correct`, `incorrect` or `unclear` against the scenario's one-sentence `mechanism_truth`.
Model: `claude-fable-5-1`, pinned in `GRADER_MODEL`. Prompt: `harness/grader-prompt.txt` (the labeling instructions and three labeling rules the human labels were made under), recorded by sha256.
It runs through the Claude Code CLI sandboxed like the agent: `--tools ""`, `--strict-mcp-config` with an empty MCP config, `--setting-sources ""`, `--restricted`, `--no-session-persistence`, a temporary working directory, no API keys or `CLAUDECODE` in the environment, and `--json-schema` forcing `{label, reason}`. The agent's text reaches it only in the user prompt.
Caching: every label is stored in the batch's `grades.jsonl`, keyed by sha256 of (model, prompt sha256, truth, agent text). Only the scoring step at the end of `bench.py run` calls the grader, once per answer not already cached; `verify`, `band` and `score.py` read the cache, and `verify` fails on a miss instead of calling. Changing the model, the prompt or a `mechanism_truth` changes the key, so old labels are never reused for a new grader.
Validation: on a blind 94-item set of real and constructed explanations, the grader's labels matched a human labeler on all 21 items the human labeled, and were identical across 3 passes on all 94; human-labeled evidence on near-miss explanations (right place, wrong cause) is still thin. The validation record is kept outside this repository.

`effort` in meta.json and manifest.json is the request. Whether it reached the model is recorded per trial in `agent_loop.effort_applied` (true when the loop sent it: `output_config.effort` on anthropic, `reasoning_effort` on openai, where `agent_loop.thinking` reads `reasoning_effort`) and shows in `reasoning.jsonl` as thinking text and `usage.reasoning_tokens`. Provider openai batches before 2026-09-29 record an effort but sent no `reasoning_effort` (their `agent_loop.effort` is `n/a`) and have no reasoning.

Arms compared with each other, and batches compared across dates, must run at the same effort. An unset effort is the CLI's own default and is not recorded, so always set `run.effort`.

The model these runs use rejects a temperature parameter and the CLI has no flag for it; the substitutes are effort, the model id and the trial count.
Instead, every arm runs n>=10 trials before a rate is quoted, so the variance shows. Arms on the same image share one shuffled order; arms on different images run as separate batches, one after the other, so their order across arms is not shuffled.
