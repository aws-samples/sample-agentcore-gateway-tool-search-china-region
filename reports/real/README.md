# Real-model synthetic reference

**这是合成数据上的真实模型参考结果，不代表质量验收，也不构成开启 rerank 的依据。**

Read the [retrieval comparison](retrieval-comparison.md), [tool-count comparison](knee-comparison.md),
[provider usage and estimated cost](usage-cost.md).
The comparisons use matched offline fixtures, retain errors in denominators, and
make no production quality or capacity claim.

## Run scope

The completed reference uses the deterministic 100-query stratified
retrieval sample, with 30 paired selection queries at each of 20/50/100/200/500
tools. Both real-mode switches are required. Rerank is disabled; models are
text-embedding-v4 (1024 dimensions, batches of 10) and qwen-plus (thinking off).
Existing versioned mock examples are used; no new live examples are generated.

Only Markdown summaries are distributed. Raw JSON/JSONL, incomplete attempts and mock output are omitted. These summaries describe historical synthetic experiments; they do not prove current-default performance. A fresh run generates new raw data in its chosen local output directory.

## Reproduction

Install the existing locked dependencies using the repository
[installation instructions](../../README.md). The external runner configures and
observes deterministic code; it does not modify library files. It retains keys only
in memory through the configured credential loader, and preserves transport size
and deadline checks. Output is limited to synthetic results and numeric metadata;
secret values, AWS identity and endpoint host are never written.

Run a fresh reference into a new output directory:

```sh
SEMANTIC_EVAL_REAL=1 AWS_REGION=cn-north-1 AWS_DEFAULT_REGION=cn-north-1 \
PYTHONPATH=src .venv/bin/python reports/real/run_reference.py --mode real \
  --query-limit 100 --timeout 60 --output-dir .local/reference-rerun
PYTHONPATH=src .venv/bin/python scripts/run_evaluation.py --mode mock \
  --query-limit 100 --queries-per-size 30 --output-dir .local/reference-rerun/mock
python3 reports/real/summarize_cost.py --report-dir .local/reference-rerun
python3 reports/real/summarize_comparisons.py --report-dir .local/reference-rerun
```

Actual model aliases may evolve; the local revision label does not pin provider
weights. Timings include the local provider round trip, exclude index build for
selection, and do not measure Gateway or business-tool execution. Full load
always precedes search first, so cache, warm-up and time effects are not randomized.
Raw fixture account components are redacted consistently in committed report IDs;
source dataset digests still describe the unmodified synthetic input.
