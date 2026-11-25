# 费用口径

**合成数据，不代表质量验收。**

历史实验的模型身份未公开，本报告不列服务价目，原始账本未分发；报告中的费用数字是当次估算结论，不是新运行的默认报价或代理账单。新运行按模型服务的公开价目与合同配置单价，缺价时只保留 usage 并将费用标为未知。

每个 HTTP 请求在发出前落盘计数；失败、中断和缺失 usage 的请求也占调用预算。只保存允许清单非负整数计数与固定状态标签，不保存响应文本、请求内容或凭证。缺失、非法、别名互相矛盾、total 与 input/output 不一致的 usage 记为未知；未知请求存在时 `public_list_price_cny=null`，仅 `known_public_list_price_cny` 表示可计算部分，`unknown_cost_requests` 单独列出。明确上报零 token 与缺失 usage 分开。

embedding/rerank 可使用 input/prompt 或 total token 数；chat 必须具有输入与输出计数。新运行须核对实际生成模式与对应价目。缓存 token 数仅用于一致性核查，不自动推定服务支持缓存折扣。

文档向量缓存存于 gitignored 本地目录，以完整模型 profile、私有盐生成的端点 HMAC、`document` purpose 和文本的 SHA-256 为键。查询向量只驻内存，不写入该缓存。缓存命中无新 HTTP 调用，也不虚构 provider usage。

新调优运行通过 `--prices-json` 提供按角色划分的 `[输入单价, 输出单价]`，角色为 `embedding`、`rerank`、`chat`，单位为人民币/百万 token。该简化估算使用固定单价，阶梯或复杂合同需另行核算。未配置的角色保持费用未知；汇总器不内置历史费率。
