# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""A deterministic stand-in for the grader's model call (grade.call_cli), so no test runs a model.

Rule: "unclear" if the answer says "unsure"; else "correct" iff the answer contains the key word of
its scenario's mechanism_truth (token, address, trimmed, fraction), else "incorrect".
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import grade  # noqa: E402

# The suite's grader is the claude one with the published model (the import-time default is goose on GOOSE_MODEL); tests of the
# goose grader patch grade.CLIENT, PROVIDER and GRADER_MODEL themselves.
grade.CLIENT, grade.GRADER_MODEL = "claude", "claude-fable-5-1"

KEYS = ("token", "address", "trimmed", "fraction")


def fake_call(truth, text):
    key = next((k for k in KEYS if k in truth.lower()), None)
    if "unsure" in text.lower():
        return {"label": "unclear", "reason": "fake: hedged"}
    ok = key is not None and key in text.lower()
    return {"label": "correct" if ok else "incorrect", "reason": "fake: %r %s" % (key, "found" if ok else "absent")}


def graded():
    """Grader calls go to fake_call while this is active."""
    return mock.patch.object(grade, "call_cli", fake_call)  # CLIENT is claude, so call_goose is not reached
