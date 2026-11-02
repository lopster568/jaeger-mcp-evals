"""Root cause of the skill skip on main, 2026-10-30 (Roshan GO). CLI 2.1.285, claude-sonnet-5-5 via the harness argv,
API key, stub MCP server (probe.serve) with main's captured handshake, fresh tempfile.mkdtemp() working dir per trial
(as the full runs). Arms alternate in shuffled pairs. Usage: rc_probe.py <mode> <n per arm>. Modes:
  split      #9262 made two edits to search_traces: service_name no longer required, and the sentence "Omit to search
             all services, which some storage backends do not support" in its description. Arms: main (both edits),
             noomit (not required, sentence removed), reqomit (required, sentence kept). The fourth corner, required
             without the sentence, is svcreq: 15/20 in ../cli-api-gap-2026-10-30/runs-cwd (tmpcwd arm).
             --max-turns 1, read: read_skill in the first message.
  selfreport main only. Leg 1: harness argv, --max-turns 1, every tools/call answered "paused". Leg 2: --resume of that
             session with QUESTION as the user turn, no --json-schema, --max-turns 1, --thinking-display summarized.
             The answer is a post-hoc self-report, not the reasoning that made the first move.
  pointer    Jonah's idea. --max-turns 2. search_traces and get_services answered with the results recorded in main
             full run batch-20261028T035651Z/0-baseline; arm "pointer" appends POINTER (v1) to the search_traces result.
             Read: read_skill in the second message.
  fix        Candidate fixes, v1, texts from jaeger 238c5fa2 (never the working tree). Arms: main; idx (the root SKILL.md
             bullets under "## Available Skills" appended to the read_skill tool description); instr (the "## Skills"
             section moved before "## Investigation Strategy" and the discovery sentence made to start with read_skill);
             idxinstr (both). --max-turns 1, read: read_skill in the first message."""
import copy, json, os, random, re, subprocess, sys, tempfile, time
from types import SimpleNamespace
sys.path.insert(0, "/home/oni/jaeger-mcp-evals/harness")
sys.path.insert(0, "/home/oni/career/lfx-jaeger/probes/first-move-bounds-2026-10-28")
MODE, N = sys.argv[1], int(sys.argv[2])
sys.argv[1:] = []  # probe.py reads sys.argv[1] at import
import probe, bench, config

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = "<local install of Claude Code CLI 2.1.285>/node_modules/.bin/claude"  # path redacted for publication
OUT = os.path.abspath(os.environ.get("PROBE_OUT") or os.path.join(HERE, "runs-" + MODE))
REC = "/home/oni/career/lfx-jaeger/runs/recommendationCacheFailure/batch-20261028T035651Z/0-baseline/stream.jsonl"
QUESTION = ("I paused those calls before they ran. Before you continue, explain in a few sentences why you chose those "
            "first calls. Did you consider read_skill at that point? Why did or didn't you call it first? "
            "Answer in text only; do not call any tools.")
POINTER = "For guidance on analyzing these traces, call `read_skill` with path `SKILL.md`."  # v1, wording of the server's own Skills instructions

PIN = "238c5fa2"
MCP = "cmd/jaeger/internal/extension/jaegerquery/internal/mcptools/"


def pinned(path):
    return subprocess.run(["git", "-C", "/home/oni/jaeger", "show", PIN + ":" + MCP + path], capture_output=True, text=True, check=True).stdout


def variant(required, omit):
    t = copy.deepcopy(probe.TOOLS["main"])
    sch = [x for x in t["tools"] if x["name"] == "search_traces"][0]["inputSchema"]
    if required:
        sch["required"] = ["service_name"]
    if not omit:
        d = sch["properties"]["service_name"]["description"]
        sch["properties"]["service_name"]["description"] = d[:d.index(". Omit to search all services")]
    return t


probe.TOOLS["noomit"] = variant(False, False)
probe.TOOLS["reqomit"] = variant(True, True)
assert variant(True, False) == probe.TOOLS["svcreq"], "required+no sentence must equal the captured svcreq tools"
assert variant(False, True) == probe.TOOLS["main"]


def fix_tools(idx):
    t = copy.deepcopy(probe.TOOLS["main"])
    if idx:
        root = pinned("skills/SKILL.md")
        bullets = root[root.index("## Available Skills") + len("## Available Skills"):root.index("Installation-specific")].strip()
        r = [x for x in t["tools"] if x["name"] == "read_skill"][0]
        r["description"] += "\n\n" + bullets
    return t


def fix_init(move, reword):
    i = copy.deepcopy(probe.INIT)
    if move or reword:
        intro, inv, skills = i["instructions"].strip().split("\n\n## ")
        assert inv.startswith("Investigation Strategy") and skills.startswith("Skills")
        old = "prefer starting with broad discovery (`get_services` or `search_traces`)"
        assert inv.count(old) == 1
        if reword:
            inv = inv.replace(old, "prefer starting with `read_skill` (path `SKILL.md`) to choose an analysis approach, then broad discovery (`get_services` or `search_traces`)")
        i["instructions"] = "\n\n## ".join([intro, skills, inv] if move else [intro, inv, skills]) + "\n"
    return i


INSTR = pinned("INSTRUCTIONS.md")
assert probe.INIT["instructions"].strip() == INSTR.strip(), "stub must serve main's real instructions"
FIX = {"main": (probe.TOOLS["main"], probe.INIT), "idx": (fix_tools(True), fix_init(False, False)),
       "instr": (fix_tools(False), fix_init(True, True)), "idxinstr": (fix_tools(True), fix_init(True, True)),
       "move": (fix_tools(False), fix_init(True, False)), "reword": (fix_tools(False), fix_init(False, True))}
HEAD = "/home/oni/career/lfx-jaeger/probes/handshake-2026-10-30-head/"  # upstream main e5274265, after #9925 (sql in search_traces)
_res = lambda f: json.loads(re.search(r"^data: (.*)$", open(HEAD + f).read(), re.M).group(1))["result"]
_hi, _ht = _res("head-init.sse"), _res("head-tools.sse")
assert _hi["instructions"].strip() == subprocess.run(["git", "-C", "/home/oni/jaeger", "show", "e5274265:" + MCP + "INSTRUCTIONS.md"],
                                                     capture_output=True, text=True, check=True).stdout.strip()
_old = "prefer starting with broad discovery (`get_services` or `search_traces`)"
assert _hi["instructions"].count(_old) == 1
_hr = dict(_hi, instructions=_hi["instructions"].replace(_old, "prefer starting with `read_skill` (path `SKILL.md`) to choose an analysis approach, then broad discovery (`get_services` or `search_traces`)"))
FIX.update(head=(_ht, _hi), headreword=(_ht, _hr))
probe.TOOLS.update(head=_ht, headreword=_ht)
probe.TOOLS.update(idx=FIX["idx"][0], instr=FIX["instr"][0], idxinstr=FIX["idxinstr"][0], move=FIX["move"][0], reword=FIX["reword"][0])


def _only(a, b, key):
    assert a.keys() == b.keys() and all(a[k] == b[k] for k in a if k != key) and a[key] != b[key]


_rs = lambda t: [x for x in t["tools"] if x["name"] == "read_skill"][0]
_m, _i = copy.deepcopy(probe.TOOLS["main"]), copy.deepcopy(FIX["idx"][0])
assert _i["tools"] != _m["tools"] and _rs(_i)["description"].startswith(_rs(_m)["description"])
_rs(_i)["description"] = _rs(_m)["description"]
assert _i == _m, "idx differs from main only in read_skill description"
assert FIX["instr"][0] == probe.TOOLS["main"] and FIX["idxinstr"][0] == FIX["idx"][0]
for _a in ("instr", "move", "reword"):
    _only(FIX[_a][1], probe.INIT, "instructions")
assert FIX["move"][1]["instructions"].count("## Skills") == 1 and "prefer starting with broad" in FIX["move"][1]["instructions"]
assert FIX["reword"][1]["instructions"].index("## Investigation") < FIX["reword"][1]["instructions"].index("## Skills")
_old, _new = probe.INIT["instructions"].strip().splitlines(), FIX["instr"][1]["instructions"].splitlines()
assert all(l in _new for l in _old if "prefer starting with broad" not in l), "the move lost a line"


def recorded():
    """First result text per tool name in the recorded main run."""
    calls, res = {}, {}
    for line in open(REC):
        e = json.loads(line)
        for c in (e.get("message") or {}).get("content") or []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use":
                calls[c["id"]] = c["name"].split("__")[-1]
            elif c.get("type") == "tool_result" and calls.get(c["tool_use_id"]) not in res:
                x = c["content"]
                res[calls[c["tool_use_id"]]] = x if isinstance(x, str) else "".join(b.get("text", "") for b in x)
    return res


def messages(path):
    """[(tool names, text, thinking)] per assistant message, in order. The CLI streams one event per content block."""
    order, got = [], {}
    for line in open(path):
        e = json.loads(line)
        if e.get("type") != "assistant":
            continue
        m = e["message"]
        if m["id"] not in got:
            order.append(m["id"]); got[m["id"]] = ([], [], [])
        for c in m.get("content", []):
            if c["type"] == "tool_use":
                got[m["id"]][0].append(c["name"].split("__")[-1])
            elif c["type"] == "text":
                got[m["id"]][1].append(c["text"])
            elif c["type"] == "thinking":
                got[m["id"]][2].append(c.get("thinking", ""))
    return [got[i] for i in order]


def facts(path):
    init = cost = sid = None
    for line in open(path):
        e = json.loads(line)
        if e.get("type") == "system" and e.get("subtype") == "init":
            init = {"version": e.get("claude_code_version"), "model": e.get("model"), "cwd": e.get("cwd"),
                    "apiKeySource": e.get("apiKeySource")}
            sid = e.get("session_id")
        if e.get("type") == "result":
            cost = e.get("total_cost_usd")
    return init, cost, sid


def cli(argv, d, name, cwd):
    with open(os.path.join(d, name), "w") as out, open(os.path.join(d, name + ".stderr"), "w") as err:
        return subprocess.run(argv, stdout=out, stderr=err, stdin=subprocess.DEVNULL, env=env, cwd=cwd, timeout=600).returncode


env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE",) + bench.SECRET_KEYS}
env.update(bench.api_secrets(SimpleNamespace(provider=None), config.load()))
env["CLAUDE_CONFIG_DIR"] = "/home/oni/.claude"
assert subprocess.run([CLI, "--version"], capture_output=True, text=True, env=env).stdout.startswith("2.1.285")
ARMS = {"split": ["main", "noomit", "reqomit"], "selfreport": ["main"], "pointer": ["plain", "pointer"],
        "fix": ["main", "idx", "instr", "idxinstr"], "fixsplit": ["main", "move", "reword", "instr"], "head": ["head", "headreword"]}[MODE]
REC_RES = recorded() if MODE == "pointer" else None
rng = random.Random(20261030)
order = []
for _ in range(N):
    pair = list(ARMS)
    rng.shuffle(pair)
    order += pair
os.makedirs(OUT, exist_ok=True)
summary = {arm: [] for arm in ARMS}
for i, arm in enumerate(order):
    d = os.path.join(OUT, "%03d-%s" % (i, arm))
    os.makedirs(d, exist_ok=True)
    cwd, log = tempfile.mkdtemp(), []
    if MODE == "pointer":
        def call(name, args, arm=arm):
            if name not in REC_RES:
                return {"content": [{"type": "text", "text": "unavailable"}], "isError": True}
            content = [{"type": "text", "text": REC_RES[name]}]
            if arm == "pointer" and name == "search_traces":
                content.append({"type": "text", "text": POINTER})
            return {"content": content, "isError": False}
    elif MODE == "selfreport":
        call = lambda name, args: {"content": [{"type": "text", "text": "paused"}], "isError": True}
    else:
        call = None
    if MODE in ("fix", "fixsplit", "head"):
        probe.INIT = FIX[arm][1]  # probe.serve reads INIT per request; trials are sequential and each server is shut down first
    srv = probe.serve(arm if MODE in ("split", "fix", "fixsplit", "head") else "main", log, call)
    mcp = os.path.join(d, "mcp.json")
    json.dump({"mcpServers": {"jaeger": {"type": "http", "url": "http://127.0.0.1:%d/jaeger/ui/api/ai/mcp/" % srv.server_port}}}, open(mcp, "w"))
    a = SimpleNamespace(model="claude-sonnet-5-5", effort="high", max_turns=2 if MODE == "pointer" else 1, max_budget_usd="2")
    argv = bench.claude_argv(a, probe.PROMPT, mcp)
    argv[0] = CLI
    rc = cli(argv, d, "stream.jsonl", cwd)
    msgs = messages(os.path.join(d, "stream.jsonl"))
    init, cost, sid = facts(os.path.join(d, "stream.jsonl"))
    rec = {"i": i, "arm": arm, "rc": rc, "messages": [m[0] for m in msgs], "init": init, "cost": cost, "server_log": log}
    if MODE == "selfreport":
        j = argv.index("--json-schema")
        argv2 = argv[:2] + [QUESTION] + argv[3:j] + argv[j + 2:] + ["--resume", sid, "--thinking-display", "summarized"]
        argv2[argv2.index("--max-turns") + 1] = "1"
        rec["rc2"] = cli(argv2, d, "stream-q.jsonl", cwd)
        m2 = messages(os.path.join(d, "stream-q.jsonl"))
        rec["answer"] = "\n".join(t for m in m2 for t in m[1])
        rec["answer_thinking"] = "\n".join(t for m in m2 for t in m[2])
        rec["answer_tools"] = [n for m in m2 for n in m[0]]
        rec["cost"] = (cost or 0) + (facts(os.path.join(d, "stream-q.jsonl"))[1] or 0)
    srv.shutdown(); srv.server_close()
    k = 1 if MODE == "pointer" else 0
    rec["read_skill"] = len(msgs) > k and "read_skill" in msgs[k][0]
    rec["utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    json.dump(rec, open(os.path.join(d, "meta.json"), "w"), indent=1)
    summary[arm].append(rec)
    print("%03d %-7s msgs=%s cost=%s" % (i, arm, rec["messages"], rec["cost"]), flush=True)
tot = 0.0
for arm, recs in summary.items():
    c = sum(r["cost"] or 0 for r in recs); tot += c
    print("%s: read_skill in message %d: %d/%d, cost %.3f" % (arm, 2 if MODE == "pointer" else 1,
          sum(r["read_skill"] for r in recs), len(recs), c))
print("total cost %.3f" % tot)
json.dump(summary, open(os.path.join(OUT, "summary.json"), "w"), indent=1)
