# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Fixture settings: DEFAULTS, then fixture.env.example (its #KEY=value lines too), then fixture.env, then os.environ win."""
import json
import os
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Fixture constants no contributor sets: the flag file a batch edits and its pristine copy (relative to
# FIXTURE_DEMO_DIR), the Jaeger UI base path, the pre-flight container floor, and tag=commit pairs for jaeger_commit.
DEFAULTS = {"FIXTURE_FLAG_FILE": "src/flagd/demo.flagd.json", "FIXTURE_PRISTINE_FLAG_FILE": "overlay/demo.flagd.json.pristine",
            "JAEGER_BASE_PATH": "/jaeger/ui", "MIN_CONTAINERS": "19",
            "KNOWN_COMMITS": "quay.io/jaegertracing/jaeger:2.23.0=d723f91447f29a345c288f37eb8d1ce7807bc0be"}


ALIASES = {"cli": "claude"}  # run.client cli, written by every record before 2026-11-23


def client_name(v):
    return ALIASES.get(v, v)


def grader_client(cfg, agent):
    """GRADER_CLIENT when set, else the agent's own client when it can grade (claude), else goose."""
    return client_name(cfg.get("GRADER_CLIENT") or "") or ("claude" if agent == "claude" else "goose")


def recorded_client(v):
    """A manifest's client or grader client: absent (written before GRADER_CLIENT) means claude; cli is claude."""
    return client_name(v or "claude")


def load(root=ROOT):
    cfg = dict(DEFAULTS)
    for name in ("fixture.env.example", "fixture.env"):
        path = os.path.join(root, name)
        if os.path.isfile(path):
            for line in open(path, encoding="utf-8"):
                line = line.strip()
                if name == "fixture.env.example" and line[:1] == "#" and line[1:2].isupper():
                    line = line[1:]  # "#KEY=value" in the example is a default, shown commented out
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    v = v.split(" #", 1)[0]  # a " #" starts a comment, as in dotenv
                    cfg[k.strip()] = v.strip().strip("\"'")
    cfg.update({k: v for k, v in os.environ.items() if k in cfg})
    return cfg


def jaeger_base(cfg):
    return "http://%s:%s%s" % (cfg["FIXTURE_HOST"], cfg["JAEGER_UI_PORT"], cfg["JAEGER_BASE_PATH"])


def mcp_url(cfg):
    return jaeger_base(cfg) + "/api/ai/mcp/"


def mcp_endpoint(cfg):
    """Host-free form of mcp_url: port and path only, safe for the committed records."""
    return ":%s%s/api/ai/mcp/" % (cfg["JAEGER_UI_PORT"], cfg["JAEGER_BASE_PATH"])


def host_free(url):
    """http://<host>:<port><path> as :<port><path>, the form safe to record (see mcp_endpoint)."""
    u = urllib.parse.urlsplit(url)
    return ":%s%s" % (u.port or "", u.path)


def repo_relpath(path):
    """Relative to ROOT, for a record file: never the absolute path when the file
    lives outside the repo (that path would carry whatever local directories it
    sits under, home dirs included), so outside the repo it is the basename."""
    rel = os.path.relpath(os.path.abspath(path), ROOT)
    return os.path.basename(path) if rel.startswith("..") else rel


def mcp_config_url(config_path, server):
    """The URL of one server in a client's mcp.json; None when it lists no server (an arm without tools)."""
    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)
    servers = cfg.get("mcpServers") or {}
    if not servers:
        return None
    entry = servers.get(server)
    if not entry or not entry.get("url"):
        raise ValueError("mcp config %s has no mcpServers.%s.url" % (config_path, server))
    if entry.get("type") not in (None, "http", "streamable-http"):
        raise ValueError("mcp server %s has type %r; only streamable HTTP is supported" % (server, entry.get("type")))
    return entry["url"]
