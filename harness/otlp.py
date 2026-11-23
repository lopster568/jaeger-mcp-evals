# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""otlp.py: one trial's stream.jsonl as OpenInference spans, encoded as an OTLP protobuf request.

Span layout per trial: one AGENT root span over the whole run, one TOOL child span per
tool_use/tool_result pair, one LLM child span per model holding the final answer and the token counts
Phoenix prices (llm.model_name plus prompt, completion and cache details from the result's modelUsage).
A tool span runs from the timestamp of the assistant event holding its tool_use to the timestamp of the
user event holding its tool_result. The root runs from the trial start (started_utc) to the latest of
started_utc + duration_ms and every event's timestamp: the CLI's duration_ms can end before its last event.
The LLM span runs from the last tool_result before the model's last event to the root's end. Streams
without those timestamps (older CLIs) fall back to splitting duration_ms evenly across the child spans
(timestamp.synthetic=true). The root carries the
prompt and the answer, and the result event's `error` as its status message; tool outputs and the answer are cut to
MAX_OUTPUT_BYTES. Ids are sha256-derived from the trace key, so the
same trial always gets the same trace and span ids.

Protobuf, not OTLP/JSON: Phoenix's /v1/traces answers 415 to application/json. The encoder
covers only the fields built here (opentelemetry-proto trace/v1).
"""
import hashlib
import json
import struct
from datetime import datetime

MAX_OUTPUT_BYTES = 8 * 1024


def kv(key, value):
    v = {"intValue": str(value)} if isinstance(value, int) and not isinstance(value, bool) else {"stringValue": str(value)}
    return {"key": key, "value": v}


def truncate(text, limit=MAX_OUTPUT_BYTES):
    b = text.encode("utf-8", errors="replace")
    return text if len(b) <= limit else b[:limit].decode("utf-8", errors="ignore") + "...[truncated]"


def _ns(ts):
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1e9)
    except (AttributeError, ValueError):
        return None


def extract(events):
    """(tool_calls, final_text, result_event) from stream.jsonl events; calls in result order."""
    uses, calls, text, result = {}, [], None, {}
    for e in events:
        content = (e.get("message") or {}).get("content") if e.get("type") in ("assistant", "user") else None
        for c in content if isinstance(content, list) else []:
            if c.get("type") == "tool_use":
                uses[c.get("id")] = dict(c, ns=_ns(e.get("timestamp")))
            elif c.get("type") == "text" and e["type"] == "assistant":
                text = c.get("text") or text
            elif c.get("type") == "tool_result":
                u = uses.get(c.get("tool_use_id"), {})
                out = c.get("content")
                if isinstance(out, list):
                    out = "".join(b.get("text", "") for b in out if isinstance(b, dict))
                calls.append({"name": u.get("name", "unknown"), "input": u.get("input", {}),
                              "output": out if isinstance(out, str) else json.dumps(out), "is_error": bool(c.get("is_error")),
                              "start": u.get("ns"), "end": _ns(e.get("timestamp"))})
        if e.get("type") == "result":
            result = e
    if result.get("structured_output") is not None:
        text = json.dumps(result["structured_output"])
    return calls, text if text is not None else str(result.get("result", "")), result


def trace_spans(key, events, t0_ns, root_attrs, prompt=""):
    """Spans (OTLP/JSON shape) for one trial; trace id = sha256(key)[:32], span i = sha256(key/i)[:16]."""
    calls, final_text, result = extract(events)
    tid = hashlib.sha256(key.encode()).hexdigest()[:32]
    sid = lambda i: hashlib.sha256(("%s/%d" % (key, i)).encode()).hexdigest()[:16]
    u = result.get("usage") or {}
    model = next((a["value"]["stringValue"] for a in root_attrs if a["key"] == "model"), "")
    provider = next((a["value"]["stringValue"] for a in root_attrs if a["key"] == "provider"), None)
    models = result.get("modelUsage") or {model: {"inputTokens": u.get("input_tokens"), "cacheReadInputTokens": u.get("cache_read_input_tokens"),
                                                  "cacheCreationInputTokens": u.get("cache_creation_input_tokens"),
                                                  "outputTokens": u.get("output_tokens")}}
    n = lambda d, k: int(d.get(k) or 0)

    def tokens(m, d):
        read, write, out = n(d, "cacheReadInputTokens"), n(d, "cacheCreationInputTokens"), n(d, "outputTokens")
        prompt_n = n(d, "inputTokens") + read + write
        return ([kv("llm.model_name", m)] if m else []) + ([kv("llm.provider", provider)] if provider else []) + [
            kv("llm.token_count.prompt", prompt_n), kv("llm.token_count.completion", out),
            kv("llm.token_count.total", prompt_n + out), kv("llm.token_count.prompt_details.cache_read", read),
            kv("llm.token_count.prompt_details.cache_write", write)]

    dur = int(float(result.get("duration_ms") or 0) * 1_000_000)
    real = all(c["start"] and c["end"] for c in calls)
    if real:
        stamps = [(e.get("type"), t) for e in events for t in [_ns(e.get("timestamp"))] if t]
        start = min([t0_ns] + [t for _, t in stamps])
        end = max([t0_ns + dur] + [t for _, t in stamps])
        bounds = [(c["start"], c["end"]) for c in calls]
        last = max([t for kind, t in stamps if kind == "assistant"] or [end])  # the model's last event
        answer_start = max([start] + [c["end"] for c in calls if c["end"] < last])
    else:
        start, end, seg = t0_ns, t0_ns + dur, dur // (len(calls) + 1)
        bounds = [(t0_ns + i * seg, t0_ns + (i + 1) * seg) for i in range(len(calls))]
        answer_start = t0_ns + len(calls) * seg

    def span(i, name, s0, s1, attrs, error=False, message=None):
        s = {"traceId": tid, "spanId": sid(i), "name": name, "kind": 1, "startTimeUnixNano": str(s0),
             "endTimeUnixNano": str(s1), "attributes": attrs, "status": {"code": 2 if error else 1}}
        if message:
            s["status"]["message"] = truncate(str(message))
        if i:
            s["parentSpanId"] = sid(0)
        return s

    total = [kv("llm.token_count.prompt", sum(n(d, "inputTokens") + n(d, "cacheReadInputTokens") + n(d, "cacheCreationInputTokens") for d in models.values())),
             kv("llm.token_count.completion", sum(n(d, "outputTokens") for d in models.values()))]
    total.append(kv("llm.token_count.total", int(total[0]["value"]["intValue"]) + int(total[1]["value"]["intValue"])))
    # root has token counts but no llm.model_name, so Phoenix prices only the LLM spans (no double count)
    spans = [span(0, "agent-run", start, end, [kv("openinference.span.kind", "AGENT"), kv("input.value", truncate(prompt)),
                                               kv("output.value", truncate(final_text))] + root_attrs + total
                  + ([] if real else [kv("timestamp.synthetic", "true")]), bool(result.get("is_error")), result.get("error"))]
    for i, c in enumerate(calls, 1):
        spans.append(span(i, c["name"], *bounds[i - 1],
                          [kv("openinference.span.kind", "TOOL"), kv("tool.name", c["name"]),
                           kv("input.value", json.dumps(c["input"])), kv("output.value", truncate(c["output"] or ""))],
                          c["is_error"]))
    for j, (m, d) in enumerate(models.items()):
        spans.append(span(len(calls) + 1 + j, "final-answer" if not j else "llm-" + m, answer_start, end,
                          [kv("openinference.span.kind", "LLM")] + ([kv("output.value", truncate(final_text))] if not j else []) + tokens(m, d)))
    return spans


# ---- protobuf wire format ----------------------------------------------------------

def _varint(n):
    n &= (1 << 64) - 1
    out = bytearray()
    while n > 0x7F:
        out.append(n & 0x7F | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def _len(field, b):
    return _varint(field << 3 | 2) + _varint(len(b)) + b


def _str(field, s):
    return _len(field, s.encode("utf-8"))


def _kv(a):
    v = a["value"]
    any_value = _varint(3 << 3) + _varint(int(v["intValue"])) if "intValue" in v else _str(1, v["stringValue"])
    return _str(1, a["key"]) + _len(2, any_value)


def _span(s):
    b = _len(1, bytes.fromhex(s["traceId"])) + _len(2, bytes.fromhex(s["spanId"]))
    if s.get("parentSpanId"):
        b += _len(4, bytes.fromhex(s["parentSpanId"]))
    b += _str(5, s["name"]) + _varint(6 << 3) + _varint(s["kind"])
    b += _varint(7 << 3 | 1) + struct.pack("<Q", int(s["startTimeUnixNano"]))
    b += _varint(8 << 3 | 1) + struct.pack("<Q", int(s["endTimeUnixNano"]))
    b += b"".join(_len(9, _kv(a)) for a in s["attributes"])
    status = _str(2, s["status"]["message"]) if s["status"].get("message") else b""
    return b + _len(15, status + _varint(3 << 3) + _varint(s["status"]["code"]))


def encode(request):
    """An OTLP/JSON-shaped ExportTraceServiceRequest dict as protobuf bytes."""
    out = b""
    for rs in request["resourceSpans"]:
        body = _len(1, b"".join(_len(1, _kv(a)) for a in rs["resource"]["attributes"]))
        for ss in rs["scopeSpans"]:
            scope = _str(1, ss["scope"]["name"])
            body += _len(2, _len(1, scope) + b"".join(_len(2, _span(s)) for s in ss["spans"]))
        out += _len(1, body)
    return out
