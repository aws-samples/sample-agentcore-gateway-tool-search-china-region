# 工具数量与选择实验 / Synthetic tool-count experiment

历史合成实验摘要，使用旧分词配置；不代表当前默认配置的结果。原始运行数据不随样例发布。

**合成数据：本报告只用于回归或受控合成实验，不代表真实质量。质量验收延期；不得据此宣称 Recall 达标或批准开启 rerank。**

These configured tool-count comparisons do not establish a production breakpoint. Tool selection is evaluated without executing any business tool.

Selection accuracy is not an informative axis under the mock selector (it is a fixture, not a capability). The mock knee is a latency/retrieval-recall baseline only; the accuracy knee requires the real selector.

## Protocol

Controlled single-Gateway synthetic remapping; singleton authorized truth inserted in each nested distractor catalog. Query and descriptor specify source namespace so duplicate CRUD functions have unambiguous truth. Same queries/catalogs paired across arms. Search-first uses the original query for configured RRF retrieval then one selection chat, both with the same namespace prefix; no tool execution. Language labels refer to the source query before that prefix. Actual search modes report BM25 degradation explicitly. Index build excluded, retrieval included in latency.

Accuracy is the fraction of all attempted queries selecting an annotated authorized relevant tool. Errors and absent selections count as incorrect and stay in the latency denominator. Intervals are seeded query-bootstrap percentile 95% CIs within each arm; the intervals describe this synthetic sample. Index construction and end-to-end Gateway behavior are outside the timed comparison.

The search-first arm is configured for RRF. Search modes show the actual query counts, including any BM25 fallback; full-load rows are not_applicable. JSON observations retain selected, expected and candidate tool IDs so accuracy can be independently recomputed.

```json
{
  "queries_actual_per_size": 10,
  "queries_requested_per_size": 10,
  "seed": 20261010,
  "selector_profile": {
    "model_id": "mock-llm",
    "revision": "fixture-v1",
    "selection_rule": "deterministic lexical overlap fixture"
  }
}
```

## Accuracy and latency

| Tools | Arm | Query denominator n | Accuracy [95% CI] | Errors | Candidates min..max | p50 ms [95% CI] | p95 ms [95% CI] | Search modes (query counts) |
|---:|---|---:|---|---:|---|---|---|---|
| 20 | full_load | 10 | 0.1000 [0.0000, 0.3000] | 0 | 20..20 | 0.5886 [0.5554, 0.7065] | 0.7514 [0.6310, 0.7547] | {&quot;not_applicable&quot;: 10} |
| 20 | search_first | 10 | 0.1000 [0.0000, 0.3000] | 0 | 5..5 | 0.3623 [0.3351, 0.4028] | 0.4238 [0.3785, 0.4310] | {&quot;rrf&quot;: 10} |
| 50 | full_load | 10 | 0.1000 [0.0000, 0.3000] | 0 | 50..50 | 0.7948 [0.7575, 0.8727] | 0.8864 [0.8281, 0.8961] | {&quot;not_applicable&quot;: 10} |
| 50 | search_first | 10 | 0.1000 [0.0000, 0.3000] | 0 | 5..5 | 0.5184 [0.4823, 0.5865] | 0.6749 [0.5491, 0.7289] | {&quot;rrf&quot;: 10} |
| 100 | full_load | 10 | 0.1000 [0.0000, 0.3000] | 0 | 100..100 | 1.0859 [1.0043, 1.2625] | 1.3867 [1.1894, 1.4810] | {&quot;not_applicable&quot;: 10} |
| 100 | search_first | 10 | 0.1000 [0.0000, 0.3000] | 0 | 5..5 | 0.7768 [0.7152, 0.8295] | 0.8500 [0.8039, 0.8605] | {&quot;rrf&quot;: 10} |
| 200 | full_load | 10 | 0.1000 [0.0000, 0.3000] | 0 | 200..200 | 1.5549 [1.5213, 1.6455] | 1.6979 [1.6039, 1.7067] | {&quot;not_applicable&quot;: 10} |
| 200 | search_first | 10 | 0.1000 [0.0000, 0.3000] | 0 | 5..5 | 1.0946 [1.0562, 1.1306] | 1.1573 [1.1050, 1.1751] | {&quot;rrf&quot;: 10} |
| 500 | full_load | 10 | 0.1000 [0.0000, 0.3000] | 0 | 500..500 | 3.3106 [3.2929, 3.3614] | 3.4370 [3.3376, 3.4854] | {&quot;not_applicable&quot;: 10} |
| 500 | search_first | 10 | 0.1000 [0.0000, 0.3000] | 0 | 5..5 | 2.0810 [2.0447, 2.1225] | 2.1394 [2.1030, 2.1440] | {&quot;rrf&quot;: 10} |

## Token usage and estimation

Chat usage is provider-reported only; missing fields remain null. Mock estimate is serialized prompt UTF-8 bytes/4, not model tokens. Search embedding usage is reported separately in provider_accounting.

Provider-reported fields are measured chat usage returned by the configured endpoint. Each mean includes requests that reported that field, with its own denominator n; missing usage stays N/A and is never filled with zero. Mock-estimated prompt usage is the ceiling of serialized prompt UTF-8 bytes/4, not model tokenization or a billing measurement. Estimated and reported token columns must not be compared as equivalent. Search embedding usage is separate from chat usage.

| Tools | Arm | Provider-reported mean prompt tokens [95% CI], n | Provider-reported mean completion tokens [95% CI], n | Provider-reported mean total tokens [95% CI], n | Mock-estimated mean prompt units [95% CI], n |
|---:|---|---|---|---|---|
| 20 | full_load | N/A (n=0) | N/A (n=0) | N/A (n=0) | 2397.0000 [2377.6975, 2416.5025] (n=10) |
| 20 | search_first | N/A (n=0) | N/A (n=0) | N/A (n=0) | 710.6000 [695.1950, 725.5100] (n=10) |
| 50 | full_load | N/A (n=0) | N/A (n=0) | N/A (n=0) | 5725.6000 [5679.8650, 5766.5200] (n=10) |
| 50 | search_first | N/A (n=0) | N/A (n=0) | N/A (n=0) | 718.0000 [700.0975, 738.8025] (n=10) |
| 100 | full_load | N/A (n=0) | N/A (n=0) | N/A (n=0) | 11250.9000 [11182.6900, 11318.1075] (n=10) |
| 100 | search_first | N/A (n=0) | N/A (n=0) | N/A (n=0) | 723.3000 [704.4925, 742.9025] (n=10) |
| 200 | full_load | N/A (n=0) | N/A (n=0) | N/A (n=0) | 22312.9000 [22225.0850, 22390.6125] (n=10) |
| 200 | search_first | N/A (n=0) | N/A (n=0) | N/A (n=0) | 710.0000 [692.7925, 727.0025] (n=10) |
| 500 | full_load | N/A (n=0) | N/A (n=0) | N/A (n=0) | 55585.8000 [55583.5000, 55588.1000] (n=10) |
| 500 | search_first | N/A (n=0) | N/A (n=0) | N/A (n=0) | 702.9000 [686.0975, 722.2000] (n=10) |

## Provider accounting

Counters retain the runner's accounting scope; a shared budget can also include retrieval ablations in the same invocation.

```json
{
  "calls": {
    "chat": 100,
    "embedding": 2064,
    "rerank": 640
  },
  "document_cache_hits": 1600,
  "errors": {},
  "limit": null,
  "usage": {
    "chat": {},
    "embedding": {
      "input_items": 4870
    },
    "rerank": {
      "input_items": 11280
    }
  }
}
```
