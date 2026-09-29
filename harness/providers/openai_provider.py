"""Provider for any OpenAI-compatible chat completions endpoint (OpenAI, OpenRouter,
Ollama, vLLM or any OpenAI-compatible proxy) over plain HTTP. Standard library only.

Request shape, checked 2026-09-26 against the OpenAI OpenAPI spec
(github.com/openai/openai-openapi, openapi.yaml: CreateChatCompletionRequest,
ChatCompletionTool, ResponseFormatJsonSchema, CreateChatCompletionResponse
finish_reason enum stop|length|tool_calls|content_filter, CompletionUsage):

    POST {OPENAI_BASE_URL}/chat/completions
    Authorization: Bearer <key>, content-type: application/json
    {"model", "max_tokens", "messages": [system, user, assistant(tool_calls), tool(tool_call_id)],
     "tools": [{"type": "function", "function": {name, description, parameters}}], "tool_choice": "auto",
     "response_format": {"type": "json_schema", "json_schema": {"name": "verdict", "schema": ..., "strict": false}}}

The loop speaks Anthropic-shaped content blocks; this module translates both
ways. With an effort set the body carries "reasoning_effort" (probed 2026-09-29
against the endpoint for claude-sonnet-5: the reply message then has
`reasoning_content`, `thinking_blocks` [{type thinking, thinking, signature}] and
usage.completion_tokens_details.reasoning_tokens; an Anthropic-style `thinking`
field returned nothing), and each assistant turn's signed thinking blocks go back
on the assistant message as `thinking_blocks`, unchanged. No temperature or cache
parameters are sent. Retries
and errors come from base.post_json: 429 and 5xx retried, 3 tries in all. A 400 whose body mentions response_format is retried once without it,
and response_format stays off for the rest of the run.
"""
import json

from .base import APIError, ProviderResponse, post_json

STOP = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}


def to_openai(messages, system):
    out = [{"role": "system", "content": system}]
    for m in messages:
        c = m["content"]
        if isinstance(c, str):
            out.append({"role": m["role"], "content": c})
        elif m["role"] == "assistant":
            msg = {"role": "assistant", "content": "".join(b.get("text", "") for b in c if b.get("type") == "text") or None}
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
                     for b in c if b.get("type") == "tool_use"]
            if calls:
                msg["tool_calls"] = calls
            # Only signed blocks can be echoed; an unsigned one came from reasoning_content alone.
            echo = [b for b in c if b.get("type") == "redacted_thinking" or (b.get("type") == "thinking" and b.get("signature"))]
            if echo:
                msg["thinking_blocks"] = echo
            out.append(msg)
        else:
            for b in c:
                if b.get("type") == "tool_result":
                    # A tool message has no error flag, so the loop's is_error travels in the text.
                    text = ("Error: " if b.get("is_error") else "") + b["content"]
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": text})
                elif b.get("type") == "text":
                    out.append({"role": "user", "content": b["text"]})
    return out


def from_openai(message):
    content = list(message.get("thinking_blocks") or [])
    if not content and message.get("reasoning_content"):
        content = [{"type": "thinking", "thinking": message["reasoning_content"]}]
    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        block = {"type": "tool_use", "id": tc.get("id"), "name": fn.get("name")}
        try:
            block["input"] = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            # The MCP server's error for the missing arguments goes back to the model.
            block.update(input={}, unparsed_arguments=fn.get("arguments"))
        content.append(block)
    return content


class OpenAIProvider:
    def __init__(self, model, max_tokens, output_schema, api_key, base_url, effort=None, timeout=1800.0, backoff_s=5.0):
        if not api_key or not base_url:
            raise APIError("OPENAI_API_KEY and OPENAI_BASE_URL must both be set")
        self.api_key, self.url, self.timeout, self.backoff_s = api_key, base_url.rstrip("/") + "/chat/completions", timeout, backoff_s
        self.model, self.max_tokens, self.effort = model, max_tokens, effort
        self._schema = output_schema
        self.response_format_supported = True

    def api_version(self):
        return None

    def output_schema_sent(self):
        return self._schema

    def request_body(self, messages, tools, system):
        body = {"model": self.model, "max_tokens": self.max_tokens, "messages": to_openai(messages, system)}
        if self.effort:
            body["reasoning_effort"] = self.effort
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                               "parameters": t["input_schema"]}} for t in tools]
            body["tool_choice"] = "auto"
        if self._schema is not None and self.response_format_supported:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "verdict", "schema": self._schema, "strict": False}}
        return body

    def create(self, messages, tools, system):
        headers = {"Authorization": "Bearer " + self.api_key, "content-type": "application/json"}
        try:
            msg, h = post_json(self.url, self.request_body(messages, tools, system), headers, self.timeout,
                               self.backoff_s, "x-request-id")
        except APIError as e:
            if not (e.code == 400 and self.response_format_supported and "response_format" in e.body):
                raise
            self.response_format_supported = False
            msg, h = post_json(self.url, self.request_body(messages, tools, system), headers, self.timeout,
                               self.backoff_s, "x-request-id")
        request_id = h.get("x-request-id") or h.get("request-id")
        choice = (msg.get("choices") or [{}])[0]
        content = from_openai(choice.get("message") or {})
        finish = choice.get("finish_reason")
        # Some servers answer finish_reason "stop" alongside tool calls; the calls win.
        stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else STOP.get(finish, finish)
        u = msg.get("usage") or {}
        return ProviderResponse(
            content=content, stop_reason=stop,
            usage={"input_tokens": u.get("prompt_tokens") or 0, "output_tokens": u.get("completion_tokens") or 0,
                   "reasoning_tokens": (u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0},
            request_id=request_id, model=msg.get("model"), message_id=msg.get("id"))
