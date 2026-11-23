# Synthetic tool-count experiment: real model and offline mock

> **这是合成数据上的真实模型参考结果，不代表质量验收，也不构成开启 rerank 的依据。**

The observed p50 elapsed-latency crossover in favor of search first is bracketed by 20 and 50 tools in the tested grid. Search first had lower observed p50 at 50, 100, 200, 500 tools. Its observed accuracy was lower at every tested size. At 500 tools, full-load accuracy was 86.7% versus 60.0% for search first. No tested size combined lower p50 latency with preserved observed accuracy. These are descriptive results on this synthetic sample, not a production knee.

Each size (20, 50, 100, 200, 500 tools) uses the same 30 paired queries: 150 query-size pairs and 300 selection attempts per provider mode. Each catalog contains one authorized truth plus nested synthetic distractors. Catalogs and queries are matched between real and mock modes and between arms. The namespace prefix makes repeated CRUD functions distinguishable. Real selection uses `qwen-plus`; search first uses `text-embedding-v4` with configured RRF, followed by one selection chat over retrieved candidates. Rerank remains disabled. No business tool is executed. `configured-eval-v1` is a local configuration label, not a provider-pinned backend revision; the model aliases may evolve.

Knee queries are selected from the full source dataset, independently of the smaller matched retrieval subset. Real and mock knee dataset hashes match each other; they need not equal the retrieval-subset hashes. These rows describe the supplied completed result files. Provider attempts and reported usage are summarized separately in [cost and usage](usage-cost.md).

The fixed experiment order is **full load first, then search first for each query**. Repeated queries, shared prompts, provider-side prompt caching, warm-up, and time-varying network/model conditions can confound latency and cost differences. There is no randomized arm-order or cold-cache control. Provider cache counters, when available, are in the usage evidence. The mock selector is a deterministic regression fixture, not a model-quality or latency surrogate.

Related summaries: [retrieval comparison](retrieval-comparison.md) and [cost and usage](usage-cost.md). Raw observations and run configuration are not distributed with the sample.

## Accuracy, errors, and candidate truth

Accuracy measures selected tool identity only. Request/response errors count as incorrect and stay in the 30-query denominator. Brackets contain query-bootstrap 95% CIs. Candidate truth coverage below is recomputed from recorded candidate IDs; it is distinct from the harness `truth_present_count`, which describes truth inserted into the source catalog. If retrieval fails before updating candidates, the harness retains the initial catalog and selection is not called, so recorded coverage alone does not prove that selection received the truth.

| Tools | Arm | Real accuracy [95% CI] | Mock accuracy [95% CI] | Real errors | Mock errors | Real candidate truth | Mock candidate truth |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 20 | Full load | 1.000 [1.000, 1.000] | 0.400 [0.233, 0.567] | 0 | 0 | 30/30 | 30/30 |
| 20 | Search first | 0.967 [0.900, 1.000] | 0.400 [0.233, 0.567] | 0 | 0 | 30/30 | 21/30 |
| 50 | Full load | 0.967 [0.900, 1.000] | 0.367 [0.200, 0.533] | 0 | 0 | 30/30 | 30/30 |
| 50 | Search first | 0.867 [0.700, 0.967] | 0.200 [0.067, 0.367] | 0 | 0 | 28/30 | 13/30 |
| 100 | Full load | 1.000 [1.000, 1.000] | 0.267 [0.133, 0.433] | 0 | 0 | 30/30 | 30/30 |
| 100 | Search first | 0.900 [0.767, 1.000] | 0.100 [0.000, 0.233] | 0 | 0 | 27/30 | 9/30 |
| 200 | Full load | 0.933 [0.833, 1.000] | 0.233 [0.100, 0.400] | 0 | 0 | 30/30 | 30/30 |
| 200 | Search first | 0.800 [0.633, 0.933] | 0.033 [0.000, 0.100] | 0 | 0 | 24/30 | 4/30 |
| 500 | Full load | 0.867 [0.733, 0.967] | 0.233 [0.100, 0.400] | 0 | 0 | 30/30 | 30/30 |
| 500 | Search first | 0.600 [0.433, 0.767] | 0.167 [0.033, 0.300] | 1 | 0 | 18/30 | 8/30 |

| Tools | Arm | Real candidate range | Mock candidate range | Real selections outside candidates | Mock selections outside candidates |
| --- | --- | --- | --- | --- | --- |
| 20 | Full load | 20–20 | 20–20 | 0 | 0 |
| 20 | Search first | 5–5 | 5–5 | 0 | 0 |
| 50 | Full load | 50–50 | 50–50 | 0 | 0 |
| 50 | Search first | 5–5 | 5–5 | 0 | 0 |
| 100 | Full load | 100–100 | 100–100 | 0 | 0 |
| 100 | Search first | 5–5 | 5–5 | 0 | 0 |
| 200 | Full load | 200–200 | 200–200 | 0 | 0 |
| 200 | Search first | 5–5 | 5–5 | 0 | 0 |
| 500 | Full load | 500–500 | 500–500 | 0 | 0 |
| 500 | Search first | 5–5 | 5–5 | 0 | 0 |

## Chat tokens and coverage

Real values below are **provider-reported chat tokens retained in selection observations**, displayed as mean [95% CI]; `n/30` is field coverage. Missing usage is n/a, never zero. Mock values are serialized prompt UTF-8 bytes/4 estimates, not model tokens, and cannot be used to infer real token savings. Search embedding usage is separate. The original usage ledger (not distributed) and [cost report](usage-cost.md) additionally retain numeric provider usage from responses that failed later adapter validation; those requests can be billable even when selection-level usage is absent.

| Tools | Arm | Real prompt tokens; coverage | Real completion tokens; coverage | Real total tokens; coverage | Mock prompt bytes/4; coverage |
| --- | --- | --- | --- | --- | --- |
| 20 | Full load | 2508.5 [2496.0, 2521.6]; 30/30 | 50.0 [37.2, 65.0]; 30/30 | 2558.5 [2540.0, 2579.2]; 30/30 | 2388.2 [2375.7, 2400.4]; 30/30 |
| 20 | Search first | 749.0 [738.5, 760.4]; 30/30 | 34.3 [25.5, 45.2]; 30/30 | 783.3 [768.2, 800.4]; 30/30 | 715.6 [706.5, 725.4]; 30/30 |
| 50 | Full load | 6000.9 [5974.7, 6029.8]; 30/30 | 53.6 [40.2, 68.8]; 30/30 | 6054.5 [6025.3, 6084.1]; 30/30 | 5724.3 [5702.4, 5747.9]; 30/30 |
| 50 | Search first | 753.7 [744.5, 763.8]; 30/30 | 33.9 [25.9, 42.7]; 30/30 | 787.7 [775.2, 800.9]; 30/30 | 713.6 [703.2, 725.0]; 30/30 |
| 100 | Full load | 11798.9 [11767.5, 11833.7]; 30/30 | 42.5 [30.5, 57.1]; 30/30 | 11841.4 [11809.5, 11878.0]; 30/30 | 11252.4 [11220.9, 11287.7]; 30/30 |
| 100 | Search first | 752.9 [742.3, 763.6]; 30/30 | 35.9 [25.9, 50.1]; 30/30 | 788.8 [773.3, 806.3]; 30/30 | 720.6 [711.3, 730.5]; 30/30 |
| 200 | Full load | 23382.5 [23344.5, 23425.1]; 30/30 | 44.4 [32.6, 57.6]; 30/30 | 23426.9 [23387.1, 23472.5]; 30/30 | 22326.0 [22285.4, 22367.2]; 30/30 |
| 200 | Search first | 742.1 [732.2, 752.6]; 30/30 | 21.7 [18.1, 25.9]; 30/30 | 763.8 [753.9, 774.5]; 30/30 | 713.1 [701.2, 724.8]; 30/30 |
| 500 | Full load | 58209.2 [58208.4, 58209.9]; 30/30 | 55.4 [40.3, 73.7]; 30/30 | 58264.6 [58249.2, 58282.8]; 30/30 | 55586.6 [55585.3, 55587.7]; 30/30 |
| 500 | Search first | 744.6 [734.3, 754.6]; 29/30 | 35.1 [24.0, 50.9]; 29/30 | 779.7 [762.6, 799.8]; 29/30 | 715.0 [704.7, 725.0]; 30/30 |

## Paired arm differences

Accuracy and elapsed-time deltas are **search first minus full load** on the same queries. Negative latency favors search first. The median elapsed delta is the median of query-pair differences, not the difference of the two marginal medians. The real prompt-token reduction is `100 × (1 − sum(search prompt tokens) / sum(full-load prompt tokens))`, using only pairs with provider-reported prompt counts in both arms and positive full-load counts. Positive reduction means fewer prompt tokens; it excludes search embedding tokens, completion tokens, and cache pricing.

Additional paired 95% CIs use 1,000 query-pair bootstrap samples with seed 20261010 and linear percentile bounds. Sizes reuse the same queries, so observations across sizes are not independent; intervals do not establish a production threshold or correct synthetic sample bias.

| Tools | Real Δaccuracy | Mock Δaccuracy | Real mean Δms | Mock mean Δms | Real median Δms | Mock median Δms |
| --- | --- | --- | --- | --- | --- | --- |
| 20 | -0.033 [-0.100, +0.000] | +0.000 [-0.100, +0.100] | +56.67 [-327.90, +440.97] | -0.14 [-0.18, -0.11] | +316.15 [+189.90, +415.41] | -0.15 [-0.20, -0.06] |
| 50 | -0.100 [-0.233, +0.000] | -0.167 [-0.333, +0.000] | -527.29 [-1029.52, -145.41] | -0.03 [-0.06, +0.00] | +17.13 [-906.59, +81.82] | -0.02 [-0.07, +0.00] |
| 100 | -0.100 [-0.233, +0.000] | -0.167 [-0.301, -0.033] | -689.20 [-1187.48, -228.90] | -0.11 [-0.14, -0.09] | -345.12 [-671.25, -296.73] | -0.11 [-0.13, -0.09] |
| 200 | -0.133 [-0.300, +0.067] | -0.200 [-0.367, -0.067] | -1986.88 [-2395.92, -1632.25] | -0.38 [-0.43, -0.34] | -1594.29 [-1958.21, -1342.70] | -0.38 [-0.40, -0.35] |
| 500 | -0.267 [-0.467, -0.033] | -0.067 [-0.200, +0.067] | -7386.02 [-8165.45, -6691.39] | -1.63 [-2.18, -1.32] | -6802.77 [-8364.71, -6173.86] | -1.38 [-1.45, -1.29] |

| Tools | Real paired prompt-token reduction % [95% CI] | Complete pairs |
| --- | --- | --- |
| 20 | 70.14 [69.70, 70.57] | 30/30 |
| 50 | 87.44 [87.25, 87.60] | 30/30 |
| 100 | 93.62 [93.52, 93.70] | 30/30 |
| 200 | 96.83 [96.79, 96.87] | 30/30 |
| 500 | 98.72 [98.70, 98.74] | 29/30 |

## Latency including search

Elapsed time includes retrieval (when present) and the selection call; index build and business-tool execution are excluded. Failed attempts remain in latency summaries. Units are milliseconds. These measurements include provider/network conditions of this run and are not Gateway latency SLOs.

| Tools | Arm | Real p50 [95% CI] | Mock p50 [95% CI] | Real p95 [95% CI] | Mock p95 [95% CI] |
| --- | --- | --- | --- | --- | --- |
| 20 | Full load | 1267.06 [1228.25, 1354.07] | 0.48 [0.41, 0.53] | 3594.51 [2555.94, 3900.73] | 0.69 [0.60, 0.82] |
| 20 | Search first | 1569.13 [1495.47, 1696.05] | 0.33 [0.31, 0.37] | 3155.15 [1901.72, 4447.29] | 0.39 [0.38, 0.50] |
| 50 | Full load | 1855.60 [1562.85, 2687.96] | 0.50 [0.46, 0.56] | 4423.87 [3367.40, 6921.56] | 0.69 [0.63, 0.74] |
| 50 | Search first | 1661.92 [1565.10, 1743.26] | 0.49 [0.46, 0.52] | 3260.78 [2523.77, 3659.98] | 0.60 [0.53, 0.67] |
| 100 | Full load | 2021.42 [1936.18, 2232.01] | 0.78 [0.77, 0.81] | 4776.08 [3859.88, 5573.53] | 0.86 [0.83, 1.01] |
| 100 | Search first | 1639.19 [1566.94, 1783.31] | 0.69 [0.67, 0.70] | 3604.48 [2027.11, 4650.69] | 0.75 [0.71, 0.77] |
| 200 | Full load | 2988.64 [2857.21, 3499.25] | 1.50 [1.44, 1.52] | 6166.70 [4305.94, 6638.63] | 1.64 [1.60, 1.75] |
| 200 | Search first | 1471.63 [1421.61, 1565.50] | 1.10 [1.06, 1.14] | 2114.45 [1664.37, 2968.19] | 1.27 [1.21, 1.32] |
| 500 | Full load | 8743.75 [7798.57, 10015.81] | 3.56 [3.49, 3.62] | 11846.57 [11154.59, 14026.47] | 3.87 [3.65, 11.23] |
| 500 | Search first | 1601.88 [1476.25, 1796.82] | 2.15 [2.13, 2.23] | 3070.20 [2357.59, 5367.28] | 2.42 [2.27, 2.53] |

The search-only component is already included in elapsed time above and must not be added again. A search that fails before completion can retain a zero `search_ms` in the harness; the full elapsed attempt still appears in the preceding table.

| Tools | Real search p50 / p95 ms | Mock search p50 / p95 ms | Real observed search modes | Mock observed search modes |
| --- | --- | --- | --- | --- |
| 20 | 410.93 / 591.55 | 0.27 / 0.33 | rrf: 30 | rrf: 30 |
| 50 | 414.44 / 689.13 | 0.43 / 0.52 | rrf: 30 | rrf: 30 |
| 100 | 409.57 / 586.40 | 0.62 / 0.68 | rrf: 30 | rrf: 30 |
| 200 | 414.20 / 609.38 | 1.04 / 1.15 | rrf: 30 | rrf: 30 |
| 500 | 420.95 / 603.52 | 2.07 / 2.31 | rrf: 30 | rrf: 30 |

## Interpretation boundary

The paired rows describe the observed accuracy, prompt volume, and elapsed time trade-off for these 30 synthetic queries and catalog sizes. A tool-count point with lower prompt volume or latency is not by itself an accuracy knee or a recommended production cutoff. Candidate truth loss, response errors, the small shared sample, arm order, provider caching, and synthetic labels must be considered together. No actual LLM-generated example queries are evaluated here, and this experiment does not exercise a business tool.

> **这是合成数据上的真实模型参考结果，不代表质量验收，也不构成开启 rerank 的依据。**
