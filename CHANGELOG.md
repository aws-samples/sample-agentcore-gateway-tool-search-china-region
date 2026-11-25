# 变更记录

## 未发布：模型服务配置与迁移

- MCP REQUEST 接受有界字符串或整数请求 ID，统一拒绝空白、超界及不可编码的 ID，保留类型回显与签名绑定；兼容 Inspector 的 `_meta` 和列表 cursor。为兼容 Gateway context 字符串化，全部封套校验通过后，允许签名整数与 canonical 十进制字符串 context 单向匹配；拒绝反向及非 canonical 写法，并记录仅日志的 `binding_match="mcp_id_str_coerced"`。数字 ID 搜索仍需部署验收。
- 收紧 `tools/call` 未知 params 键和机器身份 `tools/list` params 校验；普通列表保持单页。私有路由日志摘要加入 ID 类型前缀，facade/ECS 混合版本期间关联值可能不同。
- 启用模型时显式提供 `MODEL_EMBEDDING_MODEL`、`MODEL_CHAT_MODEL`；启用重排还需 `MODEL_RERANK_MODEL`。端点由非空 `MODEL_BASE_URL` 或 secret 的 `base_url` 提供，前者优先；空白环境值使用 secret，二者皆缺时模型调用报配置不可用。
- 嵌套重排格式使用 `MODEL_RERANK_ENVELOPE=nested-output`，对应 `input`/`parameters` 请求及 `output.results` 响应；顶层格式继续使用 `flat`。
- Python 调用方从 `semantic_core.http_factories` 导入 `OriginHTTPConfig`、`create_embedding_provider`、`create_chat_provider`、`create_rerank_provider`，替代 `semantic_core.bailian` 的配置与 provider 工厂。配置工厂使用 `openai_embedding_config`、`openai_chat_config`、`configured_rerank_config`，显式传入模型名、revision 及适用的 codec；不提供旧名称别名。
- 真实评测与参考脚本要求 `--embedding-model`、`--chat-model`；启用重排的评测还需 `--rerank-model`。真实调优要求三类模型参数及已验证的 `--rerank-path`。mock 模式无需模型参数。
- 费用汇总默认保留 usage 并显示费用未知；只有显式输入单价时才估算费用，不内置历史报价。匿名历史报告保留结论，不列模型身份与服务价目。
- 调优的端点身份使用规范化 URL 与本地私有盐的 HMAC；跨阶段须保留 `.local/tuning-endpoint-salt`，该文件不能随报告分享。旧的无盐配置不能用于继续冻结运行，请使用新的输出目录。

## 0.1.0

- 提供 REQUEST、Search、Indexer、Enhancer 及中国区域 CDK 部署定义。
- 支持授权向量检索、BM25 降级、版本化索引及可选重排。
- 提供可选 ECS Search 后端、独立 Lambda 回退与 REQUEST 初始化预取。
- 增加 OIDC、模型出口、索引发布及请求预算诊断。
- 提供确定性合成数据、离线测试及可复现的 wheel/Lambda 构建。
- 整理公开样例范围，采用 MIT-0 许可证；协议测试使用合成输入。

功能、部署前提和限制分别见 [项目总览](docs/overview.md)、[部署步骤](docs/deploy.md)
和 [已知限制](docs/known-limitations.md)。本记录不声明任何云端部署或生产验收结果。
