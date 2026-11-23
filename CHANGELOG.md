# 变更记录

## 0.1.0

- 提供 REQUEST、Search、Indexer、Enhancer 及中国区域 CDK 部署定义。
- 支持授权向量检索、BM25 降级、版本化索引及可选重排。
- 提供可选 ECS Search 后端、独立 Lambda 回退与 REQUEST 初始化预取。
- 增加 OIDC、模型出口、索引发布及请求预算诊断。
- 提供确定性合成数据、离线测试及可复现的 wheel/Lambda 构建。
- 整理公开样例范围，采用 MIT-0 许可证；协议测试使用合成输入。

功能、部署前提和限制分别见 [项目总览](docs/overview.md)、[部署步骤](docs/deploy.md)
和 [已知限制](docs/known-limitations.md)。本记录不声明任何云端部署或生产验收结果。
