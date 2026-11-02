Note on paths: `rc_probe.py` is published as it ran, with local absolute paths. Its imports map to published files as
follows. `probe` (sys.path `.../probes/first-move-bounds-2026-10-28`) is `records/first-move-2026-10-28/probe.py`;
`bench` and `config` (sys.path `/home/oni/jaeger-mcp-evals/harness`) are `harness/bench.py` and `harness/config.py` in this
repo. `HEAD` (`.../probes/handshake-2026-10-30-head/`) is `handshake-head-e5274265/` here. `git -C /home/oni/jaeger show
<sha>:` reads files from a local clone of jaegertracing/jaeger (238c5fa2, e5274265). `REC` points at the unpublished stream.jsonl
of batch-20261028T035651Z/0-baseline (that batch is `records/skill-callee-down-main238-cfg/`). Relative references such as
`../cli-api-gap-2026-10-30/runs-cwd` and `../handshake-2026-10-30-head/` in the text below are local-workspace paths and
are not in this repo. The `plugins` entry in each run's stream.jsonl init line names a local plugin path.

# Root cause of the skill skip on main, 2026-10-30 (Roshan GO)

rc_probe.py. CLI 2.1.285, claude-sonnet-5-5 pinned, effort high, API key, harness argv, stub MCP server with main's
captured handshake, fresh /tmp working dir per trial (as the full runs), arms alternating in shuffled pairs.
All 60 split trials checked: model claude-sonnet-5-5, CLI 2.1.285, cwd /tmp/tmp*, rc 1 (the normal --max-turns exit).

## Step 1, split #9262's two search_traces edits: runs-split/ (n=20 per arm, first message)
| arm | service_name required | "Omit to search all services..." sentence | read_skill | first message |
|---|---|---|---|---|
| main | no | yes | 0/20 | search_traces + get_services 20/20 |
| noomit | no | no | 0/20 | search_traces + get_services 19, reversed order 1 |
| reqomit | yes | yes | 12/20 | get_services + read_skill 12, get_services alone 8 |
Fisher: main vs reqomit p = 4.5e-5; main vs noomit p = 1. The fourth corner (required, no sentence) is svcreq:
15/20 in ../cli-api-gap-2026-10-30/runs-cwd (tmpcwd arm), vs reqomit p = 0.50.
Result: the sentence does nothing; the optional service_name is the cause. When search_traces can run without a
service, the first message is search_traces(with_errors) + get_services and no skill read. When it needs a service,
search_traces is never in the first message (0/20); the agent lists services and, in 12 of 20, reads SKILL.md beside it.

## Step 2, self-report: DROPPED
Smoke trial (smoke/selfreport/000-main): the follow-up question ("why did you choose those first calls, did you consider
read_skill") was refused by the API safeguard, details `[reasoning_extraction]`. Not reworded to get around it.
The partial thinking summary of the refused turn says it "didn't actually weigh read_skill" and "followed the
instruction to prefer broad discovery tools first" (the server instructions: "prefer starting with broad discovery
(`get_services` or `search_traces`)"). n=1, refused turn: a lead, not a finding.

## Step 3, Jonah's pointer in the search_traces result: runs-pointer/ (Roshan GO 2026-10-30, n=10 per arm, message 2)
| arm | read_skill in message 2 |
|---|---|
| plain | 0/10 |
| pointer (POINTER v1 as a second text block in the search_traces result) | 0/10 |
Fisher p = 1. Message 2 is get_trace_errors / get_trace_topology in all 20; the pointer is not acted on.

## Step 4, candidate fixes: runs-fix/ (Roshan GO 2026-10-30, n=20 per arm, first message, mode fix, texts v1 from jaeger 238c5fa2)
| arm | change | read_skill | first message |
|---|---|---|---|
| main | none | 0/20 | search_traces + get_services 19, reversed 1 |
| idx | root SKILL.md "Available Skills" bullets appended to the read_skill description (Yuri's proposal) | 0/20 | search_traces + get_services 20/20 |
| instr | server instructions: Skills section before Investigation Strategy; "prefer starting with broad discovery (...)" becomes "prefer starting with `read_skill` (path `SKILL.md`) to choose an analysis approach, then broad discovery (...)" | 20/20 | read_skill + search_traces 17, + get_services 3 |
| idxinstr | both | 19/20 | read_skill + search_traces 18, search_traces + read_skill 1, search_traces + get_services 1 |
Fisher vs main (bench.fisher_p): idx p = 1, instr p = 1.5e-11, idxinstr p = 3e-10. Path read: SKILL.md 38, error-root-cause/SKILL.md 1 (idxinstr).
Trajectories read (0/20 and 20/20 rule): no first message carries thinking or text; server logs show initialize, tools/list, then the calls above.
Delivery check, zero cost (scratchpad capfix.py: dummy key, API pointed at a local 400 stub, same argv): the request
the CLI builds carries the index in read_skill's description for idx and idxinstr only (158 vs 568 chars), and the new
instructions sentence (old one absent) for instr and idxinstr only. So idx's 0/20 is the text reaching the model and not moving it.
All 120 trials: model claude-sonnet-5-5, CLI 2.1.285, cwd /tmp/tmp*, rc 0 or 1.
Result: on the first move, the server instructions' ranking line decides it; the skill index in the tool description does not.
Scope: first message only, one prompt, one model, one CLI. Whether a first-move skill read changes the verdict is Stage 2
(notes/plan-skill-skip-fix-2026-10-30.md).

## Step 5, split instr's two edits: runs-fixsplit/ (Roshan GO 2026-10-30, n=20 per arm, first message, mode fixsplit)
| arm | Skills section moved above Investigation Strategy | starting-point sentence reworded to read_skill first | read_skill |
|---|---|---|---|
| main | no | no | 0/20 |
| move | yes | no | 0/20 |
| reword | no | yes | 20/20 |
| instr | yes | yes | 20/20 |
Fisher vs main: move p = 1, reword p = 1.5e-11. All 80 trials: CLI 2.1.285, claude-sonnet-5-5, /tmp cwd, rc 0 or 1.
Result: the reworded sentence alone does it; the section's position does nothing. In reword, read_skill is in the same
first message as search_traces in 20/20, so the skill does not shape the first query.
Also checked 2026-10-30: INSTRUCTIONS.md is byte-identical at v2.20.0 and 238c5fa2 (git diff empty), so 2.20.0's skill
reads came from the required service_name, not from the instructions.

## Step 6, current upstream main: runs-head/ (Roshan GO 2026-10-30, n=20 per arm, first message, mode head)
238c5fa2 predates #9925 (merged 2026-10-25: sql input on search_traces, plus an INSTRUCTIONS.md line pointing at the
jaeger-sql skill). Handshake captured from upstream/main e5274265 built and run locally: ../handshake-2026-10-30-head/.
| arm | read_skill | first message |
|---|---|---|
| head (as served) | 0/20 | search_traces + get_services 20/20 |
| headreword (starting-point sentence reworded, nothing else) | 20/20 | read_skill + search_traces 19, + get_services 1 |
Fisher p = 1.5e-11. All 40 trials: CLI 2.1.285, claude-sonnet-5-5, /tmp cwd, rc 0 or 1. Path SKILL.md 20/20.
No trial used sql or passed service_name; no thinking text in any first message. The new jaeger-sql pointer does not
lead to a skill read. Leak gate: served tools/list hit "error flag" only, the phrase bench.py TOOLS_ALLOW exempts
(also present in the 238c5fa2 tools); PASS with it stripped.

Spend: smoke 0.085, split 0.173, fix 0.276, pointer 0.171, fixsplit 0.267, head 0.143 (CLI-reported).
