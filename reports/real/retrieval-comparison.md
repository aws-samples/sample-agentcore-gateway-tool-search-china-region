# Synthetic retrieval: real model and offline mock

> **这是合成数据上的真实模型参考结果，不代表质量验收，也不构成开启 rerank 的依据。**

Among the six configurations, **VECTOR / without examples** had the highest observed primary-cohort Recall@5 (0.937) on the 65 primary queries. Adding the fixed examples decreased observed Recall@5 for every method (BM25 -2.44 percentage points; VECTOR -1.72 percentage points; RRF -1.08 percentage points). These are descriptive results for this sample; the paired intervals below show sampling uncertainty.

Matched corpus: 500 tools across 5 synthetic Gateways; 100 queries per configuration. Real vector retrieval uses `text-embedding-v4`; BM25 is local and therefore remains a lexical control. The mock embedding is a deterministic regression fixture, not a simulation of real model quality. Rerank is disabled in every real run. The mock baseline contains separate rerank rows; those rows are excluded from this comparison. `configured-eval-v1` is a local configuration label, not a provider-pinned backend revision; the `text-embedding-v4` and `qwen-plus` aliases may evolve.

The primary comparison uses the same 100-query deterministic evaluation round-robin strata in real and mock modes. The committed [340-query mock baseline](../offline-regression.md) is an additional historical reference, not the denominator of these matched comparisons. Metrics are macro averages within the selected query sample, without reweighting to the full dataset's stratum proportions.

Queries and descriptions share synthetic template vocabulary; there is no independent human holdout. The with-examples arm uses the dataset's fixed, versioned offline mock example queries. No actual LLM generated those examples during this run. This isolates the effect of indexing those particular examples, and does not evaluate live example generation.

Related summaries: [cost and usage](usage-cost.md) and [tool-count comparison](knee-comparison.md). Raw observations and run configuration are not distributed with the sample.

## Retrieval quality and denominators

Primary metrics use only the 65 queries with 1–5 authorized relevant tools. The other cohorts are 11 queries with more than 5 and 24 with none. Truth is the intersection of relevant tools and allowed tools. Recall@5 is macro recall; Hit@5 indicates any relevant top-5 result; MRR@5 is truncated at rank 5. Errors remain in denominators and contribute zero retrieval quality. Values are on a 0–1 scale with the query-bootstrap 95% CI in brackets.

### Primary cohort: n = 65

| Configuration | Real Recall@5 | Mock Recall@5 | Real Hit@5 | Mock Hit@5 | Real MRR@5 | Mock MRR@5 |
| --- | --- | --- | --- | --- | --- | --- |
| BM25 / without examples | 0.767 [0.690, 0.835] | 0.767 [0.690, 0.835] | 0.969 [0.923, 1.000] | 0.969 [0.923, 1.000] | 0.895 [0.818, 0.956] | 0.895 [0.818, 0.956] |
| VECTOR / without examples | 0.937 [0.897, 0.973] | 0.365 [0.270, 0.461] | 1.000 [1.000, 1.000] | 0.569 [0.446, 0.692] | 0.972 [0.937, 1.000] | 0.406 [0.294, 0.505] |
| RRF / without examples | 0.857 [0.792, 0.908] | 0.523 [0.422, 0.616] | 0.985 [0.954, 1.000] | 0.723 [0.615, 0.831] | 0.938 [0.885, 0.985] | 0.587 [0.473, 0.689] |
| BM25 / with examples | 0.743 [0.666, 0.812] | 0.743 [0.666, 0.812] | 0.969 [0.923, 1.000] | 0.969 [0.923, 1.000] | 0.895 [0.817, 0.956] | 0.895 [0.817, 0.956] |
| VECTOR / with examples | 0.919 [0.871, 0.959] | 0.382 [0.291, 0.468] | 1.000 [1.000, 1.000] | 0.631 [0.508, 0.738] | 0.974 [0.944, 1.000] | 0.418 [0.324, 0.518] |
| RRF / with examples | 0.846 [0.777, 0.902] | 0.595 [0.507, 0.682] | 0.985 [0.954, 1.000] | 0.831 [0.738, 0.923] | 0.928 [0.872, 0.977] | 0.693 [0.595, 0.788] |

### >5 cohort: n = 11

| Configuration | Real Recall@5 | Mock Recall@5 | Real Hit@5 | Mock Hit@5 | Real MRR@5 | Mock MRR@5 |
| --- | --- | --- | --- | --- | --- | --- |
| BM25 / without examples | 0.386 [0.318, 0.455] | 0.386 [0.318, 0.455] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| VECTOR / without examples | 0.386 [0.318, 0.455] | 0.277 [0.136, 0.409] | 1.000 [1.000, 1.000] | 0.636 [0.364, 0.909] | 1.000 [1.000, 1.000] | 0.564 [0.291, 0.819] |
| RRF / without examples | 0.386 [0.318, 0.455] | 0.336 [0.218, 0.441] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.882 [0.736, 1.000] |
| BM25 / with examples | 0.386 [0.318, 0.455] | 0.386 [0.318, 0.455] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| VECTOR / with examples | 0.386 [0.318, 0.455] | 0.277 [0.136, 0.409] | 1.000 [1.000, 1.000] | 0.636 [0.364, 0.909] | 1.000 [1.000, 1.000] | 0.564 [0.273, 0.836] |
| RRF / with examples | 0.386 [0.318, 0.455] | 0.341 [0.227, 0.441] | 1.000 [1.000, 1.000] | 0.909 [0.727, 1.000] | 1.000 [1.000, 1.000] | 0.864 [0.636, 1.000] |

For the >5 cohort, the corpus-specific macro Recall@5 ceiling is 0.386 [0.318, 0.455] because no more than five relevant tools can be returned. This cohort is kept separate from the primary denominator.

## Paired effect of fixed examples

Each delta is **with examples minus without examples**, paired by the same 65 primary query IDs. All deltas are on the original 0–1 metric scale. These additional 95% CIs use 1,000 query-pair bootstrap samples with seed 20261010 and linear percentile bounds. They describe only this synthetic query sample and do not remove label or example-generation bias.

| Method | Real ΔRecall@5 | Mock ΔRecall@5 | Real ΔHit@5 | Mock ΔHit@5 | Real ΔMRR@5 | Mock ΔMRR@5 |
| --- | --- | --- | --- | --- | --- | --- |
| BM25 | -0.024 [-0.051, -0.004] | -0.024 [-0.051, -0.004] | +0.000 [+0.000, +0.000] | +0.000 [+0.000, +0.000] | -0.001 [-0.002, +0.000] | -0.001 [-0.002, +0.000] |
| VECTOR | -0.017 [-0.047, +0.003] | +0.017 [-0.078, +0.114] | +0.000 [+0.000, +0.000] | +0.062 [-0.062, +0.185] | +0.002 [-0.026, +0.034] | +0.013 [-0.083, +0.103] |
| RRF | -0.011 [-0.037, +0.011] | +0.073 [-0.014, +0.158] | +0.000 [+0.000, +0.000] | +0.108 [-0.015, +0.215] | -0.010 [-0.028, +0.000] | +0.106 [-0.010, +0.226] |

## Errors, authorization, and empty truth

The following counts cover all 100 matched queries. Unauthorized results count every returned tool outside the allowed set, including ranks after five.

| Configuration | Real n | Mock n | Real errors | Mock errors | Real unauthorized results | Mock unauthorized results |
| --- | --- | --- | --- | --- | --- | --- |
| BM25 / without examples | 100 | 100 | 0 | 0 | 0 | 0 |
| VECTOR / without examples | 100 | 100 | 0 | 0 | 0 | 0 |
| RRF / without examples | 100 | 100 | 0 | 0 | 0 | 0 |
| BM25 / with examples | 100 | 100 | 0 | 0 | 0 | 0 |
| VECTOR / with examples | 100 | 100 | 0 | 0 | 0 | 0 |
| RRF / with examples | 100 | 100 | 0 | 0 | 0 | 0 |

For the 24 queries with zero authorized relevant tools, a false recommendation means any top-5 result; correct empty means no result and no request error. This measures retrieval behavior, not an agent's refusal quality.

| Configuration | Real false recommendation | Mock false recommendation | Real correct empty | Mock correct empty |
| --- | --- | --- | --- | --- |
| BM25 / without examples | 0.750 [0.583, 0.917] | 0.750 [0.583, 0.917] | 0.250 [0.083, 0.417] | 0.250 [0.083, 0.417] |
| VECTOR / without examples | 0.750 [0.583, 0.917] | 0.750 [0.583, 0.917] | 0.250 [0.083, 0.417] | 0.250 [0.083, 0.417] |
| RRF / without examples | 0.750 [0.583, 0.917] | 0.750 [0.583, 0.917] | 0.250 [0.083, 0.417] | 0.250 [0.083, 0.458] |
| BM25 / with examples | 0.750 [0.583, 0.917] | 0.750 [0.583, 0.917] | 0.250 [0.083, 0.417] | 0.250 [0.083, 0.417] |
| VECTOR / with examples | 0.750 [0.583, 0.917] | 0.750 [0.583, 0.917] | 0.250 [0.083, 0.417] | 0.250 [0.083, 0.417] |
| RRF / with examples | 0.750 [0.583, 0.917] | 0.750 [0.583, 0.917] | 0.250 [0.083, 0.417] | 0.250 [0.083, 0.417] |

## Latency and actual retrieval modes

Latency is warm local retrieval elapsed time, including query embedding provider calls when used and retaining failed requests. Index build is excluded. It is not Gateway end-to-end latency or a production network SLO; mock timing has no provider round trip. p50/p95 use all 100 matched queries; units are milliseconds.

| Configuration | Real p50 [95% CI] | Mock p50 [95% CI] | Real p95 [95% CI] | Mock p95 [95% CI] |
| --- | --- | --- | --- | --- |
| BM25 / without examples | 0.19 [0.18, 0.20] | 0.20 [0.18, 0.21] | 0.26 [0.23, 0.30] | 0.29 [0.25, 0.30] |
| VECTOR / without examples | 398.00 [389.65, 411.87] | 0.11 [0.10, 0.11] | 568.88 [515.78, 661.81] | 0.13 [0.12, 0.18] |
| RRF / without examples | 392.77 [384.86, 401.41] | 0.32 [0.30, 0.34] | 539.19 [457.88, 591.08] | 0.44 [0.40, 0.46] |
| BM25 / with examples | 0.23 [0.22, 0.24] | 0.22 [0.21, 0.23] | 0.34 [0.31, 0.35] | 0.29 [0.27, 0.31] |
| VECTOR / with examples | 413.79 [400.13, 423.71] | 0.12 [0.11, 0.12] | 583.35 [513.68, 633.74] | 0.15 [0.13, 0.19] |
| RRF / with examples | 410.34 [402.87, 427.26] | 0.38 [0.36, 0.39] | 792.59 [523.32, 1091.56] | 0.50 [0.45, 0.53] |

| Configured retrieval | Real observed modes | Mock observed modes |
| --- | --- | --- |
| BM25 / without examples | bm25: 100 | bm25: 100 |
| VECTOR / without examples | vector: 100 | vector: 100 |
| RRF / without examples | bm25: 6, rrf: 94 | bm25: 6, rrf: 94 |
| BM25 / with examples | bm25: 100 | bm25: 100 |
| VECTOR / with examples | vector: 100 | vector: 100 |
| RRF / with examples | bm25: 6, rrf: 94 | bm25: 6, rrf: 94 |

Configured RRF can report BM25 for queries with an empty authorized catalog or a degradation path. The observed mode table is authoritative for execution counts; a configured method name alone does not establish that a vector provider was used for every query. No business tool was executed.

> **这是合成数据上的真实模型参考结果，不代表质量验收，也不构成开启 rerank 的依据。**
