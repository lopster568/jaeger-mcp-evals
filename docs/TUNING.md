# Tuning factors

Every factor that can change a result, where it is set, and its record keys (full lists in docs/RECORD.md).
A run is tuned only by editing its experiment file, `harness/experiments/<name>.json` (docs/USAGE.md, The experiment file). The manifest records that file and its sha256.

| Factor | Where it is set | Recorded as |
|---|---|---|
| Client and provider | `run.client`, `run.provider` | `client`, `provider`, `client_argv` |
| Model | `run.model` | `model_requested`, `observed.model`, `model_asserted` |
| Effort | `run.effort` | `effort` |
| Thinking | fixed per client | `agent_loop.thinking`, `reasoning.jsonl` |
| Temperature | not settable | nothing |
| Prompt | `arms.<arm>.prompt` | `prompt_name`, `prompt_sha256` |
| System prompt | `harness/system-prompt.txt` | `system_prompt_sha256` |
| Tools on or off | `arms.<arm>.tools` | `mcp_endpoint`, `tool_descriptions_check` |
| Tool descriptions | `arms.<arm>.image` | `tools_list_sha256`, `tool_descriptions_sha256`, `tool_descriptions_check` |
| Skills | `skill` or `noskill` prompt | `read_skill_attempted`, `read_skill_succeeded` |
| Max turns | `run.max_turns` | `max_turns` |
| Budget | `run.max_budget_usd` | `max_budget_usd`, `cost_usd` |
| History and compaction | harness truncates nothing | `history_policy`, `compaction_events` |
| Jaeger image and commit | `arms.<arm>.image` | `jaeger_image`, `jaeger_image_id`, `jaeger_commit`, `arm_pin` |
| OTel Demo ref and overlay | `fixture/` | `otel_demo_ref`, `fixture_overlay_sha256` |
| Mechanism grader | `GRADER_MODEL`, `harness/grader-prompt.txt` | manifest `grader`, `mechanism_grade`, `grader_model` |
| Scenario and fault | `scenario` | `scenario_sha256`, `scenario_version`, `fault` |
| Client version | installed `claude` or `codex`; `run.client_version` | `client_version_pre`, `observed.client_version` |
| Trials, seed, order | `run.n_per_arm`, `run.seed` | `n_per_arm`, `seed`, `order_index` |

## Things that do not compare or cannot be set

- An alias model is warned, not pinned; use a full id.
- Recorded effort is the request; always set `run.effort` and compare arms only at the same effort.
- Temperature cannot be set, so n of at least 10 shows the variance. Arms on different images run as separate consecutive batches, not shuffled across arms.
- `num_turns` and cost do not compare across clients. The cli client on a claude.ai login adds the account email to the model's context, in every arm; API-key auth avoids it.
- The CLI's autocompact cannot be switched off; compaction is flagged by `band` and `verify`.
- The client can self-update between batches unless `run.client_version` pins it.

## Mechanism grader

- Labels the agent's mechanism `correct`, `incorrect` or `unclear` against the scenario's `mechanism_truth`.
- Model `claude-fable-5-1`, pinned in `GRADER_MODEL`; prompt `harness/grader-prompt.txt`, recorded by sha256.
- Runs through the Claude Code CLI sandboxed like the agent.
- Every label is cached in the batch's `grades.jsonl`. Only the end of `bench.py run` calls the grader, and `verify` fails on a miss. Changing model, prompt or truth changes the key.
- Validation: on a blind 94-item set of real and constructed explanations, the grader's labels matched a human labeler on all 21 items the human labeled, and were identical across 3 passes on all 94; human-labeled evidence on near-miss explanations (right place, wrong cause) is still thin. The validation record is kept outside this repository.
