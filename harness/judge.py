#!/usr/bin/env python3
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
    names the differing fields.
  - refuses (exit 2) if the baseline batch's arm_pin is not the experiment's
    baseline_arm, or the variant batch's arm_pin is not one of its other arms
    (the two directories were probably swapped or mismatched)
  - for each non-baseline arm, from scores.jsonl:
      - per-tool run counts: how many of that arm's runs used a given tool
        at least once, for every tool named under tool_used_min / tool_used_max
      - median tool_output_chars (the drop percentage in
        tool_output_chars_median_drop_pct_min is measured against the
        baseline arm's median, from the baseline batch)
      - PASS count, using score.py's strict verdict == "PASS"; INVALID rows (sandbox check
        failed) and LEAK rows (the answer names a flag) stay in n, are never a PASS, and
        each arm with any gets a WARNING line

Prints one PASS/FAIL line per threshold (per test arm, if there is more
than one), then a final verdict line. A judged pair (exit 0 or 1) also writes those
lines to records/<experiment>/RESULT.md, overwriting any earlier result, with a header
naming the experiment sha256 and both batch ids, and regenerates records/INDEX.md.

Exit codes:
  0 - EXPERIMENT PASS: every threshold passed.
  1 - EXPERIMENT FAIL: at least one threshold failed.
  2 - a manifest is missing, the two experiment files differ, or an arm_pin
      mismatch suggests the wrong batch directories were passed; nothing was judged.
"""
import json
import os
import statistics
import sys
from datetime import datetime, timezone


def load_rows(batch_dir):
    """score.py summary dicts, each tagged with 'arm', from the batch's scores.jsonl."""
    path = os.path.join(batch_dir, "scores.jsonl")
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def median(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return statistics.median(xs) if xs else None


def arm_metrics(rows, arm, tool_names):
    arm_rows = [r for r in rows if r.get("arm") == arm]
    tool_used = {}
    for tool in tool_names:
        tool_used[tool] = sum(1 for r in arm_rows if tool in (r.get("call_sequence") or []))
    return {
        "n": len(arm_rows),
        "tool_used": tool_used,
        "chars_median": median([r.get("tool_output_chars") for r in arm_rows]),
        "pass_count": sum(1 for r in arm_rows if r.get("verdict") == "PASS"),
        "invalid": sum(1 for r in arm_rows if r.get("verdict") == "INVALID"),
        "leak": sum(1 for r in arm_rows if r.get("verdict") == "LEAK"),
    }


def _load_manifest(batch_dir):
    manifest_path = os.path.join(batch_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        return None, f"judge: no such file: {manifest_path}"
    with open(manifest_path, encoding="utf-8") as f:
        return json.load(f), None


CROSS_FIELDS = ["scenario", "scenario_version", "scenario_sha256", "prompt name", "prompt sha256",
                "client", "provider", "model_requested", "effort", "max_turns", "max_budget_usd", "n_per_arm",
                "fixture_overlay_sha256", "system_prompt_sha256"]


OVERLAY, OVERLAY_NOIMG = "fixture_overlay_sha256", "fixture_overlay_sha256_sans_image"


def _cross_values(m, fields=CROSS_FIELDS):
    """The comparison-defining fields of one manifest (everything but the arm's image)."""
    exp = m["experiment"]["content"]
    arm = (m.get("arm_pin") or {}).get("arm") or next(iter(exp["arms"]))
    prompt = exp["arms"][arm]["prompt"]
    v = {k: m.get(k) for k in fields if k not in ("prompt name", "prompt sha256")}
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

    exp = variant_manifest["experiment"]["content"]  # identical to the baseline's unless cross
    thresholds = exp["thresholds"]  # cross: the variant's experiment pre-registered them
    if cross:
        baseline_arm = baseline_manifest["experiment"]["content"]["baseline_arm"]
        vpin = (variant_manifest.get("arm_pin") or {}).get("arm")
        test_arms = [vpin] if vpin else list(exp["arms"])
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

    tool_min = thresholds.get("tool_used_min") or {}
    tool_max = thresholds.get("tool_used_max") or {}
    tool_names = set(tool_min) | set(tool_max)

    baseline_rows = load_rows(baseline_batch_dir)
    variant_rows = load_rows(variant_batch_dir)
    baseline_metrics = arm_metrics(baseline_rows, baseline_arm, tool_names)
    if baseline_metrics["n"] == 0:
        return ([f"judge: no rows for baseline arm '{baseline_arm}' found in {baseline_batch_dir}"], 2)

    lines, results = list(lines0), []
    for arm in test_arms:
        m = arm_metrics(variant_rows, arm, tool_names)
        if m["n"] == 0:
            lines.append(f"judge: WARNING - no rows for arm '{arm}' found in {variant_batch_dir}")
        prefix = f"{arm}: " if len(test_arms) > 1 else ""
        for label, mm in (("baseline", baseline_metrics), ("variant", m)):
            if mm["invalid"]:
                lines.append(f"judge: WARNING - {prefix}{mm['invalid']} {label} row(s) INVALID (sandbox check failed), "
                             "counted in n and never as a PASS")
            if mm["leak"]:
                lines.append(f"judge: WARNING - {prefix}{mm['leak']} {label} row(s) LEAK (the answer names a flag), "
                             "counted in n and never as a PASS")

        def check(name, ok, measured, op, required):
            results.append(ok)
            lines.append(f"{prefix}{name}: {'PASS' if ok else 'FAIL'} (measured={measured}, required{op}{required})")

        for tool, min_n in tool_min.items():
            measured = m["tool_used"].get(tool, 0)
            check(f"tool_used_min[{tool}]", measured >= min_n, measured, ">=", min_n)
        for tool, max_n in tool_max.items():
            measured = m["tool_used"].get(tool, 0)
            check(f"tool_used_max[{tool}]", measured <= max_n, measured, "<=", max_n)
        if "tool_output_chars_median_drop_pct_min" in thresholds:
            min_drop = thresholds["tool_output_chars_median_drop_pct_min"]
            base_med, var_med = baseline_metrics["chars_median"], m["chars_median"]
            drop_pct = round((base_med - var_med) / base_med * 100, 1) if base_med and var_med is not None else None
            check("tool_output_chars_median_drop_pct_min", drop_pct is not None and drop_pct >= min_drop,
                  drop_pct if drop_pct is not None else
                  "N/A (no baseline data)" if not base_med else "N/A (no variant data)", ">=", min_drop)
        if "accuracy_pass_min" in thresholds:
            min_pass = thresholds["accuracy_pass_min"]
            check("accuracy_pass_min", m["pass_count"] >= min_pass, m["pass_count"], ">=", min_pass)

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
            "- variant batch: `%s`" % batch_id(var, variant_batch_dir),
            "- judged (UTC): %s" % datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "", "```"] + lines + ["```", ""]))
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
