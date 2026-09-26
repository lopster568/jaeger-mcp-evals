"""What every provider shares: the response record and the retrying POST."""
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

TRIES = 3


class APIError(Exception):
    def __init__(self, message, code=None, body=""):
        super().__init__(message)
        self.code, self.body = code, body


@dataclass
class ProviderResponse:
    # JSON-safe content blocks: recorded in stream.jsonl and reasoning.jsonl, and appended
    # verbatim to `messages` for the next call (thinking blocks and signatures untouched).
    content: list
    stop_reason: Optional[str]
    usage: dict
    request_id: Optional[str]
    model: Optional[str] = None
    message_id: Optional[str] = None
    stop_details: Optional[dict] = None


def post_json(url, body, headers, timeout, backoff_s, id_header):
    """POST body as JSON and return (parsed response, response headers). 429 and 5xx are
    retried, TRIES in all, after retry-after seconds when given, else backoff_s * attempt.
    Any other status raises APIError with the response body."""
    data = json.dumps(body).encode("utf-8")
    for attempt in range(1, TRIES + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read()), resp.headers
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="replace")
            if (e.code == 429 or e.code >= 500) and attempt < TRIES:
                retry_after = e.headers.get("retry-after") if e.headers else None
                time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else backoff_s * attempt)
                continue
            raise APIError("HTTP %d from %s (request-id %s): %s" % (
                e.code, url, e.headers.get(id_header) if e.headers else None, text), e.code, text) from None
