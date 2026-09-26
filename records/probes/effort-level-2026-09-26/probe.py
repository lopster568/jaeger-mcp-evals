"""Effort-level probe: which named effort level matches "no --effort" (turn-1 read_skill 0/5 in the effort-cli probe)?
Same argv as the desc-change-sep28 baseline trials (bench.py claude_argv), --max-turns 2, no fault flipped."""
import json, os, random, subprocess, sys, tempfile, time
H = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "harness")
sys.path.insert(0, H)
import config
OUT = os.path.dirname(os.path.abspath(__file__))
CLIS = {"2.1.283": "claude"}
EFFORTS = {"high": ["--effort", "high"], "medium": ["--effort", "medium"]}
N = int(sys.argv[1]) if len(sys.argv) > 1 else 5
cfg = config.load()
text = lambda p: open(os.path.join(H, p)).read().rstrip("\n")
mcp = json.dumps({"mcpServers": {"jaeger": {"type": "http", "url": config.mcp_url(cfg)}}}, separators=(",", ":"))
env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL")}
cells = [(c, e, i) for c in CLIS for e in EFFORTS for i in range(N)]
random.Random(20260926).shuffle(cells)
rows = []
for k, (cli, eff, i) in enumerate(cells):
    name = "%02d-%s-%s-%d" % (k, cli, eff, i)
    d = os.path.join(OUT, name); os.makedirs(d)
    with tempfile.TemporaryDirectory() as work:
        open(os.path.join(work, "mcp.json"), "w").write(mcp)
        argv = [CLIS[cli], "-p", text("prompts/noskill.txt"), "--output-format", "stream-json", "--verbose",
                "--mcp-config", os.path.join(work, "mcp.json"), "--strict-mcp-config", "--setting-sources", "",
                "--restricted", "--tools", "", "--allowedTools", "mcp__jaeger__*", "--system-prompt", text("system-prompt.txt"),
                "--json-schema", open(os.path.join(H, "verdict-schema.json")).read(), "--model", "sonnet"] + EFFORTS[eff] + [
                "--max-turns", "2", "--max-budget-usd", "1"]
        t0 = time.time()
        with open(os.path.join(d, "stream.jsonl"), "w") as so, open(os.path.join(d, "stderr.txt"), "w") as se:
            rc = subprocess.run(argv, cwd=work, env=env, stdin=subprocess.DEVNULL, stdout=so, stderr=se, timeout=600).returncode
    ev = [json.loads(l) for l in open(os.path.join(d, "stream.jsonl")) if l.strip()]
    init = next((e for e in ev if e.get("type") == "system" and e.get("subtype") == "init"), {})
    first_id, turn1, calls = None, [], []
    for e in ev:
        m = e.get("message") or {}
        for c in m.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                n = c["name"].replace("mcp__jaeger__", ""); calls.append(n)
                first_id = first_id or m.get("id")
                if m.get("id") == first_id: turn1.append(n)
    res = next((e for e in reversed(ev) if e.get("type") == "result"), {})
    row = {"run": name, "cli_requested": cli, "cli_reported": init.get("claude_code_version"), "effort": eff,
           "model": init.get("model"), "exit": rc, "turn1_calls": turn1, "read_skill_turn1": "read_skill" in turn1,
           "calls": calls, "output_tokens": (res.get("usage") or {}).get("output_tokens"), "cost_usd": res.get("total_cost_usd"),
           "wall_s": round(time.time() - t0, 1)}
    rows.append(row); print(json.dumps(row), flush=True)
    open(os.path.join(OUT, "results.jsonl"), "a").write(json.dumps(row) + "\n")
print("\nSUMMARY  cli x effort -> read_skill in turn 1")
for cli in CLIS:
    for eff in EFFORTS:
        rs = [r for r in rows if r["cli_requested"] == cli and r["effort"] == eff]
        print("  %s  effort=%-5s  %d/%d  median_out=%s  cost=%.2f" % (cli, eff, sum(r["read_skill_turn1"] for r in rs), len(rs),
              sorted(r["output_tokens"] or 0 for r in rs)[len(rs)//2], sum(r["cost_usd"] or 0 for r in rs)))
print("total cost %.2f USD" % sum(r["cost_usd"] or 0 for r in rows))
