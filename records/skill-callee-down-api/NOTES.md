# skill-callee-down-api: notes

Scenario: recommendationCacheFailure v3. The failing symptom is at a caller; the cause is in the
callee it could not reach, which has no server span for the failed call. The agent has to look at the
callee's own spans to reach the cause.

Skill change: one bullet added to error-root-cause/SKILL.md, and `search_traces` added to its
allowed-tools line. The added bullet:

    - A client span that failed to connect (connection refused, unavailable) and
      has no server-side child is not the origin: the callee was down or
      restarting. Use `search_traces` on the callee for the period before the
      failure and `get_span_details` on its own spans to find why it went down,
      such as spans getting slower or attribute values that stand out.

The full patch is harness/experiments/images/skill-callee-down.patch (one commit on v2.20.0);
harness/experiments/images/skill-callee-down.json records the image built from it.

Result, n=10 per arm, from scores.jsonl:

- baseline (cert-recommendationcache-api, stock 2.20.0 skill): 0 PASS, 9 PARTIAL, 1 FAIL
- change (skill-callee-down-api): 3 PASS, 5 PARTIAL, 2 FAIL
- pre-registered threshold: at least 5 PASS. Measured 3, so EXPERIMENT FAIL (see RESULT.md).

Caveats:

- Both arms ran claude-sonnet-5 through the harness's own agent loop against an OpenAI-compatible
  endpoint. That path sends no reasoning or effort setting. The manifests' effort field reads
  "high" but was not applied, so these are runs without reasoning.
- The fixture overlay was compared by hand, not by manifest (the --overlay-checked text in RESULT.md).
- The hypothesis text in the two experiment files cites earlier batches run with the Claude Code CLI.
  Those batches are not published in this repo. The experiment files are left as run, because their
  sha256 is recorded in the manifests.

Next: the same experiment with reasoning enabled.
