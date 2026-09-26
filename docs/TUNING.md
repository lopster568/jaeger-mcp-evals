# Tuning factors

Every factor that can change a run's result, where it is set, and which record key holds it (key details in docs/RECORD.md).
Tune a run by editing its experiment file, `harness/experiments/<name>.json` (docs/USAGE.md, The experiment file). No flag or fixture.env key changes a run, and the manifest records the file and its sha256, so two batches that ran the same file ran the same settings.

| Factor | Where it is set | Where it is recorded |
|---|---|---|
| Client and provider | `run.client` (api, cli, codex); `run.provider` (api only: anthropic, openai) | `client`, `provider`; `client_argv` |
| Model | `run.model` (the api client refuses aliases; the CLI accepts one such as `sonnet`) | `model_requested`; `observed.model` from the init event; `model_asserted` in scores.jsonl |
| Effort | `run.effort` (low, medium, high, xhigh, max); it changes which tools the agent calls, not only how long it thinks | `effort` in meta.json, manifest.json and scores.jsonl; the stream carries no effort value, so it is the request, not an observation |
| Thinking | api: fixed at adaptive with summaries; cli and codex: not controllable | api: `agent_loop.thinking` and `reasoning.jsonl`; cli: thinking blocks in `stream.jsonl` |
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
| Scenario and fault activation | `scenario`, naming `harness/scenarios/<name>.json` (`flag`, `activation`) | `scenario`, `scenario_sha256`, `scenario_version`, `fault` (flag, activation, flip time, OFREP confirmation, fault traces seen) |
| Client version | The installed `claude` or `codex`, which can self-update between batches; for api, the loop's code. Optional `run.client_version` pins it: a batch refuses to start on any other version | `client_version_pre`, `observed.client_version`; `client_version` in preflight.json; manifest `file_hashes_sha256` |
| Trials, seed and cell order | `run.n_per_arm`; `run.seed` (null draws one), which shuffles all cells of all arms together | `n_per_arm`, `seed`, `order_index`, `trial_index`; manifest `seed` and `order` |

Numbers do not compare across clients or providers: `num_turns` and cost are counted differently by each.

Arms compared with each other, and batches compared across dates, must run at the same effort. An unset effort is the CLI's own default and is not recorded, so always set `run.effort`.

The model these runs use rejects a temperature parameter and the CLI has no flag for it; the substitutes are effort, the model id and the trial count.
Instead, every arm runs n>=10 trials before a rate is quoted, in one shuffled order across arms, so the variance shows.
