# 费用口径

**合成数据，不代表质量验收。**

费用是 provider 返回 usage 按北京地域公开目录价折算的人民币参考值，不是代理账单。核验日期：2026-10-11。不扣免费额度、促销、批处理或缓存折扣；实际代理单价与部署地域尚未确认。

| 模型与模式 | 单次输入区间 | 输入 / 百万 token | 输出 / 百万 token | 官方来源 |
|---|---:|---:|---:|---|
| text-embedding-v4，同步 | 全部 | ¥0.50 | 不适用 | [模型价格](https://help.aliyun.com/en/model-studio/text-embedding-v4) |
| qwen3-rerank，北京 | 全部 | ¥0.50 | 不适用 | [模型价格](https://help.aliyun.com/en/model-studio/qwen3-rerank) |
| qwen-plus，非思考 | ≤128,000 | ¥0.80 | ¥2.00 | [模型价格](https://help.aliyun.com/en/model-studio/qwen-plus) |
| qwen-plus，非思考 | 128,000–256,000 | ¥2.40 | ¥20.00 | [模型价格](https://help.aliyun.com/en/model-studio/qwen-plus) |
| qwen-plus，非思考 | >256,000 | ¥4.80 | ¥48.00 | [模型价格](https://help.aliyun.com/en/model-studio/qwen-plus) |

qwen3-rerank 官方页的 China (Beijing) 标签为 ¥0.50；Singapore 标签为 ¥0.749。本实验统一使用北京公开价参考口径。Supersedes: rerank 暂定 ¥0.80/百万 token，因为模型专页已核验为 ¥0.50。

每个 HTTP 请求在发出前落盘计数；失败、中断和缺失 usage 的请求也占调用预算。只保存允许清单非负整数计数与固定状态标签，不保存响应文本、请求内容或凭证。缺失、非法、别名互相矛盾、total 与 input/output 不一致的 usage 记为未知；未知请求存在时 `public_list_price_cny=null`，仅 `known_public_list_price_cny` 表示可计算部分，`unknown_cost_requests` 单独列出。明确上报零 token 与缺失 usage 分开。

embedding/rerank 可使用 input/prompt 或 total token 数；chat 必须具有输入与输出计数。使用 `enable_thinking=false` 的调用按非思考价折算。缓存 token 数仅用于一致性核查，不自动推定代理支持供应商缓存折扣。

文档向量缓存存于 gitignored 本地目录，以完整模型 profile、`document` purpose 和文本的 SHA-256 为键。查询向量只驻内存，不写入该缓存。缓存命中无新 HTTP 调用，也不虚构 provider usage。
