# 公开价格与 usage 口径

**这是合成数据上的真实模型参考结果，不代表质量验收，也不构成开启 rerank 的依据。**

核对日期：2026-10-11。以下为百炼华北 2（北京）的人民币公开同步调用单价，按 provider 返回的 usage 估算；不代表代理账单，不扣免费额度或账户优惠，不使用异步 Batch 的半价。

| 模型 / 单次输入量 | 输入（元 / 百万 token） | 非思考输出（元 / 百万 token） | 思考输出，含思维链（元 / 百万 token） |
| --- | ---: | ---: | ---: |
| text-embedding-v4 | 0.5 | 不收费 | 不适用 |
| qwen-plus：0 < input <= 128,000 | 0.8 | 2 | 8 |
| qwen-plus：128,000 < input <= 256,000 | 2.4 | 20 | 24 |
| qwen-plus：256,000 < input <= 1,000,000 | 4.8 | 48 | 64 |

[百炼模型价格](https://help.aliyun.com/zh/model-studio/model-pricing)将 qwen-plus 当前能力对应到 qwen-plus-2025-12-01。阶梯按每个请求的输入总量选择，该请求全部 token 使用该档价格，K = 1,000，M = 1,000,000。官方别名对应关系不能证明代理实际使用的后端版本。

Embedding 按返回的 `usage.total_tokens` 计输入量，不能假定存在 `prompt_tokens`；官方文档说明 text-embedding-v4 仅返回 `total_tokens`。[同步向量 API](https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api)规定每批最多 10 条、每条最多 8,192 token，维度支持 2048、1536、1024（默认）、768、512、256、128、64。代理实际响应应单独核实。

Chat 使用 `usage.prompt_tokens` 和 `usage.completion_tokens`；`total_tokens` 是二者之和，不再重复相加。思考 token 已包含在输出 token 中，不额外叠加 `reasoning_tokens`。工具描述计入输入。[Chat API](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)、[工具调用](https://help.aliyun.com/zh/model-studio/qwen-function-calling)

官方 qwen-plus 默认不启用思考；代理是否保留该默认值仍需依据实际请求与响应判断。[思考模式](https://help.aliyun.com/zh/model-studio/deep-thinking)

按请求计算无缓存折扣的公开价：`(prompt_tokens * input_rate + completion_tokens * output_rate) / 1,000,000`；embedding 为 `total_tokens * 0.5 / 1,000,000`。未知或缺失 usage 不能按零用量处理。

官方支持 qwen-plus 上下文缓存。隐式缓存自动运行且无法关闭；有缓存命中时，使用 `prompt_tokens_details.cached_tokens`，其输入价为标准输入价的 20%，其余输入按原价。显式缓存创建通常为 125%、命中为 10%，只有实际使用且返回对应明细时才应计算。如果代理未返回缓存明细，报告应注明“未计缓存折扣的公开价估算”，不能断言没有命中。[上下文缓存](https://help.aliyun.com/zh/model-studio/context-cache)

已查的官方 Chat API 和 Function Calling 指南未给出 `tools` 数组的硬性数量上限；指南建议最终候选工具不超过 20 个，这是实践建议，不能解释为 20、128 或其他数量的 API 硬限制。200/500 工具的可用性由本次代理响应验证。[工具调用指南](https://help.aliyun.com/zh/model-studio/qwen-function-calling)
