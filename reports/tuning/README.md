# Retrieval tuning evidence

**合成数据，不代表质量验收。Rerank 默认关闭。**

This directory contains reusable reporting utilities. Historical raw tuning data and delivery records are not distributed. Run new experiments into your own output directory; historical candidates do not set service defaults.

Each real phase requires both switches, loads the approved credential only into
memory, performs a cumulative per-kind call preflight, and counts actual HTTP
attempts before dispatch:

```bash
SEMANTIC_EVAL_REAL=1 AWS_REGION=cn-north-1 AWS_DEFAULT_REGION=cn-north-1 \
PYTHONPATH=src python scripts/run_tuning.py --phase dev --real --output-dir NEW_OUTPUT

SEMANTIC_EVAL_REAL=1 AWS_REGION=cn-north-1 AWS_DEFAULT_REGION=cn-north-1 \
PYTHONPATH=src python scripts/run_tuning.py --phase test --real --output-dir NEW_OUTPUT \
  --freeze-evidence-ref FREEZE_EVIDENCE_REFERENCE
```

These commands spend the separately authorized model budget; `NEW_OUTPUT` is
a reproduction directory, not permission to repeat the current held-out test.
For a future authorized run, separately capture `recommendation.json`,
`split-manifest.json` and `freeze-boundary.json` after dev and before test;
pass that external capture reference through `--freeze-evidence-ref`. The reference
is recorded but not authenticated; an independent reviewer must verify the capture
contents and time. New ledger rows include UTC timestamps, and new recommendations
record `dev_finished_at` plus the split manifest hash. Local timestamps still do
not independently prove when an external capture occurred. Historical artifacts
must not be retrofitted with this metadata or rerun to strengthen past claims.
The refined cosine grid (step 0.05) and `vector_ungated` test control apply only to
future separately authorized evaluations; the historical test evidence is unchanged.
The test claim prevents repeated test invocation and dev is refused after that
claim. `run_tuning.py` returns exit code `20` for `CredentialsExpired` after
recording a stopped state. The fixed markers `ExpiredToken` (including
`ExpiredTokenException`), `RequestExpired` and `TokenRefreshRequired` require
refreshing the configured credential before resuming. Only fixed classifications
are recorded; raw subprocess output is not logged.

Offline smoke requires a fresh, separate directory:

```bash
PYTHONPATH=src python scripts/run_tuning.py --phase smoke --output-dir .local/new-smoke
PYTHONPATH=src python reports/tuning/summarize_tuning.py --phase all --output-dir .local/new-smoke
PYTHONPATH=src python reports/tuning/summarize_usage.py --directory .local/new-smoke
```

Document-only vectors are cached under gitignored `.local/`, keyed by the full
model profile and text. Query vectors stay in memory. Cache hits do not invent
provider usage. Retrieval latency is **reconstructed**: arms replay the same
single live query vector and add one measured query-embedding duration to their
local/rerank duration. This is not an end-to-end latency measurement for each arm.
The knee uses fresh query embedding for each search arm, and arm order is
randomized per paired query.
All reported methods include failure and abstention cases in their denominators.
