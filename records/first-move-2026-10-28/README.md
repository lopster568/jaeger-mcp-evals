# first-move-2026-10-28

What the agent plans in its first message, given only the MCP handshake. A local stub serves a build's captured
`initialize` and `tools/list` verbatim, answers every `tools/call` with an error, and the CLI runs with `--max-turns 1`.
Same model, effort, prompt and system prompt as the batches (`harness/prompts/neutral.txt`). 30 trials per build.

| Tools served by | read_skill in first message | Log |
| --- | --- | --- |
| main 238c5fa2 | 0/30 | `run-main.log`, `summary-main.json` |
| main 238c5fa2 with `service_name` required | 27/30 | `run-v2200-svcreq.log` (arm `svcreq`), `summary-v2200-svcreq.json` |
| v2.20.0 + main's skill text | 29/30 | `run-v2200-svcreq.log` (arm `v2200`), `summary-v2200-svcreq.json` |

Handshakes, captured live from each image:

- `initialize-tools-list-v2200-vs-svcreq.txt`: v2.20.0 build (`26686`) and `service_name`-required build (`26687`)
- `main238-init.sse`, `main238-tools.sse`: main 238c5fa2

`probe.py` produced the logs: run once with the default arms (`v2200,svcreq`) and once with `PROBE_ARMS=main`, 30 trials
per arm by default.
