"""Provider for the Anthropic Messages API over plain HTTP. Standard library only.

Request shape, checked 2026-09-26 against the claude-api skill reference bundled
with Claude Code 2.1.283 (curl/examples.md "Tool Use", "Extended Thinking",
"Required Headers"; shared/tool-use-concepts.md "Structured Outputs";
shared/error-codes.md):

    POST https://api.anthropic.com/v1/messages
    x-api-key, anthropic-version: 2023-06-01, content-type: application/json
    {"model", "max_tokens", "system", "messages", "tools", "tool_choice": {"type": "auto"},
     "thinking": {"type": "adaptive", "display": "summarized"},
     "output_config": {"effort": ..., "format": {"type": "json_schema", "schema": ...}},
     "cache_control": {"type": "ephemeral"}}

Structured outputs and effort are GA, so no anthropic-beta header is sent.
Non-streaming: one JSON response per turn; its content blocks go back into the
next request unchanged (thinking text and signatures included). 429 and 5xx
(529 overloaded included) are retried, 3 tries in all; any other status raises
with the response body, which the loop writes to stderr.txt.
"""
from .base import APIError, ProviderResponse, post_json

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
THINKING = {"type": "adaptive", "display": "summarized"}


class AnthropicProvider:
    def __init__(self, model, max_tokens, effort, output_schema, api_key, url=API_URL, timeout=1800.0, backoff_s=5.0):
        if not api_key:
            raise APIError("ANTHROPIC_API_KEY is empty")
        self.api_key, self.url, self.timeout, self.backoff_s = api_key, url, timeout, backoff_s
        self.model, self.max_tokens, self.effort = model, max_tokens, effort
        self._schema = output_schema

    def api_version(self):
        return API_VERSION

    def output_schema_sent(self):
        return self._schema

    def request_body(self, messages, tools, system):
        output_config = {"effort": self.effort}
        if self._schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": self._schema}
        body = {"model": self.model, "max_tokens": self.max_tokens, "system": system, "messages": messages,
                "thinking": THINKING, "output_config": output_config, "cache_control": {"type": "ephemeral"}}
        if tools:  # the notools arm offers no tools, so it sends no tool_choice either
            body.update(tools=tools, tool_choice={"type": "auto"})
        return body

    def create(self, messages, tools, system):
        headers = {"x-api-key": self.api_key, "anthropic-version": API_VERSION, "content-type": "application/json"}
        msg, h = post_json(self.url, self.request_body(messages, tools, system), headers, self.timeout, self.backoff_s,
                           "request-id")
        return ProviderResponse(
            content=msg.get("content") or [], stop_reason=msg.get("stop_reason"), usage=msg.get("usage") or {},
            request_id=h.get("request-id"), model=msg.get("model"), message_id=msg.get("id"),
            stop_details=msg.get("stop_details"))
