#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Scan what the FIXTURE serves, not what the prompt says.

The prompt leak gate only reads prompts. A review found the OTel demo labels its
own faults inside the telemetry the agent reads (feature_flag events, flagd
spans). This gate samples recent traces from Jaeger and fails if any span name,
service name, tag key, tag value, event name or event field contains a leak word,
a flag name, or "flagd".

usage: fixture_leak.py --live [--lookback-s 300]
The Jaeger host comes from fixture.env (harness/config.py); the flag names are the
built-in FLAGS list (bench.py passes the flagd file's own).
exit 0 = clean, 1 = leaks found, 2 = could not read anything
"""
import argparse, json, re, sys, time, urllib.request

import config

# Fallback only: the flagd file's own "flags" keys are the authoritative list.
FLAGS = ["paymentFailure", "paymentUnreachable", "productCatalogFailure", "cartFailure",
         "adFailure", "recommendationCacheFailure", "adHighCpu", "adManualGc",
         "imageSlowLoad", "intlShippingSlowdown", "kafkaQueueProblems", "emailMemoryLeak",
         "failedReadinessProbe", "loadGeneratorTraffic", "loadGeneratorVUs"]


def patterns(flags):
    return [re.compile(r"feature[_ .-]?flag", re.I), re.compile(r"flagd", re.I)] + \
           [re.compile(re.escape(f), re.I) for f in flags]


# Envoy's response_flags tag is unrelated to feature flags.
ALLOW = re.compile(r"^response_flags$", re.I)


def scan_trace(t, hits, pats):
    """t is bench.traces()'s v3 form (OTLP spans)."""
    val = lambda v: str(next(iter(v.values()), "")) if isinstance(v, dict) else str(v)  # OTLP {"stringValue": ..}
    for s in t.get("spans", []):
        svc = s.get("serviceName", "")
        fields = [("service", svc), ("span name", s.get("name", ""))]
        for x in s.get("attributes", []):
            if ALLOW.match(str(x.get("key", ""))):
                continue
            fields += [("tag key", x.get("key", "")), ("tag value", val(x.get("value", "")))]
        for e in s.get("events", []):
            fields.append(("event name", e.get("name", "")))
            for y in e.get("attributes", []):
                fields += [("event field", y.get("key", "")), ("event value", val(y.get("value", "")))]
        for kind, text in fields:
            for p in pats:
                if p.search(text):
                    hits.setdefault((kind, svc, text[:90]), 0)
                    hits[(kind, svc, text[:90])] += 1
                    break


SERVICES = ["checkout", "payment", "product-catalog", "cart", "ad", "recommendation", "frontend"]


def report(hits, traces, out=print):
    if traces == 0:
        out("fixture-leak: no traces read")
        return 2
    if hits:
        out(f"fixture-leak: FAIL - {len(hits)} distinct leaks in {traces} traces")
        for (kind, svc, text), n in sorted(hits.items(), key=lambda kv: -kv[1])[:20]:
            out(f"  {n:4d} x [{kind}] {svc}: {text}")
        return 1
    out(f"fixture-leak: PASS - {traces} traces scanned, nothing names a flag")
    return 0


def scan_live(flags, lookback_s=300, out=print):
    """Sample recent traces from the fixture's Jaeger and report; exit-code semantics as main."""
    import bench  # here, not at the top: bench imports this module
    pats, hits, traces = patterns(flags), {}, 0
    now = int(time.time() * 1e6); start = now - lookback_s * 1000000
    cfg = config.load()
    try:
        svcs = json.load(urllib.request.urlopen(config.jaeger_base(cfg) + "/api/v3/services", timeout=20)).get("services") or []
    except Exception as e:
        out(f"fixture-leak: cannot reach Jaeger: {e}"); return 2
    for sname in svcs:
        for p in pats:
            if p.search(sname):
                hits[("listed service", sname, sname)] = hits.get(("listed service", sname, sname), 0) + 1
    for svc in SERVICES:
        try:
            data = bench.traces(cfg, svc, start, now, 30, 40)
        except Exception:
            continue
        for t in data:
            scan_trace(t, hits, pats); traces += 1
    return report(hits, traces, out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--lookback-s", type=int, default=300)
    a = ap.parse_args()
    if a.live:
        return scan_live(FLAGS, a.lookback_s)
    ap.error("give --live")

if __name__ == "__main__":
    sys.exit(main())
