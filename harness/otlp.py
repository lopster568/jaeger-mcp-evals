"""otlp.py: one trial's stream.jsonl as OpenInference spans, encoded as an OTLP protobuf request.

Span layout per trial: one AGENT root span over the whole run, one TOOL child span per
tool_use/tool_result pair, one LLM child span holding the final answer. stream.jsonl has no
per-event timestamps, so the root starts at the trial's started_utc and the result event's
duration_ms is split evenly across the child spans (timestamp.synthetic=true). Tool outputs
and the answer are cut to MAX_OUTPUT_BYTES. Ids are sha256-derived from the trace key, so the
same trial always gets the same trace and span ids.

Protobuf, not OTLP/JSON: Phoenix's /v1/traces answers 415 to application/json. The encoder
covers only the fields built here (opentelemetry-proto trace/v1).
"""
import hashlib
import json
import struct

MAX_OUTPUT_BYTES = 8 * 1024


def kv(key, value):
    v = {"intValue": str(value)} if isinstance(value, int) and not isinstance(value, bool) else {"stringValue": str(value)}
    return {"key": key, "value": v}


def truncate(text, limit=MAX_OUTPUT_BYTES):
    b = text.encode("utf-8", errors="replace")
    return text if len(b) <= limit else b[:limit].decode("utf-8", errors="ignore") + "...[truncated]"


def extract(events):
    """(tool_calls, final_text, result_event) from stream.jsonl events; calls in result order."""
    uses, calls, text, result = {}, [], None, {}
    for e in events:
        content = (e.get("message") or {}).get("content") if e.get("type") in ("assistant", "user") else None
        for c in content if isinstance(content, list) else []:
            if c.get("type") == "tool_use":
                uses[c.get("id")] = c
            elif c.get("type") == "text" and e["type"] == "assistant":
                text = c.get("text") or text
            elif c.get("type") == "tool_result":
                u = uses.get(c.get("tool_use_id"), {})
                out = c.get("content")
                if isinstance(out, list):
                    out = "".join(b.get("text", "") for b in out if isinstance(b, dict))
                calls.append({"name": u.get("name", "unknown"), "input": u.get("input", {}),
                              "output": out if isinstance(out, str) else json.dumps(out), "is_error": bool(c.get("is_error"))})
        if e.get("type") == "result":
            result = e
    if result.get("structured_output") is not None:
        text = json.dumps(result["structured_output"])
    return calls, text if text is not None else str(result.get("result", "")), result


def trace_spans(key, events, t0_ns, root_attrs):
    """Spans (OTLP/JSON shape) for one trial; trace id = sha256(key)[:32], span i = sha256(key/i)[:16]."""
    calls, final_text, result = extract(events)
    tid = hashlib.sha256(key.encode()).hexdigest()[:32]
    sid = lambda i: hashlib.sha256(("%s/%d" % (key, i)).encode()).hexdigest()[:16]
    u = result.get("usage") or {}
    prompt = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    tokens = [kv("llm.token_count.prompt", prompt), kv("llm.token_count.completion", int(u.get("output_tokens") or 0)),
              kv("llm.token_count.total", prompt + int(u.get("output_tokens") or 0))]
    dur = int(float(result.get("duration_ms") or 0) * 1_000_000)
    seg = dur // (len(calls) + 1)

    def span(i, name, start, end, attrs, error=False):
        s = {"traceId": tid, "spanId": sid(i), "name": name, "kind": 1, "startTimeUnixNano": str(start),
             "endTimeUnixNano": str(end), "attributes": attrs, "status": {"code": 2 if error else 1}}
        if i:
            s["parentSpanId"] = sid(0)
        return s

    spans = [span(0, "agent-run", t0_ns, t0_ns + dur, [kv("openinference.span.kind", "AGENT")] + root_attrs + tokens
                  + [kv("timestamp.synthetic", "true")], bool(result.get("is_error")))]
    for i, c in enumerate(calls, 1):
        spans.append(span(i, c["name"], t0_ns + (i - 1) * seg, t0_ns + i * seg,
                          [kv("openinference.span.kind", "TOOL"), kv("tool.name", c["name"]),
                           kv("input.value", json.dumps(c["input"])), kv("output.value", truncate(c["output"] or ""))],
                          c["is_error"]))
    n = len(calls) + 1
    spans.append(span(n, "final-answer", t0_ns + len(calls) * seg, t0_ns + dur,
                      [kv("openinference.span.kind", "LLM"), kv("output.value", truncate(final_text))] + tokens))
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
    return b + _len(15, _varint(3 << 3) + _varint(s["status"]["code"]))


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
