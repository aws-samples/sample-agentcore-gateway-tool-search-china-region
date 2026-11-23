# 开发者指南

本指南说明如何修改、验证和评审本仓库，供维护检索核心、Lambda 服务和部署模板的开发者使用。项目通过先搜索工具再调用工具，减少 Agent 加载全部工具定义的需要，同时保留权限检查；整体背景见[项目总览](overview.md)。读完后，你可以在独立工作树中完成本地构建、增加模型适配、修改分词规则，并提交可复核的变更。线上部署和凭证操作由[运维手册](operations-runbook.md)说明。

## 先理解代码分工

Provider 是模型调用接口的实现：检索或增强需要模型时，通过它发送请求并校验响应。索引快照是一组已发布的目录、词法统计和可选向量文件；Search 首次使用或发现新版本时加载它。快照恢复和首次服务初始化都有耗时，按[阶段计时表](service.md#冷启动分段计时)观察；实测及优化边界见[已知限制](known-limitations.md)。

| 位置 | 负责什么 | 修改时先看 |
| --- | --- | --- |
| [semantic_core](../src/semantic_core/) | 中文分词、BM25 词法评分、向量、排名、模型接口、本地快照格式 | [search.py](../src/semantic_core/search.py)、[providers.py](../src/semantic_core/providers.py) |
| [semantic_service](../src/semantic_service/) | REQUEST 身份检查、Search 授权、目录轮询、增强、远程存储 | [runtime.py](../src/semantic_service/runtime.py)、[服务配置](service.md) |
| [semantic_eval](../src/semantic_eval/) | 合成数据、质量指标、工具选择对照和调优 | [评测口径](#合成评测与离线基线) |
| [infra](../infra/) | CDK 资源、运行角色、配置对象、调度和告警 | [stack.py](../infra/stack.py)、[部署边界](deploy.md#资源和区域边界) |
| [scripts](../scripts/) | 构建、词典生成、离线 synth、审计和评测入口 | [构建及验证命令](../README.md#构建及验证) |
| [tests](../tests/) | 数学、服务、协议回放、部署模板和文档回归 | [conftest.py](../tests/conftest.py) |
| [tests/fixtures](../tests/fixtures/) | 明确标识的合成输入 | [合成夹具说明](../tests/fixtures/README.md) |
| [data](../data/)、[reports](../reports/) | 合成评测输入和历史冻结报告 | [当前默认与历史结果的区别](known-limitations.md) |

服务默认使用 `vector_bm25_fallback`，核心库 `SearchConfig` 默认仍是 `rrf`。前者先用向量排序，低相似度时可选有正匹配的 BM25 排序；后者融合两个通道的名次。写测试时显式说明采用哪一层默认值，不用核心库测试替代服务配置测试。精确规则见[设计第 5 节](design.md#5-检索和存储)。

## 我要完成一次本地构建

1. 创建独立 Git 工作树，记录基线提交。使用 Python 3.12、现有 `uv` 和 Node.js/npm。完整安装命令以 [README 安装](../README.md#安装)为准；Python 包装入当前工作树的 `.venv`，CDK CLI 装入本地 `node_modules`。
2. 按 [README 构建及验证](../README.md#构建及验证)逐条执行。下表说明执行次序和每组命令验证什么，不另维护一份命令副本。
3. 检查 Git 差异，确认没有提交生成的词典、安装目录、构建包、凭证或原始日志。构建产物保存在忽略目录，测试日志保存在仓库外或忽略目录。

| README 命令组 | 成功标准 | 失败时先查 |
| --- | --- | --- |
| `build_dictionary.py`、离线 wheel 构建、`verify_wheel.py` | 词典生成并校验，wheel 含所需运行资源 | Python 版本、锁定的 jieba/setuptools、构建输入摘要 |
| `build_lambda.py`，再加 `--offline` 重建 | 目标 wheel 校验通过，第二次无需下载且产物可复现 | [目标 wheel 锁](../lambda-wheels.lock.json)、本地 wheel 缓存、构建 manifest |
| Ruff 检查、格式检查、mypy strict | 命令退出码为零 | 使用 README 的完整目录和脚本列表，避免漏查部署代码 |
| pytest 完整覆盖率命令 | 全部普通测试通过；真实集成按开关跳过 | 具体失败断言；文档中的默认值和表格也是受测契约 |
| 北京、宁夏各运行 `scripts/synth.py` | 两区都生成模板，分区及权限断言通过 | 配置 schema、资源引用、目标包；不要改为直接联网 lookup |
| `audit_dependencies.py` | 已安装依赖、传递依赖、目标 wheel 和 CDK CLI 与锁一致 | 是否混入未锁定包，是否先完成 Lambda 构建 |

Synth 是把 CDK 程序转换为 CloudFormation 模板的本地步骤，不会部署资源。本仓库的 [synth 脚本](../scripts/synth.py)隔离继承环境、AWS 配置和凭证，并关闭 lookup。生成模板成功不能证明账号配额、目标 IdP 连通性或线上性能通过。

下文提供词典与初始化基准、专项协议回归、行为变异检查、离线搜索及 provider 示例、合成数据重建与离线评测。运行这些命令时保留各自的成功标准：正常变异检查应通过；故意启用 `late-child-mask` 或 `acl-average-length` 的两条命令应各返回退出码 `1`，证明测试能发现预设错误。依赖重新解析、真实集成和真实模型评测属于显式开启的路径，不是普通离线构建的一部分。

## 我要选择合适的测试

| 层级 | 检查内容 | 入口与边界 |
| --- | --- | --- |
| 核心单元测试 | BM25 数学、向量归一化、授权屏蔽、候选聚合、降级、版本化分词 | [test_bm25.py](../tests/test_bm25.py)、[test_search.py](../tests/test_search.py)、[test_versioned_recall.py](../tests/test_versioned_recall.py) |
| 服务测试 | 身份封套、撤权、目录分页、快照发布、缓存、凭据刷新 | [test_service_roundtrip.py](../tests/test_service_roundtrip.py)、[test_catalog_auth_refresh.py](../tests/test_catalog_auth_refresh.py)；使用替代存储和 HTTP transport |
| 协议回放 | Gateway/模型的合成输入能否被当前代码正确解析 | [合成夹具说明](../tests/fixtures/README.md)；离线用例不代表线上验证 |
| 基础设施测试 | 中国区 ARN、最小权限、表隔离、告警和日志字段匹配 | [tests/infra](../tests/infra/) 与双区域 synth |
| 离线评测 | 固定数据上的召回、误推荐、语言分组及逐查询退化 | [离线评测命令](#合成评测与离线基线)；mock 分数不是生产质量 |
| 真实集成 | 外部模型协议 | 必须同时设置 `SEMANTIC_CORE_RUN_INTEGRATION=1` 和 pytest `--run-integration`；当前 fixture 未配置时仍跳过 |
| 真实模型评测 | 指定模型上的合成检索与工具选择实验 | 必须同时设置 `SEMANTIC_EVAL_REAL=1` 和 `--mode real`；会检查凭证并读取模型密钥，只在获准范围执行 |

普通测试在 [conftest.py](../tests/conftest.py)中阻断网络。不要为让测试通过而删除这项保护。覆盖率必须同时统计 `semantic_core`、`semantic_service`、`semantic_eval`、`infra` 和 `scripts`，完整命令见 README。

修改行为时先写能复现缺陷的断言，确认旧实现失败，再修改实现并确认通过；这就是红绿测试。纯文字修订复用现有文档测试，不为了增加测试数量而重复检查实现本身。

## 我要新增模型 provider

1. 先判断是否只需换配置。已有 [OpenAI-compatible 适配器](../src/semantic_core/openai_compatible.py)和 [rerank 适配器](../src/semantic_core/rerank.py)支持受控的地址、路径和响应格式；配置示例见 [provider_profiles.py](../examples/provider_profiles.py)。兼容模型名称不等于已验证协议。
2. 需要新实现时，遵守 [providers.py](../src/semantic_core/providers.py)中的 `EmbeddingProvider`、`LLMProvider` 或 `RerankProvider`。`describe()` 返回明确的 `ModelProfile`；调用使用 `RequestContext` 的剩余截止时间，不能自行延长预算。
3. 验证输入和输出。Embedding 必须返回匹配的模型身份、行数、维度和归一化向量；rerank 必须返回所有候选的一一对应分数；生成问句必须通过本地 schema。错误使用受控 `ProviderError`，保留合法 `usage` 与 `failure`，不把上游正文放进异常。
4. 增加离线成功、超时、认证、限流、错误外壳、模型回显不符、非法向量等测试。使用 `MockTransport`；模型密钥由服务层注入，不让核心适配器发现环境凭证。
5. 如果要接入 Lambda，再修改 [模型设置](../src/semantic_service/models.py)、[运行时组装](../src/semantic_service/runtime.py)及对应测试。更换模型、revision、维度或向量空间时，同时更新查询侧和文档侧并重建索引。

成功标准是协议测试、服务降级及原授权回归全部通过。失败时先检查地址拼接、精确 host allowlist、模型回显与预算，不用关闭校验来兼容未知响应。Embedding 与 chat 使用独立的传输超时；现有配置和限制见 [Adapter 配置边界](#adapter-配置边界)。

## 我要修改分词、业务词典或同义词

分词把输入文本拆成检索使用的词；同义词扩展在词法检索时补充已配置的等价词。文档与查询必须采用同一份配置，否则查询词与索引统计可能不匹配。这些操作不改写发给模型的原始查询。

1. 在 [新索引配置](../src/semantic_core/_index_config.py)修改 `TOKENIZER_CONFIG`；业务词和同义词分别更新 `dictionary_version`、`synonyms_version`。当前新索引采用 `synonyms-v2`。不要只改标签而不记录内容变化，也不要只替换查询侧分词器。
2. 保持 [TokenizerConfig](../src/semantic_core/tokenization.py) 的单跳扩展约束，并新增动作、资源名、中英文、否定词及复合词的回归。同步核对 [BM25 预算和权重](../src/semantic_core/bm25.py)。
3. 核对旧快照仍恢复持久化配置；Indexer 在配置变化时发布新代次，即使目录摘要不变也要重建；Enhancer 沿用原代次配置。测试入口见 [test_versioned_recall.py](../tests/test_versioned_recall.py)。
4. 按[迁移与召回验收](deploy.md#分词配置迁移与召回验收)做新旧对照，分别检查健康向量路径和 BM25 降级路径。保留逐查询退化，不以整体平均提升代替回归分析。

成功标准是新规则生效、旧快照可回放、授权行为不变，并且已解释逐查询差异。`TokenizerConfig.version` 是受实现支持的格式标记；任意递增会被拒绝。只有改变格式兼容协议时才连同读写器和兼容测试一起修改。

新构建的索引使用快照内带版本的 `TokenizerConfig`（`synonyms-v2`），
文档和查询对称进行单跳同义词扩展：
创建（create/新增/新建/创建/添加）、读取（read/查看/查询/读取）、
更新（update/更新/修改/编辑）、删除（delete/删除/移除）和商品
（product/商品/产品）。搜索层不再另行改写查询，也不按 OOV 或向量是否可用切换
扩展规则。“查找工具”中的“查找”不解释成 read。授权 mask 仍在全目录评分后、
候选截取前应用；embedding、rerank 和授权绑定仍使用原始查询。
两个入口通过
`IndexSnapshot.build(..., tokenizer=...)` 显式传入分词器；省略参数时采用当前新索引配置。
新旧配置的 TF/DF/IDF、长度和词法排序可能不同，
包括低相似度兜底与 RRF；旧快照自身的分数和行为仍可回放。
资源名词目前只覆盖商品一组：仅匹配动作时可能返回其他资源，罗列同义词的文档
会累加贡献；“更新日志”等复合名词也可能被误解为操作。整体 Recall@5 提升不代表
每条查询均改善，须同时检查逐查询退化、跨资源表达及健康/降级路径。
同义词、旧配置持久化和降级回归使用独立合成数据，见 [test_versioned_recall.py](../tests/test_versioned_recall.py)。

## 我要修改指标过滤器或告警

指标过滤器从结构化日志中提取数值；告警再对这些指标应用阈值。两者必须同时与实际日志字段一致，否则服务正常输出日志时，告警仍可能没有数据。

1. 先查 [runtime.py](../src/semantic_service/runtime.py) 的实际日志输出，再改 [infra/monitoring.py](../infra/monitoring.py)。Indexer 的固定计数由 [COUNTER_FIELDS](../src/semantic_service/_indexer_telemetry.py)共享；embedding 分类来自 [FailureKind](../src/semantic_core/providers.py)。
2. 保持受控名称和数值维度。不要加入 query、工具描述、JWT、密钥、完整 URL、原始错误正文或任意响应头。
3. 用真实结构的合成日志检查过滤器匹配，再核对 synth 中的周期、比较方式、阈值、缺失数据处理和通知目标。重点回归见 [test_embedding_log_contract.py](../tests/infra/test_embedding_log_contract.py)、[test_indexer_log_contract.py](../tests/infra/test_indexer_log_contract.py)及 [test_oidc_monitoring.py](../tests/test_oidc_monitoring.py)。
4. 更新[运维告警表](operations-runbook.md)和适用的[服务字段表](service.md)。新指标不自动拥有告警；例如 embedding 错误分类当前是计数过滤器。

成功标准是日志样本被预期过滤器捕获，两区模板与处置文档一致。修改 Lambda 代码不会自动更新已部署的 CloudWatch 过滤器；后续必须部署相应 CDK 变更并验证入库。

## 我要复现构建或审计依赖

复现构建先使用同一份输入和锁文件，再比较产物摘要；这不等同于“在另一台机器上能安装”。完整过程以 [README 构建及验证](../README.md#构建及验证)和[依赖锁更新](#依赖锁更新)为准。

| 文件 | 约束 |
| --- | --- |
| [pyproject.toml](../pyproject.toml) | Python 版本、直接依赖、构建依赖与检查配置 |
| [requirements.lock](../requirements.lock) | 完整 Python 依赖版本和公开分发物 SHA-256 |
| [requirements-build.lock](../requirements-build.lock) | 构建 jieba 所需的固定 setuptools |
| [lambda-wheels.lock.json](../lambda-wheels.lock.json) | Linux x86_64 / Python 3.12 目标 wheel、标签和摘要 |
| [package-lock.json](../package-lock.json) | 本地 CDK CLI 版本和 integrity |

日常构建不重新解析锁，也不运行未固定版本的全局 CDK。只有依赖变更获准后才更新锁，并同步构建依赖、目标 wheel 锁及许可证审计。静态 wheel 标签和摘要检查不能替代目标 Lambda 的实际导入验证。

## 我要提交可评审的变更

1. 限定修改范围，核对 Git 差异和未跟踪文件。报告问题、最终行为、验证结果及仍未验证的环境条件。
2. 提交信息使用纯 ASCII；中文正文保留在文档中。提交后运行以下原样检查，退出码 **1** 表示未发现非 ASCII 字符：

   ```sh
   git log -1 --format=%B | LC_ALL=C command grep -n '[^[:blank:] -~]'
   ```

3. 由独立评审者复核授权边界、错误处理、测试和文档。需要代码评审时先准备草稿，不发布、不合并，直到获得对应授权；本仓库未提供草稿评审 CLI 的固定命令。
4. 每次 squash、amend 或生成代码评审后重新检查提交信息。记录已完成检查和准确的失败/跳过原因，不把未运行的真实集成写为通过。

提交应让评审者从最终差异理解变更。不要提交原始模型输出、个人绝对路径、临时证据或真实凭证；仅分享经审查的脱敏记录。

## 构建与依赖细节

`pyproject.toml` 固定直接依赖，`requirements.lock` 固定完整传递依赖及分发物 SHA-256；Windows 专属依赖保留环境 marker。
uv 是外部 CLI，不在 Python 锁文件内；复现前记录实际路径和版本。
`requirements-build.lock` 只包含同版本、同 hash 的 setuptools，先安装它是为了在当前 `.venv` 内构建 jieba 的源分发物，避免额外隔离环境下载未锁定依赖。
jieba 0.42.1 使用 `pkg_resources`，因此运行时也显式固定 setuptools 80.9.0。
CDK CLI 固定在 [package-lock.json](../package-lock.json)，npm 校验 SHA-512 integrity。
安装命令为用户与全局 npm 配置使用两个不同的空文件路径，避免重复加载同一个 `/dev/null`。
后续使用 `node_modules/.bin/cdk`，不使用未锁定的 `npx latest`。

wheel 输出到 `dist/`。README 的 `build_dictionary.py` 命令为源码测试生成忽略的词典资源；wheel 的构建 hook
和 Lambda 打包器各自重新生成并校验产物，不依赖源码目录曾经生成过资源。
源码和 sdist 携带带固定 SHA-256 的基础词典输入；普通隔离构建只需声明的 setuptools，
不需要提前安装 jieba。全套测试会用仅含该声明构建依赖的新环境验证源码和 sdist 构建。
pytest 的 `pythonpath` 指向 `src`，因此开发测试无需另装 editable package。
mypy strict覆盖全部`src`、`infra`、构建 hook 及 README 所列工具脚本；
未修改的历史benchmark/mutation脚本不扩大类型检查范围。coverage同时统计核心、服务、
评测库、infra和scripts目录，未执行的脚本行仍显示为未覆盖。
jieba/jsonschema使用`follow_untyped_imports`；
boto3/botocore 的无类型导入限于 SDK 边界，服务以窄 Protocol 约束其操作。

依赖审计逐一核对当前平台的锁定版本、已安装分发物及由已批准根包可达的必需
依赖，清单默认写到忽略的 `.artifacts/dependency-closure.json`；可用 `--output` 指定
本地暂存文件。过程产物保存在仓库外。
该清单包含本地Python/CDK依赖、Linux目标wheel的hash/tag/许可证/传递闭包以及本地
CDK CLI的完整性锁和打包许可证通知。审计要求先运行Lambda构建，不能仅用宿主
安装包代表目标wheel。共享与 REQUEST 产物分别接受依赖审计；REQUEST 的目标
Linux x86_64 原生扩展、Botocore 数据与 jsonschema 资源通过
[Finch 验证流程](deploy.md#本地依赖打包和模板检查)复核，容器成功仍不代表
Lambda 平台 Init 或真实 Gateway 链路已经验收。

### REQUEST 独立包

REQUEST 独立包使用同一目标 wheel 锁裁剪依赖和业务模块，不更新依赖版本：

```sh
.venv/bin/python scripts/build_lambda.py --operation request --output .artifacts/request.zip
.venv/bin/python scripts/build_lambda.py --operation request --output .artifacts/request.zip --offline
.venv/bin/python scripts/audit_dependencies.py --operation request
```

两种审计默认分别写入 `.artifacts/dependency-closure.json` 与 `.artifacts/request-dependency-closure.json`。
独立包的配置、重新 synth 与回退步骤见[部署手册](deploy.md#本地依赖打包和模板检查)。

独立包保留完整 Botocore 数据和 REQUEST 所需认证、配置、授权及签名依赖；
移除检索、模型和后台索引模块及不使用的高层 SDK、分词和向量依赖。

## 本地初始化基准

本地性能复测在同一个工作树使用当前解释器和锁定依赖运行：

```sh
.venv/bin/python scripts/benchmark_dictionary.py --output .artifacts/dictionary-benchmark.json
.venv/bin/python scripts/benchmark_request_initialization.py --output .artifacts/request-initialization-benchmark.json
```

词典基准从固定输入独立重建旧格式，执行 20 组 ABBA，校验完整词频和 total，
并要求当前解码中位数不高于旧格式的 85%；计时包含字典恢复，内存另行测量。
如需历史实现比较，须自行提供经授权保留的兼容基线提交；公开样例不提供该历史基线。
这是可选检查，不是前述两个当前版本基准的前提：

```sh
.venv/bin/python scripts/benchmark_runtime_initialization.py --baseline-ref "${SEMSEARCH_BASELINE_REF:?Set an available compatible baseline revision}" --repeats 20 --output .artifacts/runtime-benchmark.json
```

历史初始化基准 `benchmark_runtime_initialization.py` 读取指定提交的 runtime
源码，与当前实现交替启动独立进程，调用真实
REQUEST 工厂；两边使用相同依赖，SDK 配置读取由已签名的内存响应代替，网络被禁用。
`--baseline-ref` 必须显式提供，拒绝与当前 runtime 源码相同的基线；基线须导入
Boto3/s3transfer，当前实现须不导入它们，两侧配置请求必须恰好一次，否则非零退出。
该基准保留 Boto3→Botocore 的历史比较，不用于新入口或裁包收益归因。
`benchmark_request_initialization.py` 直接测量部署入口，默认分别在正常导入和强制
源码编译条件下运行 20 对按需/预置并发探针，记录模块导入、Init factory 和首次
调用剩余 factory 的耗时，并核对 PC Init 的 discovery/JWKS 离线读取及预取结果。
每个探针还确认预取在同一进程中使用独立 HTTP 客户端，HTTP 边界由离线响应替代，
网络禁止守卫持续生效；它不通过替换生产预取函数模拟成功。
它使用本地锁定依赖与离线响应，不能代替 Lambda 平台
Init 或端到端延迟测量。
这个脚本验证观测前提，不以性能阈值宣称收益；负收益样本也保留。
它同时报告普通导入和强制源码编译两种条件；后者只模拟缺少 pyc，不模拟 Lambda
文件系统、CPU 或网络。输出保存在忽略目录，线上收益仍须部署后按逐请求链路验证。

## 专项验证

SDK 兼容层的专项回归会用合成 AWS HTTP 响应经过真实 botocore parser，验证字段
保留、请求绑定和目录准入；不会访问 AWS：

```sh
.venv/bin/python -m pytest tests/test_sdk_response_adapter.py tests/test_service_catalog.py -q
```

真实集成测试标记为 `integration`，需要环境开关和 pytest 选项同时开启：

```sh
SEMANTIC_CORE_RUN_INTEGRATION=1 .venv/bin/python -m pytest --run-integration -m integration -ra
```

真实集成保留显式双开关；未提供已批准 fixture 时不会自动执行。合成 Gateway fixture 不证明目标账号的 CUSTOM_JWT、签名 alias、网络、模型质量或吞吐。

行为变异检查在独立进程内替换实现，不改写工作树源码。以下第一条应通过 4 项测试；
后两条故意启用缺陷，各应失败 2 项测试并返回 1，用于证明断言能捕获真实行为错误：

```sh
.venv/bin/python scripts/run_mutation_checks.py
.venv/bin/python scripts/run_mutation_checks.py --mutation late-child-mask
.venv/bin/python scripts/run_mutation_checks.py --mutation acl-average-length
```

## 依赖锁更新

只有在依赖变更获批后才重新解析；日常构建使用已经提交的锁文件：

```sh
uv --no-config pip compile pyproject.toml --extra dev --python-version 3.12 --universal --generate-hashes --default-index https://pypi.org/simple --keyring-provider disabled --cache-dir .uv-cache --no-python-downloads --output-file requirements.lock
```

修改 setuptools 时，须同步 `pyproject.toml` 的 build-system/runtime pin，以及
`requirements-build.lock` 中从完整锁复制的 setuptools 条目和 hashes。

## 运行离线示例

[examples/offline_search.py](../examples/offline_search.py) 使用三个人工工具描述符、
确定性 mock embedding 和本地快照，演示全量 catalog 建索引、保存并校验加载、
授权 mask、BM25 与向量的 RRF 融合。默认使用临时目录并自动清理；标准输出只有
两项已授权工具的描述符，不输出被拒绝的第三项、内部标识或分数。示例不访问网络。

```sh
PYTHONPATH=src .venv/bin/python examples/offline_search.py
```

可用 `--query` 替换查询。要保留快照，显式指定一个尚不存在、父目录已存在的位置；
保存接口拒绝覆盖已有目录：

```sh
PYTHONPATH=src .venv/bin/python examples/offline_search.py --query "查询订单状态" --output .local-snapshot
```

`ToolRecord.from_descriptor(...)` 接收原始描述符并保存为不可变 JSON，
`descriptor` 属性返回副本。调用方必须提供来自可信 catalog 流程、经过内容审核的
完整 Gateway 目录；schema 校验和不可变存储不能判断描述是否真实，也不能授予权限。
`IndexSnapshot.build(...)` 对完整目录建立 BM25，向量须同时提供匹配的
`ModelProfile`。`save_snapshot(...)` / `load_snapshot(...)` 校验本地格式版本、
schema、大小、SHA-256、目录身份、模型配置和索引统计；这些检查证明文件相互一致，
可信 manifest 指针、发布授权和远程存储身份由 `SnapshotStore` 校验。加载 BM25 时恢复持久化的
TF、文档长度、DF、IDF 和平均长度，并验证统计一致性，不重新分词全量 catalog。
原文与 TF 的对应关系由构建过程及可信 manifest 的摘要绑定；本地摘要无法认证攻击者
同时替换原文、统计及 manifest 的完整重写。

`SearchEngine.search(query, allowed_ids, context, reauthorize=...)` 接收可信调用方
传入的 `frozenset` 工具身份集合。`RequestContext` 的 deadline 使用
`time.monotonic()` 的绝对值。`reauthorize` 回调在候选外发前及返回前重新读取授权，
权限变化只收紧初始集合；读取失败拒绝请求。示例回调返回固定测试集合；服务层使用强一致控制记录、不可变策略和安全目录提供真实授权来源。空授权集合不会调用 provider；rerank 默认关闭，失败或超预算
保留实际基础排序（默认 RRF），向量不可用时返回授权 BM25。公共描述符可通过
`[hit.tool.descriptor for hit in result.hits]` 取得。SearchHit 不再包含 score，
避免默认序列化泄漏全量 catalog 的词法统计。
原 `BM25Index.score` 已改为私有 `_score`；低层索引及其统计仅在可信核心内部使用，
服务边界只返回已授权工具描述符。
实际排序模式、拒答标记和诊断回调见[检索与降级规则](design.md#52-召回基础排序和-rerank)。

## Adapter 配置边界

`OpenAIEmbeddingProvider` 和 `OpenAIChatProvider` 位于
[openai_compatible.py](../src/semantic_core/openai_compatible.py)。两者复用同一传输层，
分别实现原 `EmbeddingProvider` 和 `LLMProvider` 协议。切换服务只需替换配置；
模型向量空间改变时，query 和 document 必须同步切换并重建绑定的索引。

| 配置对象 | 配置项 |
|---|---|
| `OpenAICompatibleConfig` | `base_url`、`allowed_hosts`、`timeout_seconds`、`max_response_bytes`、`auth_header_name`、`auth_header_format`、模型回显策略 |
| `EmbeddingConfig` | `profile` 中的 model/revision/dimension/config_version，`path`、`max_batch`、`max_text_bytes`、`send_dimensions`、`encoding_format`、`extra_body` |
| `ChatConfig` | `profile`、`path`、`max_text_bytes`、`max_tokens`、`temperature`、`json_mode`、`extra_body` |
| `RerankConfig` / `RerankCodec` | 模型、路径、native/flat 格式、分数字段、候选数、query/文档/总字节预算 |

`base_url` 可含 HTTPS 端口和路径前缀，`path` 追加到该前缀；例如
`https://models.example:8443/tenant` 加 `/v1/embeddings`。路径不能替换 origin，
不能含 query/fragment 或目录跳转；host 必须精确列入 allowlist，重定向不跟随。
所有文本上限明确按 UTF-8 字节计，不冒充模型 token 上限；超预算在发请求前拒绝。
Embedding 的 `max_batch` 控制单次调用上限，adapter 不自动拆批或截断输入；
chat 的协议一次接收一个描述，生成条数仍受本地 schema 的 1–6 条约束。

传输代码可以复用，**embedding 与 chat 必须分别配置超时预算**。
通用 `HTTPConfig.timeout_seconds` 默认 `1.0` 秒，直接构造的
`OpenAICompatibleConfig`、`BailianConfig` 会继承它，使用默认传输的低层 rerank
调用也受影响。服务层独立设置 query embedding 1.0 秒、rerank 0.45 秒、
document/chat 各 30 秒。通用默认值不能直接用于最多生成 2048 token 的 chat。
每次 HTTP 尝试的有效预算取
`min(timeout_seconds, context.remaining())`，仅延长 `RequestContext.deadline`
无法突破单次传输上限。离线示例通过
`chat_transport = dataclasses.replace(profile.transport, timeout_seconds=30.0)`
创建独立配置，并给 chat 设置从调用开始计的 65 秒总 deadline，覆盖初次生成和最多一次
schema 修复尝试及本地处理；embedding 保留原来的短超时。
30/65 秒仅为示例预算，真实值须按模型输出上限与实测延迟配置，不保证生产服务可用性。
该 chat 示例只替换自己的传输配置；离线 Enhancer 消费 LLM 协议。

代理路径、已验证协议及探针范围见[模型接入](design.md#62-客户代理默认接入与可选百炼直连预设)；离线示例的低维 mock 配置只用于演示，不是部署维度。

运行时认证由 Secrets Manager 提供，核心库仍由调用方注入：可以使用已配置的 `httpx.Client`，也可以设置
`auth_header_name="Authorization"`、`auth_header_format="Bearer {token}"`，
并在 provider 构造时显式传入 `credential`。自定义 `api-key` 可用
`auth_header_format="{token}"`。配置中只保存头名称与格式，adapter 不读取环境变量、
key 文件或 Secrets。示例只使用固定的测试字符串；实际部署按服务隔离 client/认证；创建 HTTP client 时使用 `trust_env=False`，
不会隐式采用环境代理。

通用响应解析忽略未知字段，`response_model_aliases` 默认空 tuple，
`validate_model_echo` 默认开启。只允许精确模型名或显式完整别名；可在已验证回显契约后
显式关闭检查，不使用前缀猜测。模型回显和未知 `revision/config_version` 扩展不会
替换返回的本地 `ModelProfile`。`extra_body` 在内部保存有限 JSON 的不可变快照，
读回数组和对象时返回新的 list/dict 副本，与 JSON 输入和请求体保持相等；
修改原输入或读回的嵌套副本不影响后续请求。它不能覆盖 model、输入、维度、
messages、流式或工具调用等受控字段。

百炼直连由 `bailian_embedding_config(...)`、`bailian_chat_config(...)`、
`bailian_rerank_config(...)` 提供可选预设，不用于客户代理的默认路由；
只有直连 chat 预设发送 `enable_thinking=False`。
`send_dimensions=False`、`encoding_format=None`、`json_mode=False` 可分别省略对应字段。
原 [bailian.py](../src/semantic_core/bailian.py) 保留兼容工厂名，返回通用 provider，
不再保留独立的百炼解析实现。`BailianConfig` 保留旧的 origin/443 限制和 version
回显检查，以保持既有百炼测试及调用用法；新服务使用 `OpenAICompatibleConfig`。

Usage 仅保留五种已知非负整数 token 计数；未知字段及 token-detail 子对象被忽略，
已知计数字段类型非法仍拒绝响应。Chat 成功结果按字段累计初次尝试和 schema
修复尝试返回的计数；缺失的计数不会由其他字段推算。Rerank 接口要求 `top_n == len(documents)`，
返回全部候选的一一对应分数，供核心保持完整候选排序；该参数不是任意截断开关。
`HTTPRerankProvider` 位于 [rerank.py](../src/semantic_core/rerank.py)，支持百炼原生
`output.results` 和常见 Jina/Cohere 风格的扁平 `/v1/rerank`，`score_field` 显式选择
`relevance_score` 或 `score`。它不是 OpenAI 标准接口，也不假设所有服务扩展都一致。
两个百炼直连 rerank URL 保留为尚未真实探测的兼容预设。直接预设要求 transport
使用无路径的 origin，防止与代理 `/v1` 前缀混用；保留字段大小写变体和多重编码
目录跳转均被拒绝。
代理 rerank 的已测路由与预算边界见上述模型接入章节。

[provider_profiles.py](../examples/provider_profiles.py) 默认使用 `proxy`，
另保留 `bailian` 直连兼容示例、`openai-compatible` 和自托管 `vllm` 配置。
`select_profile("proxy", proxy_transport=...)` 要求显式传入地址/allowlist 配置；
CLI 的 `run()` 为离线演示提供固定的 `.invalid` fixture 配置。
示例中的未验证路由标记只描述该离线profile，不覆盖模型接入章节已测的代理配置。
所有调用使用 `MockTransport`，不查询 `/models` 或访问真实推理端点：

```sh
PYTHONPATH=src .venv/bin/python examples/provider_profiles.py
PYTHONPATH=src .venv/bin/python examples/provider_profiles.py --profile bailian
PYTHONPATH=src .venv/bin/python examples/provider_profiles.py --profile openai-compatible
PYTHONPATH=src .venv/bin/python examples/provider_profiles.py --profile vllm
```

## 合成评测与离线基线

这些是**合成回归数据及本地实验**，不构成[设计验收要求](design.md#10-测试和评测策略)中的完整质量验收。
人工 holdout、rerank 启用批准及 Gateway 端到端负载仍待后续完成。
评测只使用标准库和已有锁定包。

[data/synthetic-v1.json](../data/synthetic-v1.json) 包含 500 个工具，分布于
5 个 Gateway、5 类业务系统，每个 Gateway 100 个相似 CRUD 工具。
340 条查询中中文 170、英文 85、混合 85；授权比例为 0%/1%/10%/100%，
相关授权数量覆盖 0、1、2、3、4、5、10、20。`relevant_ids` 是同 Gateway 内
语义相关集合，评测时与 `allowed_ids` 求交。示例由确定性
MockLLMProvider 每工具生成 3 中/3 英；评测查询采用独立规则，不复用这些示例。
这是模板合成数据，不是人工标注集，也没有可用于调参验收的独立 holdout。

在当前工作树的 `.venv` 中运行：

```sh
PYTHONPATH=src .venv/bin/python scripts/generate_eval_data.py --seed 20261010
PYTHONPATH=src .venv/bin/python scripts/run_evaluation.py --output-dir .artifacts/offline-current
```

第一条命令逐字节重建已提交数据；第二条默认不读取凭证、不访问网络，执行
BM25、纯向量、RRF、RRF+rerank 各自有/无示例的八组消融，以及
20/50/100/200/500 工具数实验，每档 10 条查询。可用 `--queries-per-size 50`
增加每档查询数（上限 50）。省略 `--output-dir` 时也会写入仓库根目录下忽略的
`.artifacts/offline-current/`，不会覆盖已提交的冻结基线；显式提供 `--output-dir`
仍可选择其他目录。
`--query-limit` 用于按语言、权限、相关数量分层轮转抽样；
`--bootstrap-samples` 控制 bootstrap 次数，默认 1,000 次和固定 seed。

输出均为 JSON + Markdown，以上命令将当前结果放在忽略的
`.artifacts/offline-current/`。仓库保留历史合成报告的 Markdown 摘要；原始 JSON/JSONL 不随样例分发。摘要使用旧配置，不是当前默认配置的验证结果。新实验的原始输出保存在你指定的本地目录。

- [迁移前检索离线基线](../reports/offline-regression.md)（原始 JSON 不随样例发布）：
  主评测分组的分母、>5 理论上限、空集误推荐率、语言/权限/相关数量分组、
  每组样本数和比例、Recall@5、Hit@5、MRR@5、p50/p95 及 95% CI。
- [迁移前工具数实验](../reports/offline-knee.md)（原始 JSON 不随样例发布）：
  全量加载与先 RRF 搜索再选择的准确率、token、延迟及 CI。每次只做工具选择，
  不执行业务工具；固定使用原始查询加 namespace 前缀搜索，不测 Agent 自主改写查询。

拐点实验把合成目录映射为一个受控 Gateway，查询与工具描述均明确来源 namespace，
避免跨目录的同义 CRUD 工具使答案含糊。每条查询使用包含正确工具的嵌套候选目录，
两条路径配对使用相同查询、目录和模型。这里只取授权相关单工具任务；结果不能推断
多工具规划或拒绝能力。索引构建不计入选择延迟，搜索路径的检索延迟计入。
模型拒绝过多工具、超时和错误均保留在分母，不截断目录以绕过模型限制。

`run_evaluation.py` 的检索计时包括查询 provider 调用；文档向量可以跨消融缓存，
查询向量不缓存。另行调优的 `run_tuning.py` 检索臂复用同次 query embedding 耗时，
报告中的检索延迟为 reconstructed，并非每臂独立端到端实测；调优拐点实验则每个搜索臂
重新调用 query embedding。口径见[调优说明](../reports/tuning/README.md)。
JSON 保留实际执行模式、逐查询结果和失败，不能把降级 BM25 当作成功的 rerank。
区间是固定合成样本条件下的 query bootstrap；小样本或恒定结果会有退化区间。
mock embedding 是内容哈希，mock selector 是复用 MockLLMProvider 的词法 fixture，
其分数不能用来选择生产模型或证明工具数拐点。mock token 为序列化请求
UTF-8 字节数/4 的估算，单独列出；真实 usage 缺失时保留 N/A，不推算为零。
延迟依赖本机负载，不要求逐字节复现，也不代表 Gateway 冷启动/刷新或生产 SLO。

## 可重复运行的真实评测

真实模式必须同时提供环境开关和 `--mode real`。以下变量选择 AWS CLI profile、区域和
Secrets Manager secret；它们是非秘密定位信息，不能填写 API key：

| 变量 | 默认值 |
|---|---|
| `SEMANTIC_EVAL_AWS_PROFILE` | `sample-semantic-eval` |
| `SEMANTIC_EVAL_AWS_REGION` | `cn-north-1` |
| `SEMANTIC_EVAL_SECRET_ID` | `semsearch/model-api` |

先配置你自己的 profile，并设置这些变量。程序先执行该 profile 的 STS 检查，再读取指定
secret（JSON 字段 `base_url`、`api_key`）；普通前置条件失败写跳过报告，凭证过期则停止。
key 只保留在进程内存，不接收环境变量 key、文件 key 或其他凭证来源。配置的确切 host
必须与 secret 中的 HTTPS base URL 匹配；下例 host 是占位符。

固定标记 `ExpiredToken`（含 `ExpiredTokenException`）、`RequestExpired` 和
`TokenRefreshRequired` 均分类为 `CredentialsExpired`；仅在内存中检查 stderr，
不把原始输出写入报告或日志。`run_tuning.py` 对此返回退出码 `20` 并记录停止状态，
等待实际刷新指定凭证后才可继续；`run_evaluation.py` 写停止报告后抛出该异常，
不会伪装成成功或普通跳过。

```sh
SEMANTIC_EVAL_REAL=1 PYTHONPATH=src .venv/bin/python scripts/run_evaluation.py \
  --mode real --allow-host approved-model-host.example \
  --embedding-model text-embedding-v4 --embedding-dimension 1024 \
  --chat-model qwen-plus --query-limit 100 --queries-per-size 10 \
  --output-dir .local/real-reports
```

base URL 的路径前缀由 transport 保留，默认追加 `/embeddings` 和
`/chat/completions`。模型、revision、维度、路径、超时、批量大小和 chat 请求/输出
预算均可配置，见`--help`；`--embedding-batch-size`默认10，与当前代理已测上限一致。
默认真实评测取固定的 100 条分层查询，执行六组
无 rerank 消融；示例仍为同一份已版本化的离线 mock 示例，不生成新的真实 LLM 示例。
真实rerank需另加显式开关；对于采用`/v1` base 且支持下列路由的服务，可配置
`--enable-rerank --rerank-path /rerank --rerank-score-field relevance_score`，
模型默认`qwen3-rerank`，CLI使用flat codec。其它endpoint须提供各自已验证路由，
不能根据模型名称猜测或重复拼接`/v1`。
该实验开关不改变核心的生产默认值，也不替代 rerank 启用批准。

每次运行在读取凭证前按当前 batch 10 保守预估 embedding、rerank 和 chat
全部调用数，再在每次请求前执行硬限制（`--max-model-calls` 默认 2,000），
不自动重试。默认 500 工具、100 条检索查询和每档 10 条拐点查询预计最多
900 次 embedding + 100 次 chat = 1,000 次；启用 rerank 则为 1,100 次 embedding
+ 200 次 rerank + 100 次 chat = 1,400 次。1000/1400 均为离线预检上界，
不是实际执行调用量。改用全部 340 条查询时，无 rerank 为 1,960 次；
启用 rerank 为 3,320 次，会在读取凭证前被默认 2,000 次上限拒绝，需减小查询数
或单独获批调整预算。五档 chat 最多各 50 条配对查询，即每档最多 100 次 chat。
报告分别保留 provider usage 和 mock 估算；估费应使用所配置服务的实际报价，
不能把公开模型标价视为代理报价。原始基线运行因指定STS profile不可用而跳过真实
模式；后续独立模型探针不是该评测的真实质量结果。
