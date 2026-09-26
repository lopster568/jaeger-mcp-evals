#!/usr/bin/env python3
"""Scan what the FIXTURE serves, not what the prompt says.

The prompt leak gate only reads prompts. A review found the OTel demo labels its
own faults inside the telemetry the agent reads (feature_flag events, flagd
spans). This gate samples recent traces from Jaeger (or reads saved trace files) and
fails if any span name, service name, tag key, tag value, event name or event field
contains a leak word, a flag name, or "flagd".

usage: fixture_leak.py --live [--flagd demo.flagd.json] [--lookback-s 300] [--services a,b,c]
       fixture_leak.py --file trace.json [more.json ...] [--flagd demo.flagd.json]
The Jaeger host comes from fixture.env (harness/config.py). --flagd names the
flagd file whose "flags" keys are the flag names to look for; without it the
built-in FLAGS list is used.
exit 0 = clean, 1 = leaks found, 2 = could not read anything
"""
import argparse, json, re, sys, time, urllib.parse, urllib.request

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
    procs = {pid: p.get("serviceName", "") for pid, p in t.get("processes", {}).items()}
    for s in t.get("spans", []):
        svc = procs.get(s.get("processID"), "")
        fields = [("service", svc), ("span name", s.get("operationName", ""))]
        for x in s.get("tags", []):
            if ALLOW.match(str(x.get("key", ""))):
                continue
            fields += [("tag key", x.get("key", "")), ("tag value", str(x.get("value", "")))]
        for l in s.get("logs", []):
            for y in l.get("fields", []):
                fields += [("event field", y.get("key", "")), ("event value", str(y.get("value", "")))]
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


def scan_live(flags, lookback_s=300, services=SERVICES, out=print):
    """Sample recent traces from the fixture's Jaeger and report; exit-code semantics as main."""
    pats, hits, traces = patterns(flags), {}, 0
    now = int(time.time() * 1e6); start = now - lookback_s * 1000000
    base = config.jaeger_base(config.load()) + "/api"
    try:
        svcs = json.load(urllib.request.urlopen(base + "/services", timeout=20)).get("data") or []
    except Exception as e:
        out(f"fixture-leak: cannot reach Jaeger: {e}"); return 2
    for sname in svcs:
        for p in pats:
            if p.search(sname):
                hits[("listed service", sname, sname)] = hits.get(("listed service", sname, sname), 0) + 1
    for svc in services:
        q = urllib.parse.urlencode({"service": svc, "start": start, "end": now, "limit": 30})
        try:
            data = json.load(urllib.request.urlopen(f"{base}/traces?{q}", timeout=40)).get("data") or []
        except Exception:
            continue
        for t in data:
            scan_trace(t, hits, pats); traces += 1
    return report(hits, traces, out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--flagd", help="flagd JSON file; its flags keys replace the built-in list")
    ap.add_argument("--lookback-s", type=int, default=300)
    ap.add_argument("--services", default=",".join(SERVICES))
    ap.add_argument("--file", nargs="*")
    a = ap.parse_args()
    flags = list(json.load(open(a.flagd))["flags"]) if a.flagd else FLAGS
    if a.file:
        pats, hits, traces = patterns(flags), {}, 0
        for f in a.file:
            for t in json.load(open(f)).get("data", []):
                scan_trace(t, hits, pats); traces += 1
        return report(hits, traces)
    if a.live:
        return scan_live(flags, a.lookback_s, a.services.split(","))
    ap.error("give --live or --file")

if __name__ == "__main__":
    sys.exit(main())
