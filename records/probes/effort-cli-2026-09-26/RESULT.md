# Probe 2026-09-26: CLI version or --effort?

Question: in the desc-change-sep28 baseline batch (records/desc-change-sep28/batch-20260926T141252Z) the
no-skill agent's first call was read_skill in 8 of 10 runs, and it read the skill in 10 of 10. Earlier pilot
runs, not in records/, opened with search_traces and get_services instead. Two inputs had changed between them:
Claude Code CLI 2.1.278 to 2.1.283, and no --effort to --effort xhigh.

Method: probe.py. Same argv as the desc-change-sep28 baseline trials (noskill prompt, system prompt, verdict
schema, --strict-mcp-config, --setting-sources "", --restricted, --tools "", --allowedTools mcp__jaeger__*,
--model sonnet), --max-turns 2, fixture on stock quay.io/jaegertracing/jaeger:2.20.0, no fault flipped. CLI
2.1.278 from npm (set CLAUDE_2_1_278 to its binary); 2.1.283 installed as `claude`. 4 cells x 5 runs,
interleaved in a seeded random order. Every run exited 1 at the turn limit; model claude-sonnet-5 in all 20.
Cost 0.27 USD.

| read_skill in turn 1 | no --effort | --effort xhigh |
|---|---|---|
| CLI 2.1.278 | 0/5 | 5/5 |
| CLI 2.1.283 | 0/5 | 5/5 |

No --effort: turn 1 is search_traces + get_services. --effort xhigh: turn 1 is read_skill + get_services.
Per-run rows are in results.jsonl. The per-run stream folders probe.py writes beside itself hold session
identifiers and are not committed.

Conclusion (n=5 per cell, a ranking, but unanimous): the effort flag, not the CLI version, causes the
unprompted read_skill. Which named level equals "no --effort" is not settled here; see
records/probes/effort-level-2026-09-26.
