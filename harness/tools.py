#!/usr/bin/env python3
"""tools.py: the MCP tool list an agent sees, and a check of its descriptions.

  tools.py capture --out <path> [--timeout <s>]
      Fetches tools/list from the Jaeger MCP endpoint named by fixture.env
      (harness/config.py) and writes the tools array verbatim as JSON to
      --out, then prints the sha256 of the written file (hex, alone on
      stdout). --out is only written once the whole list is in hand.

The MCP transport is mcp_client.MCPClient. bench.py uses list_tools() and
tools_text() for the batch tools.json, and report() to compare served
descriptions with a pre-registered wording file (exact string equality,
no normalisation: the variant image bakes the file's text in verbatim).
Python 3 stdlib only.
"""
import argparse
import hashlib
import json
import sys

import config
from mcp_client import MCPClient, MCPError


def list_tools(url, timeout):
    """The served tools/list array; MCPError if it cannot be read or is empty."""
    c = MCPClient(url, timeout=timeout, client_name="jaeger-mcp-evals-harness-capture-tools", client_version="1")
    c.initialize()
    tools = c.list_tools()
    if not tools:
        raise MCPError("tools/list returned an empty tools array from %s" % url)
    return tools


def tools_text(tools):
    """The bytes of tools.json: what tools_list_sha256 hashes."""
    return json.dumps(tools, indent=2, ensure_ascii=False) + "\n"


def expected_descriptions(doc):
    """Tool name -> description from a descriptions file, the "status" key dropped."""
    return {k: v for k, v in doc.items() if k != "status"}


def compare_descriptions(tools, expected):
    """Return [(tool_name, expected, served_or_None)] for every expected tool
    whose served description differs or which is not served at all."""
    served = {t.get("name"): t.get("description") for t in tools if isinstance(t, dict)}
    mismatches = []
    for name in sorted(expected):
        got = served.get(name)
        if got != expected[name]:
            mismatches.append((name, expected[name], got))
    return mismatches


def capture(out, timeout):
    try:
        url = config.mcp_url(config.load())
        tools = list_tools(url, timeout)
    except MCPError as e:
        print("tools: FAIL - %s" % e, file=sys.stderr)
        return 1
    text = tools_text(tools)
    with open(out, "wb") as f:
        f.write(text.encode("utf-8"))
    print("tools: %d tools from %s -> %s" % (len(tools), url, out), file=sys.stderr)
    print(hashlib.sha256(text.encode("utf-8")).hexdigest())
    return 0


def report(tools, expected, out=print):
    """Print each mismatch and a PASS or FAIL line; 0 on a full match, 1 otherwise."""
    mismatches = compare_descriptions(tools, expected)
    for name, want, got in mismatches:
        out("tools check: MISMATCH %s" % name)
        out("  pre-registered: %s" % json.dumps(want, ensure_ascii=False))
        out("  served:         %s" % (json.dumps(got, ensure_ascii=False) if got is not None else "<tool not served>"))
    if mismatches:
        out("tools check: FAIL - %d of %d pre-registered descriptions differ from what the fixture serves"
            % (len(mismatches), len(expected)))
        return 1
    out("tools check: PASS - %d pre-registered descriptions match what the fixture serves" % len(expected))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Capture the MCP tools/list array.")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--out", required=True)
    c.add_argument("--timeout", type=float, default=20.0)
    a = p.parse_args(argv)
    return capture(a.out, a.timeout)


if __name__ == "__main__":
    sys.exit(main())
