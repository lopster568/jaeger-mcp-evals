#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""judge.py <baseline_batch_dir> <variant_batch_dir> [--overlay-checked "<text>"]

Scores two batches of one experiment against each other, against the thresholds
fixed in the experiment file. Each manifest.json carries that file verbatim under
"experiment" (content and sha256, written by bench.py run before any trial ran), so
a threshold can never be moved after seeing the results. Either a RUNS_DIR batch dir
or its copy under records/ works: judge.py reads only manifest.json and scores.jsonl.

Two directories, because an arm is pinned to a Jaeger image and bench.py run runs only
the arms whose image the fixture is running, so baseline and variant come from two runs.
judge.py:

  - refuses (exit 2) if the two manifests carry different experiment sha256 values, unless
    their manifests agree on CROSS_FIELDS (everything but the arm's image); then it judges
    with the variant experiment's thresholds and says so on the first output line. Else it
    names the differing fields. With one experiment sha256 it refuses (exit 2) if the manifests
    differ on SAME_RUN_FIELDS (scenario, its version and hash, prompt, system prompt).
  - refuses (exit 2) if the baseline batch's arm_pin is not the experiment's
    baseline_arm, or the variant batch's arm_pin is not one of its other arms
    (the two directories were probably swapped or mismatched)
  - refuses (exit 2) if the experiment's thresholds block is empty: nothing would be judged
  - for each non-baseline arm, from scores.jsonl:
      - PASS count, using score.py's strict verdict == "PASS"; INVALID rows (sandbox check
        failed) and LEAK rows (the answer names a flag) stay in n, are never a PASS, and
        each arm with any gets a WARNING line
      - a "grounding:" line when any compared row has score.py's evidence_unseen, tool_result_leak_hits or
        export_marker_hits fields: how many PASS rows have one non-empty (report only, no verdict change)
      - fisher_p_max: two-sided Fisher p (bench.fisher_p) of the baseline and arm PASS counts,
        n per arm; PASS when p < the value

Prints one PASS/FAIL line per threshold (per test arm, if there is more
than one), then a final verdict line. A judged pair (exit 0 or 1) also writes those
lines to records/<experiment>/RESULT.md, overwriting any earlier result, with a header
naming the experiment sha256 and both batch ids, and regenerates records/INDEX.md.

Exit codes:
  0 - EXPERIMENT PASS: every threshold passed.
  1 - EXPERIMENT FAIL: at least one threshold failed.
  2 - a manifest is missing, the two experiment files differ, the manifests
      differ on the scenario, prompt or system prompt, the thresholds block is empty,
      or an arm_pin mismatch suggests the wrong batch directories were passed; nothing was judged.
"""
import json
import os
import sys
from datetime import datetime, timezone

import config


def load_rows(batch_dir):
    """score.py summary dicts, each tagged with 'arm', from the batch's scores.jsonl."""
    path = os.path.join(batch_dir, "scores.jsonl")
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def invalid_cause(rows):
    """Why the INVALID rows are INVALID: score.py scores INVALID on a sandbox breach (sandbox_ok
    false) or a fixture change the scenario did not declare (fixture_ok false), either or both."""
    inv = [r for r in rows if r.get("verdict") == "INVALID"]
    why = [k for k, bad in (("sandbox", any(r.get("sandbox_ok") is False for r in inv)),
                            ("fixture", any(r.get("fixture_ok") is False for r in inv))) if bad]
    return " and ".join(why) or "sandbox or fixture"


def arm_metrics(rows, arm, tool_names):
    arm_rows = [r for r in rows if r.get("arm") == arm]
    tool_used = {}
    for tool in tool_names:
        tool_used[tool] = sum(1 for r in arm_rows if tool in (r.get("call_sequence") or []))
    return {
        "n": len(arm_rows),
        "tool_used": tool_used,
        "pass_count": sum(1 for r in arm_rows if r.get("verdict") == "PASS"),
        "invalid": sum(1 for r in arm_rows if r.get("verdict") == "INVALID"),
        "invalid_cause": invalid_cause(arm_rows),
        "leak": sum(1 for r in arm_rows if r.get("verdict") == "LEAK"),
    }


GROUNDING = ("evidence_unseen", "tool_result_leak_hits", "export_marker_hits")


def grounding_line(rows_by_label):
    """One line on PASS rows carrying a grounding flag, or None when no row has the fields (published records).
    rows_by_label: [(label, rows)] for the compared arms."""
    if not any(k in r for _, rows in rows_by_label for r in rows for k in GROUNDING):
        return None
    flagged = {label: [r.get("dir") or "?" for r in rows if r.get("verdict") == "PASS" and any(r.get(k) for k in GROUNDING)]
               for label, rows in rows_by_label}
    n = sum(len(v) for v in flagged.values())
    if not n:
        return "grounding: 0 flagged PASS"
    return ("grounding: %d flagged PASS (baseline %d, variant %d): read them before trusting this result: " % (
        n, len(flagged.get("baseline", [])), len(flagged.get("variant", [])))
        + "; ".join("%s %s" % (label, d) for label, ds in flagged.items() for d in ds))


def _load_manifest(batch_dir):
    manifest_path = os.path.join(batch_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        return None, f"judge: no such file: {manifest_path}"
    with open(manifest_path, encoding="utf-8") as f:
        return json.load(f), None


def _versions(manifest, rows):
    """The client version of each row: its own (observed in the stream), else the manifest's client_version_pre, each
    cut to its first digit-led token ("2.1.285 (Claude Code)", "codex-cli 0.153.2"); None where neither was recorded."""
    tok = lambda s: next((t for t in str(s or "").split() if t[:1].isdigit()), None)
    pre = tok(manifest.get("client_version_pre"))
    return [tok(r.get("client_version")) or pre for r in rows] or [pre]


CROSS_FIELDS = ["scenario", "scenario_version", "scenario_sha256", "prompt name", "prompt sha256",
                "client", "provider", "model_requested", "effort", "max_turns", "max_budget_usd", "n_per_arm",
                "fixture_overlay_sha256", "system_prompt_sha256", "grader"]
SAME_RUN_FIELDS = ["scenario", "scenario_version", "scenario_sha256", "prompt name", "prompt sha256", "system_prompt_sha256", "grader"]


OVERLAY, OVERLAY_NOIMG = "fixture_overlay_sha256", "fixture_overlay_sha256_sans_image"


def _cross_values(m, fields=CROSS_FIELDS):
    """The comparison-defining fields of one manifest (everything but the arm's image)."""
    exp = m["experiment"]["content"]
    arm = (m.get("arm_pin") or {}).get("arm") or next(iter(exp["arms"]))
    prompt = exp["arms"][arm]["prompt"]
    v = {k: m.get(k) for k in fields if k not in ("prompt name", "prompt sha256", "grader")}
    if "client" in v:
        v["client"] = config.client_name(v["client"])  # cli, written before 2026-11-23, is claude
    g = m.get("grader") or {}  # manifests from before the goose grader have no client: they were claude (once cli)
    if "grader" in fields:
        v["grader"] = (config.recorded_client(g.get("client")), g.get("provider"), g.get("model"), g.get("prompt_sha256"))
    v["prompt name"] = prompt
    v["prompt sha256"] = (m.get("file_hashes_sha256") or {}).get("prompts/%s.txt" % prompt)
    return v


def _cross_fields(a, b):
    """CROSS_FIELDS with the overlay entry swapped for the image-free hash when both manifests have
    it, else without the overlay entry (the caller must then have checked it by hand)."""
    if a.get(OVERLAY_NOIMG) and b.get(OVERLAY_NOIMG):
        return [OVERLAY_NOIMG if k == OVERLAY else k for k in CROSS_FIELDS]
    return [k for k in CROSS_FIELDS if k != OVERLAY]


def _cross_diffs(a, b, fields=CROSS_FIELDS):
    """[(field, baseline value, variant value)] for every entry of fields that differs."""
    va, vb = _cross_values(a, fields), _cross_values(b, fields)
    # a manifest that recorded no scenario hash proves nothing about agreement
    return [(k, va[k], vb[k]) for k in fields if va[k] != vb[k] or (k == "scenario_sha256" and va[k] is None)]


def judge(baseline_batch_dir, variant_batch_dir, overlay_checked=None):
    """Returns (lines, exit_code). lines is the full list of printed lines,
    in order, including the final verdict (or the refusal line)."""
    baseline_manifest, err = _load_manifest(baseline_batch_dir)
    if err:
        return ([err], 2)
    variant_manifest, err = _load_manifest(variant_batch_dir)
    if err:
        return ([err], 2)

    baseline_sha = baseline_manifest["experiment"]["sha256"]
    variant_sha = variant_manifest["experiment"]["sha256"]
    cross = baseline_sha != variant_sha
    if cross:
        fields = _cross_fields(baseline_manifest, variant_manifest)
        if OVERLAY_NOIMG not in fields and not overlay_checked:
            return ([f"judge: different experiment files and a manifest lacks {OVERLAY_NOIMG}; the fixture "
                     "overlay cannot be compared by manifest (pass --overlay-checked \"<what you checked>\"); refusing to compare"], 2)
        diffs = _cross_diffs(baseline_manifest, variant_manifest, fields)
        if diffs:
            return ([
                "judge: baseline and variant batches ran different experiment files "
                f"(sha256 {baseline_sha!r} != {variant_sha!r}) and their manifests differ on: "
                + "; ".join(f"{k} ({a!r} vs {b!r})" for k, a, b in diffs) + "; refusing to compare"
            ], 2)
    else:  # arms legitimately differ in image and overlay, but not in what was asked
        diffs = _cross_diffs(baseline_manifest, variant_manifest, SAME_RUN_FIELDS)
        if diffs:
            return ([
                "judge: baseline and variant batches ran one experiment file but their manifests differ on: "
                + "; ".join(f"{k} ({a!r} vs {b!r})" for k, a, b in diffs) + "; refusing to compare"
            ], 2)

    exp = variant_manifest["experiment"]["content"]  # identical to the baseline's unless cross
    thresholds = exp["thresholds"]  # cross: the variant's experiment pre-registered them
    if cross:
        baseline_arm = baseline_manifest["experiment"]["content"]["baseline_arm"]
        vpin = (variant_manifest.get("arm_pin") or {}).get("arm")
        test_arms = [vpin] if vpin else [a for a in exp["arms"] if a != exp["baseline_arm"]]
        lines0 = [f"judge: CROSS-EXPERIMENT comparison: baseline from {baseline_manifest['experiment']['name']} "
                  f"({baseline_sha}), variant from {variant_manifest['experiment']['name']} ({variant_sha}); "
                  "thresholds from the variant's experiment; manifests agree on: " + ", ".join(fields)]
        if OVERLAY_NOIMG not in fields:
            lines0.append("fixture overlay compared by hand, not by manifest: " + overlay_checked)
    else:
        baseline_arm = exp["baseline_arm"]
        test_arms = [a for a in exp["arms"] if a != baseline_arm]
        lines0 = []
    if not baseline_arm or not test_arms:
        return (["judge: the experiment needs a baseline_arm and at least one other arm"], 2)

    # Catch a swapped or mismatched pair of directories before trusting any row in them.
    baseline_pin = (baseline_manifest.get("arm_pin") or {}).get("arm")
    if baseline_pin and baseline_pin != baseline_arm:
        return ([
            f"judge: baseline_batch_dir ({baseline_batch_dir}) is pinned to arm "
            f"'{baseline_pin}', expected the experiment's baseline_arm '{baseline_arm}' - "
            "wrong batch directory?"
        ], 2)
    variant_pin = (variant_manifest.get("arm_pin") or {}).get("arm")
    if variant_pin and variant_pin not in test_arms:
        return ([
            f"judge: variant_batch_dir ({variant_batch_dir}) is pinned to arm "
            f"'{variant_pin}', which is not a non-baseline arm of the experiment "
            f"({test_arms}) - wrong batch directory?"
        ], 2)

    if not thresholds:
        return (["judge: the experiment has no thresholds; nothing was judged"], 2)

    baseline_rows = load_rows(baseline_batch_dir)
    variant_rows = load_rows(variant_batch_dir)
    seen = {"baseline": _versions(baseline_manifest, [r for r in baseline_rows if r.get("arm") == baseline_arm]),
            "variant": _versions(variant_manifest, [r for r in variant_rows if r.get("arm") in test_arms])}
    if len({v for vs in seen.values() for v in vs if v}) > 1:
        return (["judge: refusing to compare: the client version differs (" + "; ".join(
            "%s arm ran on %s" % (k, " and ".join(sorted({v for v in vs if v}))) for k, vs in seen.items() if any(vs))
            + "); keep each pair on one client"], 2)
    if any(v is None for vs in seen.values() for v in vs):
        print("judge: client version not recorded", file=sys.stderr)  # not in lines: RESULT.md stays as it was
    baseline_metrics = arm_metrics(baseline_rows, baseline_arm, ())
    if baseline_metrics["n"] == 0:
        return ([f"judge: no rows for baseline arm '{baseline_arm}' found in {baseline_batch_dir}"], 2)

    def count(label, arm, mm):
        return f"{label} {arm}: {mm['pass_count']}/{mm['n']} PASS" + (f", {mm['invalid']} INVALID" if mm["invalid"] else "")

    lines, results = list(lines0), []
    grounding = grounding_line([("baseline", [r for r in baseline_rows if r.get("arm") == baseline_arm]),
                                ("variant", [r for r in variant_rows if r.get("arm") in test_arms])])
    lines.append(count("baseline arm", baseline_arm, baseline_metrics))
    for arm in test_arms:
        m = arm_metrics(variant_rows, arm, ())
        if m["n"] == 0:
            lines.append(f"judge: WARNING - no rows for arm '{arm}' found in {variant_batch_dir}")
        prefix = f"{arm}: " if len(test_arms) > 1 else ""
        lines.append(count("arm", arm, m))
        for label, mm in (("baseline", baseline_metrics), ("variant", m)):
            if mm["invalid"]:
                lines.append(f"judge: WARNING - {prefix}{mm['invalid']} {label} row(s) INVALID ({mm['invalid_cause']} check failed), "
                             "counted in n and never as a PASS")
            if mm["leak"]:
                lines.append(f"judge: WARNING - {prefix}{mm['leak']} {label} row(s) LEAK (the answer names a flag), "
                             "counted in n and never as a PASS")

        def check(name, ok, measured, op, required):
            results.append(ok)
            lines.append(f"{prefix}{name}: {'PASS' if ok else 'FAIL'} (measured={measured}, required{op}{required})")

        if "accuracy_pass_min" in thresholds:
            min_pass = thresholds["accuracy_pass_min"]
            check("accuracy_pass_min", m["pass_count"] >= min_pass, m["pass_count"], ">=", min_pass)
        if "fisher_p_max" in thresholds:
            import bench  # here, not at the top: bench imports judge
            n, pmax = baseline_metrics["n"], thresholds["fisher_p_max"]
            if m["n"] != n:  # the table below needs equal arms
                check("fisher_p_max", False, "N/A (n %d vs %d)" % (n, m["n"]), "<", pmax)
            else:
                p = bench.fisher_p(baseline_metrics["pass_count"], m["pass_count"], n)
                check("fisher_p_max", p < pmax, round(p, 4), "<", pmax)
                base_k, var_k = baseline_metrics["pass_count"], m["pass_count"]  # output only: the rule stays two-sided
                verdict = ("better" if var_k > base_k else "worse") if p < pmax else "no significant difference at n=%d" % n
                lines.append(f"{prefix}direction: variant {var_k}/{n} vs baseline {base_k}/{n}: {verdict}")

    if grounding:
        lines.append(grounding)
    all_pass = all(results)
    lines.append("EXPERIMENT PASS" if all_pass else "EXPERIMENT FAIL")
    return (lines, 0 if all_pass else 1)


def write_result(lines, baseline_batch_dir, variant_batch_dir, records=None):
    """records/<experiment>/RESULT.md: the current answer for the experiment, then INDEX.md."""
    import bench  # here, not at the top: bench imports judge
    records = records or bench.RECORDS
    base, var = _load_manifest(baseline_batch_dir)[0], _load_manifest(variant_batch_dir)[0]
    exp = var["experiment"]

    def batch_id(m, d):
        return m.get("batch_id") or os.path.basename(os.path.normpath(d))

    dest = os.path.join(records, exp["name"])
    os.makedirs(dest, exist_ok=True)
    notes = ["Note: the oracle failed in the %s batch; trials ran on the signal wait alone." % label
             for label, m in (("baseline", base), ("variant", var)) if (m.get("preflight") or {}).get("oracle") == "FAIL"]
    jaeger = ["- %s Jaeger: %s (commit %s)" % (label, m.get("jaeger_image") or "not recorded", m.get("jaeger_commit") or "not recorded")
              for label, m in (("baseline", base), ("variant", var))]
    path = os.path.join(dest, "RESULT.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join([
            "# %s: judge result" % exp["name"], "",
            "Written by `harness/judge.py`; each judge run overwrites it, so this is the current answer.", "",
            "- experiment: %s" % exp["name"],
            "- baseline experiment: %s" % (base["experiment"]["name"] + " (different file, see first line below)"
                                          if base["experiment"]["sha256"] != exp["sha256"] else "same file"),
            "- experiment sha256: `%s`" % exp["sha256"],
            "- baseline batch: `%s`" % batch_id(base, baseline_batch_dir),
            "- variant batch: `%s`" % batch_id(var, variant_batch_dir)] + jaeger + [
            "- judged (UTC): %s" % datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "", "```"] + lines + ["```", ""] + (notes + [""] if notes else [])))
    bench.write_index(records, out=lambda *a: None)
    return path


def main():
    args, overlay_checked = sys.argv[1:], None
    if "--overlay-checked" in args:
        i = args.index("--overlay-checked")
        overlay_checked, args = " ".join(args[i + 1:i + 2]), args[:i] + args[i + 2:]
    if len(args) != 2 or overlay_checked == "":
        print("usage: judge.py <baseline_batch_dir> <variant_batch_dir> [--overlay-checked \"<text>\"]", file=sys.stderr)
        sys.exit(2)
    sys.argv[1:] = args
    lines, code = judge(sys.argv[1], sys.argv[2], overlay_checked)
    for line in lines:
        print(line)
    if code in (0, 1):
        path = write_result(lines, sys.argv[1], sys.argv[2])
        print("judge: wrote %s and records/INDEX.md" % os.path.relpath(path), file=sys.stderr)
    sys.exit(code)


if __name__ == "__main__":
    main()
