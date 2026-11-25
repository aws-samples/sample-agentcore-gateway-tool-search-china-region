# Retrieval tuning evidence

**合成数据，不代表质量验收。Rerank 默认关闭。**

This directory contains reusable reporting utilities. Historical raw tuning data and delivery records are not distributed. Run new experiments into your own output directory; historical candidates do not set service defaults.

Each real phase requires both switches, loads the approved credential only into
memory, performs a cumulative per-kind call preflight, and counts actual HTTP
attempts before dispatch. Model names and the verified rerank route are required;
the names below are placeholders. Endpoint and credentials come from controlled
configuration. Optional `--prices-json` supplies per-role input/output rates in CNY
per million tokens; missing rates keep costs unknown:

```bash
SEMANTIC_EVAL_REAL=1 AWS_REGION=cn-north-1 AWS_DEFAULT_REGION=cn-north-1 \
PYTHONPATH=src python scripts/run_tuning.py --phase dev --real --output-dir NEW_OUTPUT \
  --embedding-model example-embedding-model --chat-model example-chat-model \
  --rerank-model example-rerank-model --rerank-path /rerank

SEMANTIC_EVAL_REAL=1 AWS_REGION=cn-north-1 AWS_DEFAULT_REGION=cn-north-1 \
PYTHONPATH=src python scripts/run_tuning.py --phase test --real --output-dir NEW_OUTPUT \
  --embedding-model example-embedding-model --chat-model example-chat-model \
  --rerank-model example-rerank-model --rerank-path /rerank \
  --freeze-evidence-ref FREEZE_EVIDENCE_REFERENCE
```

These commands spend the separately authorized model budget; `NEW_OUTPUT` is
a reproduction directory, not permission to repeat the current held-out test.
For a future authorized run, separately capture `recommendation.json`,
`split-manifest.json`, `model-config.json`, `accounting-config.json` and `freeze-boundary.json` after dev and before test;
pass that external capture reference through `--freeze-evidence-ref`. The reference
is recorded but not authenticated; an independent reviewer must verify the capture
contents and time. New ledger rows include UTC timestamps, and new recommendations
record `dev_finished_at` plus the split manifest and model configuration hashes.
Model names, revisions, embedding dimension, paths and the validated endpoint HMAC
must match across phases. Endpoint identity normalizes scheme/host case, the default
HTTPS port and trailing slashes before applying HMAC-SHA256 with a random 32-byte
salt. The salt is stored with owner-only permissions in gitignored
`.local/tuning-endpoint-salt`; keep it private and exclude it from captures. Reports
persist the HMAC, not the URL, credential or salt. This limits offline URL guessing
only while the salt remains private; it does not hide other model metadata or
attest to model weights. Preserve the salt for resumes and cross-phase comparison.
A missing or invalid salt refuses a frozen run; use a fresh output directory if
the salt is lost. Older unsalted configurations cannot resume under this format.
Endpoint comparison occurs after credential loading and before model requests or
test claims. The same HMAC separates the document cache across endpoints.
The persisted accounting configuration also requires the same explicit prices on resume. Local timestamps still do
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
model profile, endpoint HMAC and text. Query vectors stay in memory. Cache hits do not invent
provider usage. Retrieval latency is **reconstructed**: arms replay the same
single live query vector and add one measured query-embedding duration to their
local/rerank duration. This is not an end-to-end latency measurement for each arm.
The knee uses fresh query embedding for each search arm, and arm order is
randomized per paired query.
All reported methods include failure and abstention cases in their denominators.
