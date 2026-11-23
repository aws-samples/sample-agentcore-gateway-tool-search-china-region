# 调用量与费用

**这是合成数据上的真实模型参考结果，不代表质量验收，也不构成开启 rerank 的依据。**

**状态：完整运行。** 统计时间（UTC）：2026-10-11T03:11:47.834789+00:00。

本快照包括 1920 次 HTTP 尝试，其中配置探测 2 次。公开价估算为 **人民币 2.65306570 元**，覆盖 1919 次有完整且一致 usage 的成功响应。另有 1 次费用未知；此数值为可计算部分的小计，未知费用不按零处理。

估算使用百炼华北 2（北京）同步调用公开价，不扣免费额度、优惠或异步 Batch 折扣。代理实际收费标准不可得，因此不是实际账单。

| 阶段 | 类型 | 尝试 | 200 响应头 | 完整 200 响应 | 失败/未完整响应 | 无 usage | usage 不完整/不一致 | 输入 token | 输出 token | 可估请求 | 元 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| configuration_probe | embedding | 1 | 1 | 1 | 0 | 0 | 0 | 5 | 0 | 1 | 0.00000250 |
| evaluate | embedding | 616 | 616 | 616 | 0 | 0 | 0 | 88328 | 0 | 616 | 0.04416400 |
| incomplete_configuration_probe | embedding | 1 | 1 | 1 | 0 | 0 | 0 | 5 | 0 | 1 | 0.00000250 |
| incomplete_evaluate | embedding | 802 | 802 | 801 | 1 | 1 | 1 | 68457 | 0 | 801 | 0.03422850 |
| run_knee | chat | 300 | 300 | 300 | 0 | 0 | 0 | 3169254 | 12195 | 300 | 2.55979320 |
| run_knee | embedding | 200 | 200 | 200 | 0 | 0 | 0 | 29750 | 0 | 200 | 0.01487500 |

| 模型 | 尝试 | 输入 token | 输出 token | 可估请求 | 费用未知请求 | 元 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| qwen-plus | 300 | 3169254 | 12195 | 300 | 0 | 2.55979320 |
| text-embedding-v4 | 1620 | 186545 | 0 | 1619 | 1 | 0.09327250 |

Embedding + rerank 尝试数：**1620 / 2,000**；rerank 请求数：**0**。

HTTP 状态计数：{"200": 1920}。无 usage 与 usage 不完整/不一致两列可重叠；详细状态与原始数值字段分别汇总于 JSON，不能将各字段再次相加。
HTTP 200 响应头不等于完整响应。传输错误共 1 次，其中 1 次在收到 200 响应头后失败；这些尝试计入请求总量，费用仍为未知。完整响应以观察器解码完整响应体后设置的标记确认。

费用按每次请求计算。Embedding 优先使用 `prompt_tokens`，其次 `input_tokens`，均缺失且无输出 token 时才使用 `total_tokens`。Chat 分别使用输入与输出字段；`total_tokens` 不再相加，`reasoning_tokens` 也不额外计数。非 200、传输错误、未观察到完整响应体、缺 usage、字段矛盾或不符合本次非思考配置的请求均标为费用未知。

| 模型 / 单次输入量 | 输入元/百万 token | 非思考输出元/百万 token |
| --- | ---: | ---: |
| text-embedding-v4 | 0.5 | 0 |
| qwen-plus <= 128,000 | 0.8 | 2 |
| qwen-plus 128,000 < input <= 256,000 | 2.4 | 20 |
| qwen-plus 256,000 < input <= 1,000,000 | 4.8 | 48 |

Chat 配置为 `enable_thinking=false`；每次请求的全部输入/输出分别使用其输入量对应档位，K = 1,000。请求单价计算后再求和，不使用全局累计输入选择阶梯。

缓存敏感性估算：人民币 **2.64806858 元**。仅对 300 次明确返回 `cached_tokens`、且未报告显式缓存创建的成功 Chat 请求，按缓存输入原价的 20% 折算；其余可估请求保持原价。该值不表示代理实际采用此折扣，未返回明细的请求不假定命中。

费用未知原因（同一请求可能有多个原因）：`{"complete_response_body_not_observed": 1, "missing_input_or_output_usage": 1, "transport_error_cost_unknown": 1}`。

[公开价格](https://help.aliyun.com/zh/model-studio/model-pricing) · [Embedding usage](https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api) · [Chat usage](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions) · [缓存计费](https://help.aliyun.com/zh/model-studio/context-cache)

公开价格核对日期：2026-10-11。
