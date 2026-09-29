# skill-callee-down-api: judge result

Written by `harness/judge.py`; each judge run overwrites it, so this is the current answer.

- experiment: skill-callee-down-api
- baseline experiment: cert-recommendationcache-api (different file, see first line below)
- experiment sha256: `edacdba3f424daecedb55dffb06426915d4cba9188392fc621bfe82d50ea0836`
- baseline batch: `batch-20260929T084130Z`
- variant batch: `batch-20260929T124013Z`
- judged (UTC): 2026-09-29T13:14:14Z

```
judge: CROSS-EXPERIMENT comparison: baseline from cert-recommendationcache-api (ed8e27dbd607e8672977eb968117871e00b7b0c7d585ecfc4106619849c4bbcf), variant from skill-callee-down-api (edacdba3f424daecedb55dffb06426915d4cba9188392fc621bfe82d50ea0836); thresholds from the variant's experiment; manifests agree on: scenario, scenario_version, scenario_sha256, prompt name, prompt sha256, client, provider, model_requested, effort, max_turns, max_budget_usd, n_per_arm, system_prompt_sha256
fixture overlay compared by hand, not by manifest: 2026-09-29: both recorded fixture_overlay_sha256 values reproduced from the host's overlay files with only the JAEGERTRACING_IMAGE line set to each arm's image (4619530b stock, fd82a36f variant)
accuracy_pass_min: FAIL (measured=3, required>=5)
EXPERIMENT FAIL
```
