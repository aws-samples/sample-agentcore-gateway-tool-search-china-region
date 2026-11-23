**这是合成数据上的真实模型参考结果，不代表质量验收，也不构成开启 rerank 的依据。**

# 工具数量与选择实验 / Synthetic tool-count experiment

**合成数据：本报告只用于回归或受控合成实验，不代表真实质量。质量验收延期；不得据此宣称 Recall 达标或批准开启 rerank。**

These configured tool-count comparisons do not establish a production breakpoint. Tool selection is evaluated without executing any business tool.

## Protocol

Controlled single-Gateway synthetic remapping; singleton authorized truth inserted in each nested distractor catalog. Query and descriptor specify source namespace so duplicate CRUD functions have unambiguous truth. Same queries/catalogs paired across arms. Search-first uses the original query for configured RRF retrieval then one selection chat, both with the same namespace prefix; no tool execution. Language labels refer to the source query before that prefix. Actual search modes report BM25 degradation explicitly. Index build excluded, retrieval included in latency.

Accuracy is the fraction of all attempted queries selecting an annotated authorized relevant tool. Errors and absent selections count as incorrect and stay in the latency denominator. Intervals are seeded query-bootstrap percentile 95% CIs within each arm; the intervals describe this synthetic sample. Index construction and end-to-end Gateway behavior are outside the timed comparison.

The search-first arm is configured for RRF. Search modes show the actual query counts, including any BM25 fallback; full-load rows are not_applicable. JSON observations retain selected, expected and candidate tool IDs so accuracy can be independently recomputed.

```json
{
  "queries_actual_per_size": 30,
  "queries_requested_per_size": 30,
  "seed": 20261010,
  "selector_profile": {
    "max_tokens": 256,
    "model_id": "qwen-plus",
    "revision": "configured-eval-v1",
    "temperature": 0
  }
}
```

## Accuracy and latency

| Tools | Arm | Query denominator n | Accuracy [95% CI] | Errors | Candidates min..max | p50 ms [95% CI] | p95 ms [95% CI] | Search modes (query counts) |
|---:|---|---:|---|---:|---|---|---|---|
| 20 | full_load | 30 | 1.0000 [1.0000, 1.0000] | 0 | 20..20 | 1267.0570 [1228.2451, 1354.0748] | 3594.5121 [2555.9372, 3900.7329] | {&quot;not_applicable&quot;: 30} |
| 20 | search_first | 30 | 0.9667 [0.9000, 1.0000] | 0 | 5..5 | 1569.1348 [1495.4736, 1696.0541] | 3155.1471 [1901.7158, 4447.2944] | {&quot;rrf&quot;: 30} |
| 50 | full_load | 30 | 0.9667 [0.9000, 1.0000] | 0 | 50..50 | 1855.5998 [1562.8512, 2687.9639] | 4423.8747 [3367.3974, 6921.5564] | {&quot;not_applicable&quot;: 30} |
| 50 | search_first | 30 | 0.8667 [0.7000, 0.9667] | 0 | 5..5 | 1661.9155 [1565.1031, 1743.2615] | 3260.7819 [2523.7681, 3659.9761] | {&quot;rrf&quot;: 30} |
| 100 | full_load | 30 | 1.0000 [1.0000, 1.0000] | 0 | 100..100 | 2021.4203 [1936.1760, 2232.0117] | 4776.0776 [3859.8832, 5573.5331] | {&quot;not_applicable&quot;: 30} |
| 100 | search_first | 30 | 0.9000 [0.7667, 1.0000] | 0 | 5..5 | 1639.1896 [1566.9393, 1783.3114] | 3604.4787 [2027.1097, 4650.6922] | {&quot;rrf&quot;: 30} |
| 200 | full_load | 30 | 0.9333 [0.8333, 1.0000] | 0 | 200..200 | 2988.6392 [2857.2118, 3499.2480] | 6166.6958 [4305.9403, 6638.6295] | {&quot;not_applicable&quot;: 30} |
| 200 | search_first | 30 | 0.8000 [0.6333, 0.9333] | 0 | 5..5 | 1471.6286 [1421.6077, 1565.5031] | 2114.4467 [1664.3733, 2968.1907] | {&quot;rrf&quot;: 30} |
| 500 | full_load | 30 | 0.8667 [0.7333, 0.9667] | 0 | 500..500 | 8743.7491 [7798.5700, 10015.8063] | 11846.5659 [11154.5917, 14026.4734] | {&quot;not_applicable&quot;: 30} |
| 500 | search_first | 30 | 0.6000 [0.4333, 0.7667] | 1 | 5..5 | 1601.8833 [1476.2475, 1796.8185] | 3070.1987 [2357.5928, 5367.2829] | {&quot;rrf&quot;: 30} |

## Token usage and estimation

Chat usage is provider-reported only; missing fields remain null. Mock estimate is serialized prompt UTF-8 bytes/4, not model tokens. Search embedding usage is reported separately in provider_accounting.

Provider-reported fields are measured chat usage returned by the configured endpoint. Each mean includes requests that reported that field, with its own denominator n; missing usage stays N/A and is never filled with zero. Mock-estimated prompt usage is the ceiling of serialized prompt UTF-8 bytes/4, not model tokenization or a billing measurement. Estimated and reported token columns must not be compared as equivalent. Search embedding usage is separate from chat usage.

| Tools | Arm | Provider-reported mean prompt tokens [95% CI], n | Provider-reported mean completion tokens [95% CI], n | Provider-reported mean total tokens [95% CI], n | Mock-estimated mean prompt units [95% CI], n |
|---:|---|---|---|---|---|
| 20 | full_load | 2508.5000 [2496.0283, 2521.6000] (n=30) | 50.0000 [37.2325, 65.0358] (n=30) | 2558.5000 [2540.0292, 2579.2000] (n=30) | N/A (n=0) |
| 20 | search_first | 749.0333 [738.4975, 760.4342] (n=30) | 34.3000 [25.4992, 45.2017] (n=30) | 783.3333 [768.2333, 800.3783] (n=30) | N/A (n=0) |
| 50 | full_load | 6000.9333 [5974.6792, 6029.8075] (n=30) | 53.6000 [40.1958, 68.7692] (n=30) | 6054.5333 [6025.3267, 6084.0833] (n=30) | N/A (n=0) |
| 50 | search_first | 753.7333 [744.4642, 763.7667] (n=30) | 33.9333 [25.8942, 42.6683] (n=30) | 787.6667 [775.1967, 800.8692] (n=30) | N/A (n=0) |
| 100 | full_load | 11798.9333 [11767.4992, 11833.7083] (n=30) | 42.4667 [30.5317, 57.0742] (n=30) | 11841.4000 [11809.5142, 11878.0333] (n=30) | N/A (n=0) |
| 100 | search_first | 752.9000 [742.3317, 763.5683] (n=30) | 35.8667 [25.9333, 50.1008] (n=30) | 788.7667 [773.3300, 806.3017] (n=30) | N/A (n=0) |
| 200 | full_load | 23382.4667 [23344.5483, 23425.1058] (n=30) | 44.4333 [32.5658, 57.6367] (n=30) | 23426.9000 [23387.0550, 23472.4617] (n=30) | N/A (n=0) |
| 200 | search_first | 742.0667 [732.1658, 752.6350] (n=30) | 21.7000 [18.0650, 25.8675] (n=30) | 763.7667 [753.8650, 774.5342] (n=30) | N/A (n=0) |
| 500 | full_load | 58209.2000 [58208.4000, 58209.9333] (n=30) | 55.4333 [40.2525, 73.7333] (n=30) | 58264.6333 [58249.1992, 58282.8342] (n=30) | N/A (n=0) |
| 500 | search_first | 744.5862 [734.3371, 754.6259] (n=29) | 35.0690 [23.9931, 50.9034] (n=29) | 779.6552 [762.5741, 799.7966] (n=29) | N/A (n=0) |

## Provider accounting

Counters retain the runner's accounting scope; a shared budget can also include retrieval ablations in the same invocation.

```json
{
  "calls": {
    "embedding": 816
  },
  "document_cache_hits": 1600,
  "errors": {},
  "limit": 1187,
  "usage": {
    "embedding": {
      "prompt_tokens": 118078,
      "total_tokens": 118078
    }
  }
}
```
