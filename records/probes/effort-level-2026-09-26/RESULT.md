# Probe 2026-09-26: which named effort level matches "no --effort"?

Same method as records/probes/effort-cli-2026-09-26 (argv of the desc-change-sep28 baseline trials,
--max-turns 2, stock quay.io/jaegertracing/jaeger:2.20.0, no fault), CLI 2.1.283 only, 5 runs per level,
seeded interleaved order. Model claude-sonnet-5 in all 10. Cost 0.12 USD.

| read_skill in turn 1 | result |
|---|---|
| no --effort (effort-cli probe, same day) | 0/5 |
| --effort medium | 0/5 |
| --effort high | 0/5 |
| --effort xhigh (effort-cli probe, same day) | 5/5 |

Turn 1 without read_skill is search_traces + get_services in 9 of 10 (one medium run: search_traces alone).
Per-run rows are in results.jsonl; the stream folders are not committed.

Conclusion (n=5, first move only): the opening flips between high and xhigh. This probe cannot tell whether
"no --effort" equals high or medium. The next version of desc-change will run at high, the highest level
that keeps the opening seen without --effort. Whether a full run at high changes the tool use recorded for
desc-change-sep28 is untested.
