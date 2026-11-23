# 运维操作手册

本手册帮助值班人员维护 Gateway 工具搜索的可用性、索引更新和权限边界。服务通过搜索减少 Agent 加载的工具数量，所有故障处理都应保留当前授权与撤销状态；整体目标见[项目总览](overview.md)。本文适合负责部署、凭证维护、告警响应和恢复的运维人员。读完后，你可以按现象定位故障、确认恢复结果，并按既有部署流程执行变更或清理。

## 先认识运行对象

REQUEST 拦截器在 Gateway 转发前验证身份、权限和调用频率；Search 执行检索并返回当前授权工具。Indexer 定时读取完整工具目录并发布基础索引；Enhancer 为目录补充向量及经批准的例句。索引快照是一次发布所需的完整索引文件集合，保存在 S3；运行实例校验后才使用。

每个 Gateway 有两张 DynamoDB 表。state 表的 `CONTROL` 保存当前内容指针；protected 表的 `PROTECTED` 保存授权指针和永久删除状态。后者必须保留最新值，不能随着代码或内容回滚。组件和数据边界见[架构说明](architecture.md)与[安全与数据流](security-and-data-flow.md)。

Lambda alias 是指向某个已发布函数版本的别名，本项目使用 `live`；切换别名可以切换入口使用的版本。CDK 是本仓库生成基础设施模板的工具，CloudFormation 按模板管理资源；change set 是执行前可检查的资源变更清单。IdP 是客户的身份服务，后续 OIDC 和 JWKS 诊断会检查它提供的身份配置与验签公钥。

本页中的数值若标注为默认值或告警阈值，均来自代码配置，不是容量或性能实测。实测数据集中在[已知限制](known-limitations.md)，避免在多个手册中维护互相冲突的数字。

`ecs_search` 可选且默认关闭；启用时 Gateway 调用 facade Lambda，经 internal NLB
到 ECS Search，并按规定条件回退到独立 Lambda。

## 我要部署或回滚服务

### 1. 确认本次变更的输入

记录目标账号、区域、Gateway、已审核代码包、配置对象版本和实际两表名称。支持区域为 `cn-north-1` 与 `cn-northwest-1`；具体资源边界见[部署前置条件](deploy.md#前置条件)。已有共享表布局的环境必须先按[迁移流程](deploy.md#已有同表数据迁移)处理，不能直接切换到新表名。

后续部署本系列预算改动前，按既有部署 Search 和 Enhancer 的配置引用读取各自的
固定版本 S3 配置，确认 `MODEL_EMBEDDING_TIMEOUT_SECONDS` 为 `(0, 6]` 内的
有限秒数，并核对 Search 的 `SEMSEARCH_SEARCH_TIMEOUT_SECONDS` 为 `(0.3, 6]`
内有限秒数。新代码默认分别为 `1.0` 和 `3.0` 秒；固定 S3 显式 `0.45`/`1.5` 秒
仍覆盖它们，只有 S3 和环境变量都省略相应字段时才使用新默认。
超界或无效时，先核实单位并改为正确秒数，
发布并引用新的不可变配置版本，再部署代码。核对必须使用既有部署引用的对象版本，
不能用本地示例或 S3 当前版本替代；本次本地修订未执行这项既有部署核对。
当前源码默认和各已部署函数的实际配置须分别核对；归档部署与采样不能代替下一次
发布的资产/配置检查，见[部署前置条件](deploy.md#前置条件)。

### 2. 按部署手册准备并执行变更

1. 执行[本地构建和模板检查](deploy.md#本地依赖打包和模板检查)，取得可复用的 Lambda 包、CDK assembly 和配置清单。
2. 按[部署顺序](deploy.md#获授权后的部署顺序)上传不可变配置并记录实际 `VersionId`，准备和审核 change set，再由获授权变更者执行。
3. 按[事后验证清单](deploy.md#事后验证清单)核对 alias、权限、身份配置、索引、普通 Agent 搜索和后台轮询，最后恢复计划内流量。

首次部署默认 `models_enabled=false`、`schedules_enabled=false`。这表示先检查基础配置和 BM25 路径，再分别启用模型和调度；默认关闭来自[配置示例](../infra/config.example.json)。不要因栈已创建就认定服务已就绪。

已有 dev 后续更新必须以新读取的 Original 和完整资源清单逐项比较新 synth，
检查 REQUEST alias/PC、四个 request-init 监控及 alias 依赖，以及 ECS 网络、
服务、IAM 与监控资源。新 ZIP 和 Version 引用的差异须解释，
资源总数相同不等于配置相同。

升级时还须单独处理以下差异：

- 原始 synth 为新建 ECS 服务声明 `DesiredCount=2`。已有服务可能已扩容到 3/4，
  应按[ECS 容量保留流程](ecs-search-operations.md#离线部署预检与完整模板保护)
  从升级候选移除该属性，并核对实时容量，避免更新时写回 2。
- 新源码构建会生成新的 REQUEST Version。保留 Init 行为和 PC=1，不等于
  部署后数字版本与受审资产一致；核对新代码、alias 目标、旧 Version 保留和 PC READY。
- 历史模板中的已解析值恢复为 `Ref`/`Fn::Join` 时，按新读取的栈参数与固定 S3
  bucket/key/VersionId 核对。Original 的参数定义不证明实际参数值，不能默认为等值。
- 收口离线比较发现 20 条旧告警增加 `AlarmActions`。这是通知配置变更，须逐项审核；
  SNS 无订阅和通知未送达验收仍是独立缺口，不能因增加 action 就标为通知可用。

### 3. 需要回滚时先区分代码与内容

按[回退和保留数据](deploy.md#回退和保留数据)停止受影响入口及写入，排空在途操作，记录当前 alias 和一致性控制视图。代码只可回退到支持两个独立表绑定的兼容版本。相同目录版本的内容恢复须条件发布；跨目录版本应使用当前完整目录重建，不能把历史 `CONTROL` 或 `PROTECTED` 直接覆盖在线数据。

### 看到什么算成功

四个 `live` alias 指向已验证版本，配置引用与预期摘要一致；搜索成功，已撤销主体继续被拒绝；Indexer 出现 `published` 或 `reconciled`，对应 `poll-success` 恢复。若模型尚未启用，Enhancer 的 `disabled` 是预期状态。

### 出错怎么办

无法证明当前授权和永久删除状态完整时，保持拒绝服务。不要通过删除 stack、重新初始化表、回读旧授权或无条件写表恢复流量。

## 我要核对或修改配置

### 1. 从部署清单确认实际资源

模板输出包括 `IndexBucketName`、`StateTableName`、`ProtectedTableName`，以及 `SearchAliasArn`、`RequestAliasArn`、`IndexerAliasArn`、`EnhancerAliasArn`。不要根据旧环境名称猜测当前资源。输出定义见 [stack.py](../infra/stack.py)。

资源前缀是 `semsearch-{stage}-{deployment_name}`。函数名为 `<prefix>-search`、`<prefix>-request`、`<prefix>-indexer`、`<prefix>-enhancer`；各自日志组为 `<prefix>-<operation>-logs`。下文中的 `<prefix>` 要替换为本次部署值，来源见[配置对象](../infra/config.py)和[函数资源](../infra/functions.py)。

### 2. 确认配置属于哪一层

| 配置类别 | 常用字段 | 核对重点与完整参考 |
|---|---|---|
| 资源身份 | `SEMSEARCH_ACCOUNT`、`SEMSEARCH_REGION`、`SEMSEARCH_GATEWAY_ID`、`SEMSEARCH_SEARCH_TARGET_ID`、`SEMSEARCH_SEARCH_TOOL_NAME`、`SEMSEARCH_SEARCH_INCARNATION` | 与真实 Gateway、target、工具名称及注册身份一致；见[配置与入口](service.md#配置与入口) |
| 存储绑定 | `SEMSEARCH_BUCKET`、`SEMSEARCH_TABLE`、`SEMSEARCH_PROTECTED_TABLE` | 两个表名必须不同；protected 表保持当前授权权威 |
| 配置版本引用 | `SEMSEARCH_CONFIG_BUCKET`、`SEMSEARCH_CONFIG_KEY`、`SEMSEARCH_CONFIG_VERSION_ID`、`SEMSEARCH_CONFIG_SHA256`、`SEMSEARCH_CONFIG_BYTES` | 五项必须同时提供，必须匹配同一个不可变对象版本；见[版本化 S3 配置](service.md#版本化-s3-配置) |
| 身份验证 | `OIDC_ISSUER`、`OIDC_AUDIENCE`、`OIDC_DISCOVERY_URI`、`OIDC_JWKS_URI`、`OIDC_ALLOWED_HOSTS`、`OIDC_ALGORITHMS`、`OIDC_TOKEN_USE_CLAIM`、`OIDC_TOKEN_USE` | 精确匹配签发方、受众、用途、公网地址和主机允许清单；见[身份和授权](service.md#身份和授权) |
| 后台目录身份 | `OIDC_TOKEN_ENDPOINT`、`OIDC_MACHINE_SCOPES`、`SEMSEARCH_MACHINE_PRINCIPALS_JSON`、`SEMSEARCH_OAUTH_SECRET_ID` | 只授予后台协议初始化和列目录权限，不能作为 Agent 调用身份 |
| 检索行为 | `SEMSEARCH_MODELS_ENABLED`、`SEMSEARCH_FUSION_STRATEGY`、`SEMSEARCH_TOP_K`、`SEMSEARCH_MIN_VECTOR_SIMILARITY`、`SEMSEARCH_MIN_VECTOR_MARGIN`、`SEMSEARCH_SEARCH_TIMEOUT_SECONDS`、`MODEL_EMBEDDING_TIMEOUT_SECONDS`、`MODEL_RERANK_ENABLED` | 默认向量/BM25 回退策略、结果数 5、rerank 关闭、拒答门槛省略；Search 总预算默认 3.0 秒、embedding 子预算默认 1.0 秒；见[配置解析](../src/semantic_service/config.py) |
| 模型连接 | `MODEL_SECRET_ID`、`MODEL_BASE_URL`、`MODEL_ALLOWED_HOSTS`、`MODEL_EMBEDDING_MODEL`、`MODEL_EMBEDDING_DIMENSIONS`、`MODEL_CONFIG_VERSION` | endpoint、模型空间及维度一致；变更空间后完整重建索引；见[Secrets 与模型](service.md#secrets-与模型) |
| 签名及撤销 | `SEMSEARCH_SIGNING_SECRET_ID`、`SEMSEARCH_REVOKED_KIDS` | 后者已有运行时支持，当前 CDK 尚未分发，不能直接作为部署配置开关使用；见[紧急撤销](#3-泄露时执行紧急撤销) |
| 目录和协议 | `SEMSEARCH_REGISTRY_JSON`、`SEMSEARCH_GATEWAY_URL`、`SEMSEARCH_GATEWAY_ALLOWED_HOSTS`、`SEMSEARCH_MCP_VERSIONS` | 注册实际 target 身份，默认协商版本 `2025-03-26`；见[目录与调度](service.md#目录与调度) |
| 主体限流 | `SEMSEARCH_SUBJECT_CALLS_PER_MINUTE`、`SEMSEARCH_MACHINE_CALLS_PER_MINUTE` | 默认各 600 次/固定分钟；这是应用上限，不能替代账号配额 |
| 部署开关及通知 | `models_enabled`、`schedules_enabled`、`alarm_topic_arns`、`log_retention_days` | 通知目标由运维提供；默认日志保留 30 天，配置允许值见[部署配置](../infra/config.py) |

模型超时、批次、响应别名和 rerank 编解码等细项不在本页重复，按[完整模型选项表](service.md#secrets-与模型)逐项检查。API key 只从 Secrets Manager 读取，不能写入环境变量或版本化 S3 配置。

### 3. 通过新配置版本发布变更

修改非秘密配置后重新生成清单、上传并记录新对象版本，再通过 change set 更新 Lambda version 和 alias。暖实例会缓存已验证配置；只改 S3 当前版本或后台修改 secret 的 endpoint 都不能代替正式配置发布。

### 看到什么算成功

新实例读取预期对象版本且通过摘要检查，工具名称与 schema 摘要一致，运行行为符合变更目的。配置错误应显式拒绝，不应表现为成功空结果。

### 出错怎么办

检查五项配置引用是否缺失、对象是否跨账号或跨操作复用、实际字节数和摘要是否相符。不要删除其中一项以强迫回退旧环境配置。每类函数可读字段由 [deployment_config.py](../src/semantic_service/deployment_config.py)限定。

## 我要处理告警

### 1. 确认告警的范围

以下是 [monitoring.py](../infra/monitoring.py) 定义的全部默认告警。真实名称均以 `<prefix>-` 开头；表中列出其后缀。`search`、`request`、`indexer`、`enhancer` 分别表示四个函数。自定义指标使用 `semsearch/<prefix>` 命名空间，Lambda 原生指标使用 `AWS/Lambda`。

| 告警后缀 | 触发条件及含义 | 首要处置 |
|---|---|---|
| `search-lambda-errors` | 5 分钟内 `Errors > 0`，至少一次 Lambda 调用失败 | 查 Search 最终 `status`；无最终日志则查超时、内存耗尽或导入失败 |
| `request-lambda-errors` | 同上，范围是 REQUEST | 区分输入/认证/授权主动拒绝与内部故障 |
| `indexer-lambda-errors` | 同上，范围是 Indexer | 查目录读取、IdP、配置及发布失败 |
| `enhancer-lambda-errors` | 同上，范围是 Enhancer | 查模型调用、内容校验和条件发布 |
| `search-throttles` | 1 分钟内 `Throttles > 0` | 查账号可用并发、流量和 Search 实际时长 |
| `request-throttles` | 同上，范围是 REQUEST | 查入口并发和流量；不要仅扩大主体分钟限额 |
| `indexer-throttles` | 同上，范围是 Indexer | 检查上一轮是否仍运行；保持后台预留并发为 1 |
| `enhancer-throttles` | 同上，范围是 Enhancer | 检查切片时长和重叠调用；保持后台预留并发为 1 |
| `search-internal-error-rate` | 5 分钟内内部错误占已记录调用的比例大于 1% | 查 `internal failure`、`index not ready`，重点看索引与配置 |
| `request-internal-error-rate` | 同上，范围是 REQUEST | 查依赖、配置和 `auth_failure` |
| `indexer-internal-error-rate` | 同上，范围是 Indexer | 查轮询完整性、权限与存储 |
| `enhancer-internal-error-rate` | 同上，范围是 Enhancer | 查模型、快照和发布路径 |
| `indexer-async-dropped` | 1 分钟内 `AsyncEventsDropped > 0` | 查投递年龄、限流和 Lambda 异步配置；修复后等待下一轮 |
| `enhancer-async-dropped` | 同上，范围是 Enhancer | 查切片积压；不批量重放旧调度事件 |
| `poll-missing-3m` | 连续 3 个一分钟周期没有成功全量对账 | 尽快检查 Scheduler、Indexer、IdP 和 target |
| `poll-missing-10m` | 连续 10 个一分钟周期没有成功全量对账 | 升级处置，评估目录变更是否受阻 |
| `request-auth-recovered` | 5 分钟内 `Sum > 2`，即至少 3 个请求完成 OIDC 元数据补偿恢复 | 查 discovery/JWKS 反复失败及最终请求状态 |
| `request-init-auth-recovered` | 同一阈值，统计 PC Init 中最终成功的 OIDC 预取补偿恢复 | 按日志流和 Init 时间窗口查 `oidc_prefetch` 及 `phase=init` 的 JWKS 事件 |
| `request-init-oidc-prefetch-failed` | 5 分钟内 `Sum > 0`，即至少 1 次预取失败，包括顶层 `status=ready` 的情况 | 用 `failure_kind` 区分依赖、结构故障及预算耗尽；查首请求是否重新读取 |
| `search-execution-p95` | 5 分钟窗口内 Search Lambda `Duration` 的 p95 大于 2000 毫秒 | 区分首次调用、快照刷新及普通请求，再定位阶段 |

p95 表示该统计窗口内约 95% 的样本不超过该耗时。这里的 2000 毫秒是告警阈值，不是已达到的性能；Lambda `Duration` 不包含完整 Gateway/网络耗时，也不等于客户端端到端耗时。

### 2. 用日志分类，避免误判

原生 `Errors` 包含函数主动抛出的输入、认证和授权拒绝。内部错误率只统计 `internal failure` 与 `index not ready`，分母是有最终结构化日志的调用，不能覆盖日志缺失的进程失败。`poll-success` 是轮询心跳，不直接度量工具变更到可搜索的耗时。

除两项轮询缺失告警外，默认将需要补入的缺失点按不超阈值处理。已有真实超阈值点仍可能参与评估，因此没有新请求或等待一段时间不保证立即恢复 OK。计划内关闭 schedule 可能触发 `poll-missing-3m` 和 `poll-missing-10m`，应结合变更窗口判断。若部署配置 `alarm_topic_arns` 为空，告警会改变状态，但本栈不创建通知主题或订阅。

### 3. 验证恢复

检查触发原因对应的成功日志和指标，再确认告警恢复。对于 `request-auth-recovered`，恢复只说明 discovery/JWKS 补偿请求成功，随后仍可能因令牌或权限被拒绝；不要把该次数当作成功请求数。`request-init-auth-recovered` 是独立的 Init 计数，阈值同为 300 秒周期 `Sum > 2`；预取失败由 `request-init-oidc-prefetch-failed` 检出。两项均为一个评估周期、缺失数据不告警，并沿用现有通知目标。

### 出错怎么办

告警持续时，按以下故障章节定位。不要通过删除过滤器、扩大阈值或放宽授权来消除告警。需要调整指标或告警逻辑时，走[开发手册](developer-guide.md)中的测试与评审流程。

## 我要排查认证失败或 Gateway 403

OIDC discovery 提供签发方及公钥地址，JWKS 提供验证令牌签名的公钥。预置并发 Init 预取、首次调用或缓存刷新时可能需要访问这两个端点；请求期看同次调用的阶段日志，Init 单独看预取记录。Gateway 自己也执行 `CUSTOM_JWT` 验证，所以 HTTP 403 不一定来自 Lambda。

### 1. 先判断失败发生在哪一段

按客户端报错时间检查 REQUEST 日志。若有对应记录，查看 `status`、`auth_failure`、`auth_recovered` 和 `stage_timings`；若没有，再检查 Gateway 认证配置、入口地址及 Gateway 到拦截器 alias 的调用权限。日志缺失也可能是平台或日志写入故障，不能单凭“没看到日志”作结论。

JWKS 独立逐次事件与 PC Init 记录按实际部署版本核对。用同一
`aws_request_id` 筛选请求期 `operation=oidc_fetch`、`event=attempt_finished`，
按 `attempt` 查看 `compensation`、`duration_ms`、`outcome` 和受控 `failure`。
每次实际 GET 结束即记录，首错先于补偿等待；耗时不含等待或写日志。
`outcome=ok` 只表示读取及 JSON 对象解析完成，仍需看最终认证和请求结果。
字段与边界见[逐次读取事件](service.md#jwks-逐次读取事件)；历史摘要不能补出这些字段，
缓存命中和 discovery 读取也不会生成此类事件。
Init 事件带 `phase=init`，`aws_request_id` 固定为 `null`；按同一 `@logStream`
及 Init 时间窗口关联预取摘要与 `INIT_REPORT`，不用空 UUID 关联多个环境。

### 2. 按现象检查

| 现象 | 检查内容 | 修复方向 |
|---|---|---|
| `unauthenticated`，没有 `auth_failure` | 访问令牌签名、`iss`/`aud`、`iat`/`exp`、用途及内部签名绑定 | 修复客户端取令牌或身份配置；不把它自动归为 IdP 网络故障 |
| `auth_failure.category=http` | `endpoint` 和 `http_status` | 核对 IdP 可用性和正确公开地址；不要记录响应正文 |
| `auth_failure.category=timeout` 或 `network` | `reason`、可选 `cause`，以及 discovery/JWKS 阶段 | 排查 DNS、TLS、连接及延迟；没有 `cause` 不表示已排除网络问题 |
| `auth_failure.category=document` | discovery 是否匹配固定 issuer/JWKS、公钥结构是否有效 | 修复身份服务文档或批准配置；完整原因枚举见[诊断说明](service.md#冷启动分段计时) |
| `auth_recovered=true` | 同次请求最终 `status`，相关阶段的 `count`、`errors`；部署逐次事件后再查 JWKS `failure` | 说明补偿读取成功；继续查为何需要补偿以及后续是否被拒绝 |
| `oidc_prefetch.auth_recovered=true` | 同一日志流和 Init 窗口的预取摘要及 `phase=init` 事件 | 表示预取补偿成功且缓存就绪；首请求仍须看缓存是否过期 |
| `oidc_prefetch.status=failed` | `failure_kind`、可用的 `auth_failure`、`metadata_recovered` 和首请求重新读取 | `dependency` 查 IdP 与文档，`structural` 查代码或接线，`deadline` 查预取预算；已有局部恢复不等于预取成功 |
| `unauthorized` | 当前策略 `disabled`、`not_before`、主体 scope/group、Search 自身权限、工具摘要 | 由授权管理员修复策略或工具注册，不能通过重新取令牌自动解决 |
| Indexer 的 `gateway_403_count` 或 `gateway_401_count` 增长 | Gateway 认证配置、后台主体、`gateway_replay_*` 和汇总 `status` | 区分替换令牌失败、第二次请求失败和整次轮询失败 |

### 3. 检查后台令牌恢复结果

Indexer 已在有限预算内支持一次 401/403 后的替换令牌与请求重放。`gateway_replay_success_count` 增长只说明第二次 Gateway 请求成功，不保证最终索引发布成功。`gateway_auth_refresh_failure_count` 表示替换令牌获取失败；`gateway_replay_skipped_count` 表示预算或刷新准入不允许重放；`gateway_replay_failure_count` 表示第二次请求已发出但失败。八项计数及口径见[部署告警说明](deploy.md#告警与处置)。

### 看到什么算成功

有效主体完成初始化和授权操作，无效、过期及撤销主体仍被拒绝；Indexer 完整轮询达到 `published` 或 `reconciled`。若是正常 IdP 密钥轮换，还须验证旧密钥重叠窗口和新公钥传播。

### 出错怎么办

持续失败时停止反复取令牌，交由身份管理员核查 IdP 配置和限额。未知 `kid` 的刷新失败后存在默认 30 秒冷却，合法新密钥可能暂时被拒绝；30 秒是 [OIDCConfig](../src/semantic_service/auth.py) 默认值，具体边界见[已知限制](known-limitations.md)。不要增加无限重试、忽略签名或扩大主机允许清单。

## 我要排查 embedding 降级

embedding 把查询或工具描述转成向量；BM25 根据词语匹配排序。查询 embedding 超时或失败时，搜索可以退回 BM25，当前请求的语义匹配质量可能下降。日志有降级标志，公共响应仍只有工具描述符。

### 1. 确认是否发生故障

查看 Search 的 `ranking_mode`、`degraded`、`vector_availability` 和 `embedding_error`。有意关闭模型，或当前主体不允许查询外发，会使用无向量路径；这不自动构成模型故障。健康的低相似度词法兜底也不能当作超时。

### 2. 按错误类别处理

| `embedding_error.kind` | 可确认的含义 | 下一步 |
|---|---|---|
| `http_rate_limited` | 观察到 HTTP 429 | 核对代理及模型账号共同使用量、并发和配额；不能仅凭 429 认定百炼某项额度耗尽 |
| `http_client_error` | 其它 HTTP 4xx，包括常规 401/403 | 查凭证状态、模型名和协议参数；保留状态码，不抄录正文 |
| `http_server_error` | HTTP 5xx | 核对代理/模型可用性，观察持续范围 |
| `http_status_error` | 其它非成功状态，包括未跟随的重定向 | 查批准的 endpoint 和路径 |
| `connect_timeout`、`read_timeout`、`write_timeout`、`pool_timeout`、`timeout` | 连接、读取、写入、连接池或未细分的客户端超时 | 结合 `model.embedding` 阶段定位，不能只凭超时类别判断网络根因 |
| `deadline_exceeded` | 本地总预算已耗尽 | 检查之前的初始化、快照和授权耗时；不等同于上游返回超时 |
| `network_error` | 其它传输或网络错误 | 查连接、DNS、TLS 及代理路径 |
| `response_parse` | JSON、响应身份、向量或结构验证失败 | 查模型响应契约、维度及配置；不能使用未验证向量 |
| `authentication`、`rate_limited`、`invalid_request`、`unavailable`、`unknown` | provider 只提供较粗分类或未识别异常 | 核对适配器和受控诊断，不猜测上游错误正文 |

完整分类、可保留的响应头和计数口径见[embedding 错误诊断](embedding-diagnostics.md)。`http_status=200` 仍可能发生解析失败或本地期限耗尽。头缺失不能排除限流；头存在也不能证明由百炼生成。

### 3. 检查模型与索引是否匹配

核对 `MODEL_EMBEDDING_DIMENSIONS`、模型身份、配置版本和快照模型空间。
`MODEL_EMBEDDING_TIMEOUT_SECONDS` 同时控制核心子预算和 HTTP 上限，默认 1.0 秒，
允许 `(0, 6]` 内有限秒数；`SEMSEARCH_SEARCH_TIMEOUT_SECONDS` 默认 3.0 秒，
允许 `(0.3, 6]` 内有限秒数。核对固定版本 S3 配置的实际值；它覆盖同名环境变量。
显式旧值不会因代码默认值调整而改变，S3 和环境变量均省略字段时才采用新默认。
例如 800 ms 应填 `0.8` 秒，不能填 `800`。解析及传输入口分别见
[配置解析](../src/semantic_service/config.py)和[模型 provider](../src/semantic_service/models.py)。

Search 从 handler 的处理起点 `started` 计算配置期限。只有 Lambda 剩余期限分支
扣除 0.3 秒收尾余量，再与配置期限比较：

```text
provider_deadline = min(started + search_timeout, started + Lambda_remaining - 0.3)
embedding_deadline = min(provider_deadline, embedding_started + embedding_timeout)
```

缺少或无法解析 Lambda 剩余时间时沿用配置期限；异常值通过
`lambda_deadline_ignored` 标记，详见[预算与诊断边界](embedding-diagnostics.md#可配置预算与授权收尾)。
授权和快照加载消耗总余额，模型 secret 读取还会消耗子余额。逐请求比较
`embedding_timing.stage_budget_ms` 与 `attempts[].effective_timeout_ms`，确认档位是否被截断；
空 `attempts` 还可能表示自定义 provider 没有采集到 HTTP 记录，不能单独当成未发请求。
这些期限控制结果接纳与降级，不保证 HTTP 在配置时长内结束，最终授权仍须执行。

调整 endpoint、维度或模型空间必须评审并重建索引，不能只修改 secret 的地址。

### 看到什么算成功

预期启用向量的请求记录 `vector_availability=available`，无 `embedding_error`；即使保持 BM25，结果也通过当前授权校验。用实际查询核验质量，不以单次模型调用成功替代端到端验收。

### 出错怎么办

允许的降级可继续提供 BM25，但应跟踪质量和持续时间。持续 429、协议错误或超时的后续选择及代价见[模型托管选择](embedding-hosting.md)。不要在未核对配额和总预算前盲目重试或延长超时。

## 我要排查索引没有更新

### 1. 确认更新链路走到了哪一步

检查上游工具是否已发布、是否完成必要同步并处于 `READY`，再查看 Indexer 是否执行成功。Indexer 默认每 60 秒尝试一次完整对账，工作预算 45 秒；Enhancer 默认每 300 秒执行一次，切片预算 240 秒。这些是[调度](../infra/scheduling.py)和[函数配置](../infra/functions.py)的默认值，不承诺“工具变更后 60 秒内一定可搜索”。

### 2. 检查目录、发布和读取

1. **上游目录：** target 必须属于已批准注册表。MCP target 只接受可验证的显式 `DEFAULT` 或有效静态 schema；`DYNAMIC` 和无法验证的模式会拒绝。上游发布流程负责同步，Indexer 不修改 target。
2. **完整轮询：** 查看 `published`、`reconciled` 或错误状态。分页失败、目录前后不一致、超时和未知工具不会产生“部分成功”目录。
3. **发布与读取：** 核对当前 `CONTROL` 的内容和安全目录指针、S3 `VersionId` 及摘要。目录内容不变时 `reconciled` 是正常结果，不要求每轮增加 `catalog_revision`。Search 内容检查有缓存，安全或策略变化仍需同步校验。

CAS 是带条件的状态更新：只有读取时看到的版本仍有效，发布才能推进指针。发生 CAS 冲突时应废弃冲突产物，读取最新状态后交给后续正常轮询或切片；不能改成无条件写入，也不能增加后台并发。

### 3. 检查增强是否停在待审核或暂停状态

`disabled` 表示模型增强未开启。`paused` 表示当前切片未完成，需结合模型预算、429、期限和进度检查。自动生成的例句默认进入待审核缓存，不自动加入索引；新目录版本还会使旧增强进度失效。这些情况不阻止基础目录按正常流程发布。

### 看到什么算成功

`poll-success` 持续入库，最新完整目录的身份与摘要可验证，新工具经授权后能被搜索。要求启用增强时，再检查当前目录版本的增强快照已发布，不把轮询成功当作增强成功。

### 出错怎么办

检查 Scheduler 状态、alias、异步事件丢弃、IdP、IAM 和 target 状态。过期 tick 由下一轮完整重读恢复，不批量重放。详细发布一致性见[目录与调度](service.md#目录与调度)；持续轮询缺失按告警时长升级。

## 我要排查首条请求慢

冷启动是函数需要新的运行环境时，AWS 准备环境并加载代码，再执行请求。热请求复用已有运行环境。平台初始化 `Init Duration` 由 Lambda 平台报告；首次初始化 `handler.factory` 是应用创建 handler、读取配置和准备客户端的时间，两者不是同一个指标。

### 1. 用同一次请求区分平台和应用耗时

将应用日志的 `aws_request_id` 与同日志流的 START/REPORT 关联。`first_invocation=true` 只表示此 Python 进程首次进入 handler，不能单独证明平台冷启动。应用 `runtime_module_init_ms` 已包含在平台 `Init Duration` 中，不能重复相加。按需模式的 `handler.factory` 在调用内；REQUEST 专包启用预置并发后，还须查看同环境的 `operation=request_init` 记录，其中的 `handler.factory` 是 Init 期间的预建工作。随后调用里的近零 factory 只是缓存命中，不代表构造没有成本。

### 2. 找出耗时集中的阶段

检查 `handler.factory`、`handler.work`、`snapshot.load`、`auth.oidc`、`search.authorize` 和 `model.embedding`。阶段是嵌套计时，`handler.work` 已包含内部执行；完整阶段范围见[冷启动分段计时](service.md#冷启动分段计时)。没有执行的阶段不会出现，不能把缺项当作零耗时测量。

### 3. 根据瓶颈选择后续变更

当前代码默认 Search 内存 2048 MiB、REQUEST 内存 1769 MiB，后台函数各 1024 MiB，见[函数定义](../infra/functions.py)。这些是部署配置，不是已测内存峰值。REQUEST 按约 1 个 vCPU（处理器计算份额）需求定尺寸，首次初始化收益与回滚步骤见[容量说明](capacity.md#request-默认尺寸与回滚)。先核对实际 alias 和配置，再参考[当前冷启动实测与可选方案](known-limitations.md)评估优化。REQUEST 专包支持可选预置并发，示例默认 0；Search 不配置该能力。新环境启用前核实配额、费用和部署范围，按[部署流程](deploy.md)重新合成 assembly。

### 看到什么算成功

能够把客户端端到端耗时、Gateway/网络时间、平台初始化和应用主要阶段分开解释，再用相同请求路径复测。不要只看 Search Lambda `Duration` 就宣称客户端时延达标。

### 出错怎么办

有时长但没有最终结构化日志时，检查 REPORT 的错误、内存及超时信息。不要靠频繁修改函数配置制造冷启动，或把不同代码、内存和索引版本的样本混在同一分位数中。

REQUEST PC 预建失败会记录 `request_init / failed`，并抛出固定错误
`REQUEST initialization failed`，使平台 Init 失败。它不会伪装成健康环境后继续
服务请求；无需把 Init 混入请求次数或错误率过滤器。检查平台 `INIT_REPORT` 和
预置并发配置的 `Status`/`StatusReason`，不能只看 invocation Errors 或 `first_invocation`。
构造过程中已成功的子对象可能留在该失败进程的缓存里；固定版本配置一旦加载，
在原引用下不会重新读取。配置错误须发布正确配置并更新版本引用，等待新的环境
初始化成功，而不是假定下一次 handler 调用能修复它。中断或进程退出同样使 Init 失败。
上述致命边界适用于 factory 构造。后续 OIDC 预取的失败保留
`request_init / ready`，但 `oidc_prefetch.status=failed`，并记录受控 `failure_kind`
以及可用的脱敏 `auth_failure`。`dependency` 通常表示身份依赖读取或文档错误，
也可能是本地预算耗尽造成的误分类；`structural` 表示程序结构或接线故障，
`deadline` 表示预取触及软件截止时间。以下两项已知遥测限制需要一起排查，
不能仅凭失败类别认定 IdP 故障。预取失败不采用这次取得的缓存，
下一请求重新读取元数据。Init 的 JWKS attempt 带 `phase=init`，应与业务请求分开
统计恢复次数；超过 300 秒 TTL 后首批请求仍可能同步刷新，READY 不能代替这项验证。

预取在父进程内使用独立 HTTP 客户端和独立 verifier，同步完成 discovery、
JWKS 和共享一次补偿；不创建后台任务，也不预热业务请求的连接池。默认工作截止为
预取开始后的 2.75 秒，客户端关闭及缓存采用仍受同一个开始时刻后 3.0 秒的
软件截止时间约束。父进程借用空闲的 `SIGALRM` 定时器中断可中断 I/O，
随后关闭自己的客户端并还原信号状态；非主线程、已占用或被屏蔽的定时器等情况
会跳过预取并记 `structural`。此预算不包含 factory、最终 Init 摘要写出，
也不代表平台 Init 的总时限。原生 `getaddrinfo` / OS 调用可能延迟信号处理，
不能据此承诺 3 秒硬上界。
若 Init 明显超出预算，检查同环境的解析配置和平台 `INIT_REPORT`，不要寻找额外执行进程。

- **日志 FD 背压**：成功读取 JWKS 后写 attempt 日志仍在工作预算内；
  若日志 FD 阻塞，定时器可能把原本成功的结果改判为 `deadline` 并丢弃缓存。
  现场先关联同一 `@logStream` 的 Init 摘要，比较 `auth.discovery`、
  `auth.jwks` 与 `auth.oidc` 的计时和错误数，再检查同期日志采集延迟、突发输出
  及 attempt 行是否完整。JWKS 成功且很快、整体接近工作截止，是排查日志背压的
  线索，不能单独证明根因；不要将它当作 IdP 读超时或扩大 IdP 重试来处理。
- **本地预算被报成依赖失败**：某次尝试开始前发现剩余预算已耗尽时，
  当前实现会生成 `failure_kind=dependency`、`category=timeout`、`reason=read`。
  这次尝试没有发生 I/O，却可能覆盖上一尝试的真实原因并耗用共享补偿额度；
  因此这组字段不证明 IdP 实际发生了读超时。结合 discovery/JWKS 的
  `count`、`duration_ms`、`errors` 和 Init 总耗时判断：例如只记录一次 discovery
  而最终报 `timeout/read`，需要核对是否在补偿开始前已耗尽本地预算。
  `auth.oidc` 不含独立客户端构造耗时，不能把它单独作为完整预算时钟；
  attempt 事件只有 JWKS 才有，缺少 discovery 事件也不能证明未执行读取。
  现有字段无法在所有边界场景里恢复被覆盖的原因；证据不足时记录“本地预算 /
  IdP 慢待区分”，保留分段计时和同窗口 IdP 侧只读观测后再归因。

部署回读与超时处置见
[REQUEST PC 部署方法](deploy.md#request-预置并发验证)。
预取中已有补偿 GET 正常返回时保留 `metadata_recovered=true`，即使随后失败；
只有预取最终成功才记 `auth_recovered=true`。

## 我要查询日志与指标

日志组名称以函数版本的 `LoggingConfig.LogGroup` 为准；若模板使用 `Ref`，解析到对应
LogGroup 资源的 `LogGroupName` 后再查询；不要从函数名拼接默认日志组路径。

以下查询在 CloudWatch Logs Insights 查询编辑器中执行，不是应用命令。选择实际部署日志组及故障时间范围；查询只读取已有受控字段，不要求打开请求正文或凭证日志。字段来源为 [runtime.py](../src/semantic_service/runtime.py)、[阶段定义](../src/semantic_core/_timing.py)及[Indexer 计数定义](../src/semantic_service/_indexer_telemetry.py)。

### 1. 查看失败类别与请求范围

```text
fields @timestamp, operation, status, duration_ms, aws_request_id, first_invocation
| filter operation in ["request", "request_init", "search", "indexer", "enhancer"]
| filter status in ["failed", "internal failure", "index not ready", "unauthenticated", "unauthorized", "rate limited"]
| sort @timestamp desc
| limit 100
```

REQUEST 专包的预建阶段需单独查询，不能只筛选首次 invocation：

```text
fields jsonParse(@message) as record
| filter record.operation = "request_init"
| fields @timestamp, @logStream, record.status, record.request_entry_import_ms,
         record.duration_ms, record.oidc_prefetch.status,
         record.oidc_prefetch.failure_kind,
         record.oidc_prefetch.auth_failure.category,
         record.oidc_prefetch.auth_recovered,
         record.oidc_prefetch.metadata_recovered,
         record.stage_timings.`handler.factory`.duration_ms,
         record.stage_timings.`handler.factory`.errors
| sort @timestamp desc
| limit 100
```

这类记录按执行环境关联 `INIT_REPORT`，没有 `aws_request_id`。
按需运行与构建机导入不产生 `request_init` 记录；日志缺项不能当作测得零耗时。

专查“Init 已就绪，但预取失败”，不能沿用只过滤顶层失败状态的查询：

```text
fields jsonParse(@message) as record
| filter record.operation = "request_init"
| filter record.status = "ready" and record.oidc_prefetch.status = "failed"
| fields @timestamp, @logStream, record.duration_ms,
         record.oidc_prefetch.failure_kind,
         record.oidc_prefetch.auth_failure.endpoint,
         record.oidc_prefetch.auth_failure.category,
         record.oidc_prefetch.auth_failure.reason,
          record.oidc_prefetch.metadata_recovered,
          record.stage_timings.`auth.oidc`.duration_ms,
          record.stage_timings.`auth.discovery`.count,
          record.stage_timings.`auth.discovery`.duration_ms,
          record.stage_timings.`auth.discovery`.errors,
          record.stage_timings.`auth.jwks`.count,
          record.stage_timings.`auth.jwks`.duration_ms,
          record.stage_timings.`auth.jwks`.errors
| sort @timestamp desc
| limit 100
```

将结果与 `semsearch/<prefix>` 命名空间中的 `request-init-oidc-prefetch-failed`
五分钟 Sum 及同名告警核对；实际 MetricFilter 与 Alarm 名均带部署前缀。
Init 成功补偿另回读 `request-init-auth-recovered`。指标以摘要计数，
不把逐次 GET 或 Init 混入业务请求数。

Init 的 JWKS 尝试按日志流和 Init 时间窗口查看：

```text
fields jsonParse(@message) as record
| filter record.operation = "oidc_fetch" and record.phase = "init"
| fields @timestamp, @logStream, record.attempt, record.compensation,
         record.duration_ms, record.outcome, record.failure
| sort @timestamp asc
```

请求期事件没有 `phase` 键，因此用字段缺失条件筛选：

```text
fields jsonParse(@message) as record
| filter record.operation = "oidc_fetch" and not ispresent(record.phase)
| fields @timestamp, record.aws_request_id, record.attempt, record.compensation,
         record.duration_ms, record.outcome, record.failure
| sort @timestamp asc
```

缺失字段与字符串 `init` 的不等比较不会命中请求期事件。只统计事件已完整写出的
窗口；discovery 没有独立尝试事件，不能从缺项推断没有恢复。

### 2. 查看 embedding 错误分布

```text
fields operation, embedding_error.kind, embedding_error.http_status
| filter operation = "search" and ispresent(embedding_error.kind)
| stats count(*) as error_count by embedding_error.kind, embedding_error.http_status
| sort error_count desc
```

`error_count` 是此查询的临时结果列，不是服务发布的指标名。

### 3. 查看首调用和阶段耗时

```text
fields jsonParse(@message) as record
| filter record.operation = "search" and record.first_invocation = true
| fields @timestamp, record.aws_request_id, record.duration_ms, record.runtime_module_init_ms,
         record.stage_timings.`handler.factory`.duration_ms,
         record.stage_timings.`handler.work`.duration_ms,
         record.stage_timings.`snapshot.load`.duration_ms,
         record.stage_timings.`model.embedding`.duration_ms
| sort @timestamp desc
| limit 100
```

`record` 是查询临时解析出的对象，不是服务日志字段。阶段名包含点号，查询使用反引号保留该 JSON 键；`jsonParse` 和特殊键语法见[CloudWatch Logs Insights 结构类型](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/CWL_QuerySyntax-operations-functions.html#CWL_QuerySyntax-structure-types)。若查询没有结果，先查看该条结构化 `@message` 核对格式；查询在客户日志环境的实际执行结果**未核实**。

### 4. 核对 Indexer 恢复和最终结果

```text
fields @timestamp, status, gateway_401_count, gateway_403_count,
       token_proactive_refresh_count, gateway_replay_count, gateway_replay_success_count,
       gateway_replay_failure_count, gateway_replay_skipped_count,
       gateway_auth_refresh_failure_count
| filter operation = "indexer"
| sort @timestamp desc
| limit 100
```

### 看到什么算成功

查询时间范围内能看到相应函数的最终日志，请求按 `aws_request_id` 找到同次调用的平台记录，
Init 按日志流与时间窗口关联。指标浏览器中选择 `semsearch/<prefix>`，例如查看
`search-degraded`、`vector-failed`、`embedding-error-read-timeout`、`poll-success`、
`request-auth-recovered`、`request-init-auth-recovered` 和
`request-init-oidc-prefetch-failed` 的 `Sum`；定义见[指标过滤器](../infra/monitoring.py)。
默认计数只在有匹配日志时产生，不能凭空白图表认定没有故障。

### 出错怎么办

先核对日志组、区域、时间范围和实际部署模板，再检查过滤器是否与当前代码一同部署。只更新 Lambda 包不会创建新的指标过滤器。不要把模型返回头用于指标维度，也不要为了定位错误开启 query、JWT 或 secret 明文日志。

## 我要续期或轮换凭证

### 1. 识别要更新的凭证

| 凭证 | 维护方和用途 | 正常更新方式 |
|---|---|---|
| Agent 访问令牌 | 客户 IdP；供客户端连接 Gateway | 到期前按客户登录流程续期；不是 Secrets Manager 中的服务密钥 |
| OAuth 客户端凭证 | 身份管理员；供 Indexer 取目录令牌 | 更新客户 IdP 与 `SEMSEARCH_OAUTH_SECRET_ID` 指向的秘密版本，验证后台令牌获取 |
| 模型 API key | 模型账号管理员；供模型代理调用 | 协调上游 key 与 `MODEL_SECRET_ID` 的版本提升；Secrets Manager 不替上游创建或撤销 key |
| 请求签名 key | 服务安全管理员；REQUEST 签发、Search 验证 | 按 `SEMSEARCH_SIGNING_SECRET_ID` 的版本与 `activated_at` 完成轮换 |
| IdP 签名密钥及 JWKS | 身份管理员；验证外部令牌 | 保留足够公钥重叠窗口，验证新 token 与缓存传播 |

### 2. 按正常轮换窗口发布

完整步骤见[轮换与紧急撤销](deploy.md#轮换与紧急撤销)。当前 signing 旧版本保留窗口默认至少 390 秒，来自缓存有效期、签名有效期、时钟偏差及余量之和；它是[Secrets 配置](../src/semantic_service/secrets.py)的兼容窗口，不是固定服务承诺。修改任何组成项都须重新计算。

Indexer 缓存令牌会在不晚于半寿命时主动刷新，同时保留更早的安全余量和缓存上限，见 [OAuthTokenSource](../src/semantic_service/catalog.py)。不要另加定时任务高频强制取令牌。

### 3. 泄露时执行紧急撤销

紧急撤销不能继续采用正常旧版本宽限。`SEMSEARCH_REVOKED_KIDS` 已有运行时支持，但当前 [CDK 配置分发](../infra/stack.py)未将它分发到 Search 和 REQUEST；直接加入 `service_settings` 会被拒绝。应先关闭受影响入口，再通过经评审的配置分发变更同步注入两条路径、发布新版本，并确认 alias 切换及旧签名被拒绝。不能把“修改一个现成配置开关”当成当前已具备的完整撤销流程。

主体撤销由授权管理员更新 `disabled` 或 `not_before` 并推进策略版本。只改变 Secrets 的 stage 不会立即清除暖实例缓存；配置分发和拒绝验证完成前，受影响入口保持关闭。完整恢复边界见[部署手册](deploy.md#轮换与紧急撤销)。

### 看到什么算成功

新凭证完成预期路径；正常轮换的窗口边界符合配置；紧急撤销的旧签名和主体立即按方案被拒绝。报告仅记录版本引用和验证结论，不记录密钥、JWT 或完整认证头。

### 出错怎么办

先核对 IdP/模型上游与 Secrets 版本是否同步，再看运行角色读取权限、缓存和实际 alias。授权状态不能证明完整时保持停服，不能通过放宽 token lifetime 或允许旧 key 恢复。

## 我要清理或退役环境

### 1. 停止调用和后台写入

由服务负责人确认退役范围，停用入口、两个 schedule 以及手工和管理写入，排空在途调用。确认 Gateway 已删除后，由授权管理员执行永久 `tombstone` 流程，保留最新授权及撤销记录。

### 2. 区分栈资源与保留数据

本栈的 S3、两张 DynamoDB 表、日志及已发布 Lambda versions 使用保留策略；两表有删除保护。删除 stack 不等于删除数据，不能作为回滚方法。重新创建同名资源可能需要显式导入与权限重连，见[保留数据说明](deploy.md#回退和保留数据)。

### 3. 审核具体删除清单

当前没有自动垃圾回收。清理由独立角色执行，先按活动指针、旧版本、读取窗口及成功发布记录形成清单，再批准删除。保留规则和时间窗口以[部署手册的清理要求](deploy.md#回退和保留数据)为准；不能设置简单的时间生命周期规则替代引用检查。三个 secret 的上游凭证撤销也应分别协调。

### 看到什么算成功

入口不再接受业务调用，后台不再写入，永久删除状态可验证，保留和删除范围与批准清单一致。不存在靠旧快照或旧权限重新启用已退役 Gateway 的路径。

### 出错怎么办

发现仍有在途写入、引用不明或最新授权记录缺失时，停止数据删除并恢复核对。跨区域灾备和表恢复按独立方案执行，不把历史备份直接切为在线授权权威。
