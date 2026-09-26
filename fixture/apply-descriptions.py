#!/usr/bin/env python3
"""apply-descriptions.py <descriptions.json> <server.go>

Rewrites the Description string of named MCP tools inside Jaeger's
cmd/jaeger/internal/extension/jaegerquery/internal/mcptools/server.go, in
place, from a JSON file that maps tool name -> new description text plus a
top-level "status" field.

Refuses (exit 1, no write) if the JSON's "status" field contains the word
DRAFT anywhere in it (case sensitive, matching the convention used in this
repository's experiment files) - DRAFT wording is not baked into a built
binary until that status is changed.

Robust to the Go string-concatenation style server.go uses for multi-line
Description values, e.g.:

    Description: "Get the structural overview of a trace as a flat, " +
        "depth-first span list. " +
        "Does NOT include attributes, events, or links.",

as well as a plain single-string value on one line. Either way, the tool's
Description field is replaced with a single Go string literal holding the
JSON file's text for that tool, keeping the file's own indentation before the
literal untouched.

Only tool names present as keys in the JSON file (other than "status") are
touched; every other tool's Description field is left byte-for-byte alone.
"""
import json
import re
import subprocess
import sys


def die(msg):
    print(f"apply-descriptions: {msg}", file=sys.stderr)
    sys.exit(1)


def go_string_literal(text):
    """Encode text as a single Go double-quoted string literal."""
    out = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _skip_ws(src, j):
    n = len(src)
    while j < n and src[j] in " \t\r\n":
        j += 1
    return j


def _parse_string_literal(src, j):
    """src[j] must be '"'. Returns index just past the closing quote."""
    n = len(src)
    assert src[j] == '"'
    j += 1
    while j < n:
        if src[j] == "\\":
            j += 2
            continue
        if src[j] == '"':
            j += 1
            return j
        j += 1
    raise ValueError("unterminated Go string literal")


def replace_description(src, tool_name, new_text):
    """Return src with tool_name's mcp.Tool{...}.Description value replaced
    by a single Go string literal holding new_text. Raises ValueError if the
    tool's Name or Description field cannot be found/parsed - never guesses."""
    name_pat = re.compile(r'Name:\s*"' + re.escape(tool_name) + r'"\s*,')
    m = name_pat.search(src)
    if not m:
        raise ValueError(f'tool "{tool_name}": no Name: "{tool_name}", literal found')

    desc_key = "Description:"
    desc_idx = src.find(desc_key, m.end())
    if desc_idx == -1:
        raise ValueError(f'tool "{tool_name}": no Description: field found after its Name')
    # Guard against grabbing some other tool's Description if Name and
    # Description are implausibly far apart (this file's structs are small).
    if desc_idx - m.end() > 200:
        raise ValueError(
            f'tool "{tool_name}": Description: field is suspiciously far from '
            f"its Name literal; refusing to guess"
        )

    j = desc_idx + len(desc_key)
    j = _skip_ws(src, j)
    value_start = j
    last_end = None
    while j < len(src) and src[j] == '"':
        j = _parse_string_literal(src, j)
        last_end = j
        k = _skip_ws(src, j)
        if k < len(src) and src[k] == "+":
            k = _skip_ws(src, k + 1)
            j = k
            continue
        j = k
        break

    if last_end is None:
        raise ValueError(f'tool "{tool_name}": Description: value is not a Go string literal')

    comma_pos = _skip_ws(src, last_end)
    if comma_pos >= len(src) or src[comma_pos] != ",":
        raise ValueError(f'tool "{tool_name}": expected "," to terminate the Description field')

    prefix = src[desc_idx:value_start]  # "Description:" plus its original leading whitespace
    replacement = prefix + go_string_literal(new_text)
    return src[:desc_idx] + replacement + src[comma_pos:]


def main():
    if len(sys.argv) != 3:
        print("usage: apply-descriptions.py <descriptions.json> <server.go>", file=sys.stderr)
        sys.exit(2)
    json_path, server_go_path = sys.argv[1], sys.argv[2]

    try:
        with open(json_path, encoding="utf-8") as f:
            desc = json.load(f)
    except Exception as e:
        die(f"could not parse {json_path}: {e}")

    status = desc.get("status", "")
    if "DRAFT" in status:
        die(
            f'{json_path} status is still DRAFT ("{status}"); '
            f"refusing to bake unapproved wording into a build. "
            f"Set status once the wording is chosen."
        )

    tools = {k: v for k, v in desc.items() if k != "status"}
    if not tools:
        die(f"{json_path} has no tool descriptions (only 'status')")
    for name, text in tools.items():
        if not isinstance(text, str) or not text.strip():
            die(f'tool "{name}": description is not a non-empty string')

    try:
        with open(server_go_path, encoding="utf-8") as f:
            src = f.read()
    except Exception as e:
        die(f"could not read {server_go_path}: {e}")

    changed = []
    for name, text in tools.items():
        try:
            src = replace_description(src, name, text)
        except ValueError as e:
            die(str(e))
        changed.append(name)

    with open(server_go_path, "w", encoding="utf-8") as f:
        f.write(src)

    # Collapsing a multi-line concatenation to one literal changes the field's
    # ideal column alignment; gofmt fixes that. Best-effort only - a build
    # still succeeds either way, this just keeps the committed diff clean.
    try:
        subprocess.run(["gofmt", "-w", server_go_path], check=True, capture_output=True)
    except Exception as e:
        print(f"apply-descriptions: WARNING - gofmt -w failed, file left unformatted: {e}", file=sys.stderr)

    print(f"apply-descriptions: rewrote Description for: {', '.join(changed)}", file=sys.stderr)
    print(f"apply-descriptions: wrote {server_go_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
