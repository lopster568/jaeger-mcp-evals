#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""tools.py: the MCP tool list an agent sees.

  tools.py capture --out <path> [--timeout <s>]
      Fetches tools/list from the Jaeger MCP endpoint named by fixture.env
      (harness/config.py) and writes the tools array verbatim as JSON to
      --out, then prints the sha256 of the written file (hex, alone on
      stdout). --out is only written once the whole list is in hand.

The MCP transport is mcp_client.MCPClient. bench.py uses list_tools() and
tools_text() for the batch tools.json. Python 3 stdlib only.
"""
import argparse
import hashlib
import json
import sys

import config
from mcp_client import MCPClient, MCPError


def list_tools(url, timeout, served=None):
    """The served tools/list array; MCPError if it cannot be read or is empty.
    A dict passed as served gets the initialize result's "instructions" (None if absent)."""
    c = MCPClient(url, "jaeger-ai-evals-capture-tools", timeout=timeout, client_version="1")
    c.initialize()
    tools = c.list_tools()
    if served is not None:
        served["instructions"] = c.instructions
    if not tools:
        raise MCPError("tools/list returned an empty tools array from %s" % url)
    return tools


def tools_text(tools):
    """The bytes of tools.json: what tools_list_sha256 hashes."""
    return json.dumps(tools, indent=2, ensure_ascii=False) + "\n"


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
