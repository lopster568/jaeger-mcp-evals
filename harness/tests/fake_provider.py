"""Scripted provider for tests. Replays turns from a JSON file, no network.

Script shape: {"turns": [{"content": [...blocks...], "stop_reason": "...",
"usage": {...}, "request_id": "...", "stop_details": {...}?, "model": "..."?}]}
"""
import copy
import json

from providers.base import ProviderResponse


class FakeProvider:
    def __init__(self, script_path, model="claude-sonnet-5", output_schema=None):
        with open(script_path, encoding="utf-8") as f:
            self.turns = json.load(f)["turns"]
        self.model = model
        self._schema = output_schema
        self.calls = []

    def api_version(self):
        return None

    def output_schema_sent(self):
        return self._schema

    def create(self, messages, tools, system):
        self.calls.append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools), "system": system})
        i = len(self.calls) - 1
        if i >= len(self.turns):
            raise RuntimeError("fake provider script exhausted at call %d" % (i + 1))
        t = self.turns[i]
        return ProviderResponse(
            content=copy.deepcopy(t.get("content", [])),
            stop_reason=t.get("stop_reason"),
            usage=copy.deepcopy(t.get("usage", {})),
            request_id=t.get("request_id"),
            model=t.get("model", self.model),
            message_id=t.get("id", "msg_fake_%d" % (i + 1)),
            stop_details=copy.deepcopy(t.get("stop_details")),
        )
