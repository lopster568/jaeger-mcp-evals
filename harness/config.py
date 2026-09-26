"""Fixture settings: fixture.env.example, then fixture.env, then os.environ win."""
import os
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(root=ROOT):
    cfg = {}
    for name in ("fixture.env.example", "fixture.env"):
        path = os.path.join(root, name)
        if os.path.isfile(path):
            for line in open(path, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
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
