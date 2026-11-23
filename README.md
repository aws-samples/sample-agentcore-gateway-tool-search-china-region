# AgentCore Gateway 工具语义搜索

> 本仓库为样例代码，非生产用途，部署前请与贵司安全与法务团队确认合规要求。Agent 调用的是你配置的模型服务；查询与工具文本的数据流向见[安全与数据流](docs/security-and-data-flow.md)。

公开样例名称：`sample-agentcore-gateway-tool-search`。示例采用通用制造业场景，不包含业务系统的真实数据或部署记录。

本项目为中国区 AgentCore Gateway 提供工具语义搜索。
本样例仅支持部署到 AWS 中国（北京）`cn-north-1` 和中国（宁夏）`cn-northwest-1` 区域，部署定义使用 `arn:aws-cn`。
它帮助 Agent 先找到相关且已授权的工具，再调用业务工具。
仓库包含 REQUEST 拦截器、Search、Indexer、Enhancer 和 CDK 部署定义。

## 架构

REQUEST 校验身份和工具权限，Search 检索并复核授权；Indexer 构建关键词索引，Enhancer 生成工具向量，CDK 定义部署资源。
请求流程、索引发布和信任边界见[系统架构图](docs/architecture.md#系统总览)。

## 快速开始

前置条件：Python 3.12、uv、Node.js/npm。以下命令从仓库根目录运行，Python 依赖安装到 `.venv`，CDK CLI 安装到 `node_modules`。

### 安装

确认 uv 路径和版本，再按锁文件安装 Python 依赖。

```sh
command -v uv
uv --version
```

```sh
uv --no-config venv --python python3.12 --no-python-downloads --cache-dir .uv-cache .venv
uv --no-config pip install --python .venv/bin/python --no-deps --require-hashes --default-index https://pypi.org/simple --keyring-provider disabled --cache-dir .uv-cache --no-python-downloads -r requirements-build.lock
uv --no-config pip install --python .venv/bin/python --no-deps --require-hashes --no-build-isolation --default-index https://pypi.org/simple --keyring-provider disabled --cache-dir .uv-cache --no-python-downloads -r requirements.lock
```

先安装构建依赖是为了构建 jieba；下面按 npm 锁文件安装本地 CDK CLI。

```sh
mkdir -p .artifacts
: > .artifacts/npm-global.empty
npm ci --ignore-scripts --no-audit --no-fund --cache .npm-cache --userconfig /dev/null --globalconfig "$PWD/.artifacts/npm-global.empty" --registry https://registry.npmjs.org
```

### 构建及验证

依次构建 wheel 和 Lambda 包、检查代码、运行测试，再为北京和宁夏生成部署模板并审计依赖。
Lambda 首次打包下载锁定的公开依赖，第二次离线重建校验可复现性；测试与模板合成不访问 AWS。

```sh
.venv/bin/python scripts/build_dictionary.py
uv --no-config build --wheel --no-build-isolation --python .venv/bin/python --offline --cache-dir .uv-cache --out-dir dist
.venv/bin/python scripts/verify_wheel.py
.venv/bin/python scripts/build_lambda.py
.venv/bin/python scripts/build_lambda.py --offline
.venv/bin/python -m ruff check src tests scripts examples infra setup.py
.venv/bin/python -m ruff format --check src tests scripts examples infra setup.py
.venv/bin/python -m mypy --strict src infra setup.py \
  scripts/audit_dependencies.py scripts/build_dictionary.py scripts/build_lambda.py scripts/synth.py scripts/verify_wheel.py \
  scripts/run_evaluation.py scripts/generate_eval_data.py scripts/analyze_search_routes.py \
  scripts/enrich_search_measurement.py scripts/preflight_ecs_deployment.py scripts/collect_ecs_live_evidence.py \
  scripts/benchmark_dictionary.py scripts/benchmark_runtime_initialization.py \
  scripts/benchmark_request_initialization.py
.venv/bin/python -m pytest -ra --cov=semantic_core --cov=semantic_service --cov=semantic_eval \
  --cov=infra --cov=scripts --cov-report=term-missing
.venv/bin/python scripts/synth.py --region cn-north-1
.venv/bin/python scripts/synth.py --region cn-northwest-1
.venv/bin/python scripts/audit_dependencies.py
```

### 运行离线示例

示例用三个工具描述符和确定性 mock 演示建索引、保存、加载和授权搜索，输出两项已授权工具描述符。

```sh
PYTHONPATH=src .venv/bin/python examples/offline_search.py
```

## 配置与运行入口

| 组件 | 共享包 Lambda handler |
| --- | --- |
| REQUEST | `semantic_service.runtime.request_handler` |
| Search | `semantic_service.runtime.search_handler` |
| Indexer | `semantic_service.runtime.indexer_handler` |
| Enhancer | `semantic_service.runtime.enhancer_handler` |

Gateway 身份、两张状态表、模型开关等环境变量，以及版本化 S3 配置的读取规则，见[服务配置](docs/service.md#配置与入口)。
部署参数、REQUEST 独立包入口和可选 ECS Search 的配置见[部署步骤](docs/deploy.md)。

## 文档导航

| 我想做什么 | 读哪篇 |
| --- | --- |
| 理解为什么建设、做什么、效果与代价，以及当前状态 | [项目总览](docs/overview.md) |
| 看懂请求、索引、部署与信任边界 | [系统架构](docs/architecture.md) |
| 审查 ECS 主路、Lambda 回退、真实 target 绑定和安全边界 | [ECS Search 设计](docs/ecs-search-design.md) |
| 安排有损切换窗口、OAuth 轮换、告警和回滚 | [ECS 部署与运维](docs/ecs-search-operations.md) |
| 获取令牌、搜索工具、处理错误并继续调用业务工具 | [用户指南](docs/user-guide.md) |
| 构建、测试、扩展模型和分词、修改监控并准备评审 | [开发者指南](docs/developer-guide.md) |
| 部署、排障、轮换凭据、回滚和清理 | [运维手册](docs/operations-runbook.md) |
| 准备纯 Lambda 切换、退役与回滚 | [迁移至纯 Lambda](docs/migrate-to-lambda.md) |
| 核对冷启动、降级、测试身份服务、质量与容量的实测边界 | [已知限制](docs/known-limitations.md) |
| 审查认证授权、密钥、跨云数据和多租户隔离 | [安全与数据流](docs/security-and-data-flow.md) |
| 复跑扫描、审查逐项处置及维护有边界的豁免 | [安全扫描与豁免](docs/security-scanning.md) |
| 查看样例功能变更 | [变更记录](CHANGELOG.md) |
| 面向外部读者的中文技术文章：从工具发现问题讲架构、实现与实测取舍 | [中文技术博客](docs/blog/agentcore-gateway-semantic-search.md) |
| 查检索规则、模型契约和评测要求 | [详细设计](docs/design.md) |
| 配置运行环境和部署参数 | [服务配置](docs/service.md)、[部署步骤](docs/deploy.md) |
| 核对容量与配额 | [容量](docs/capacity.md)、[配额](docs/quotas.md) |
| 排查 embedding 错误、选择托管方式 | [embedding 诊断](docs/embedding-diagnostics.md)、[embedding 托管选择](docs/embedding-hosting.md) |

## 贡献与开发

代码分工、扩展方法和专项验证见[开发者指南](docs/developer-guide.md)；测试与 lint 命令在上面的[快速开始](#快速开始)中。

## 许可证

本项目采用 [MIT-0](LICENSE)；第三方归属见 [THIRD-PARTY-LICENSES](THIRD-PARTY-LICENSES)。贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 与 [行为准则](CODE_OF_CONDUCT.md)。
