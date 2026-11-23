# 部署与恢复手册

本文提供每个 Gateway 独立部署计算、存储和运行角色的详细步骤，供部署负责人使用。
它落实[项目总览](overview.md)中的工具搜索方案，重点是资源绑定、权限与恢复顺序。
读完后，你可以准备配置、检查模板，并在已获授权的账号中部署、验证或回滚。
日常值班请先看[运维手册](operations-runbook.md)，配额放行输入见[配额工作表](quotas.md)，运行字段见[服务配置](service.md)。

本手册部署通用制造业样例。所有账号、资源与身份信息必须来自你的部署清单。
已有 ECS 后端迁移请先阅读[迁移手册](migrate-to-lambda.md)，不要直接用关闭 ECS 的整栈模板覆盖已有部署。

## 资源和区域边界

默认复用已有 Gateway、Search target 和 Gateway execution role。此 stack 创建四个
Python 3.12 x86_64 Lambda 及 `live` aliases、独立运行角色、版本化 S3 bucket、
两张物理DynamoDB表、两个Scheduler schedule、日志、指标过滤器及告警。每个角色的非秘密
配置另外生成不可变S3对象，以精确VersionId/字节数/SHA-256引用。S3、两张table、
日志组保留；已发布 Lambda versions 在函数存续期间保留，删除整个函数会删除全部版本及 alias。
默认 ECS 关闭时不创建 secret 值、IdP、VPC/NAT、Function URL、
X-Ray、CloudTrail 事件链、SQS 或 DLQ。
显式启用可选 `ecs_search` 后增加 facade/fallback、ECS、internal NLB、私有子网、
NAT 和 S3/DynamoDB gateway endpoint 等资源，详见[ECS 运维](ecs-search-operations.md)。

每Gateway的`semsearch-{stage}-{deployment_name}-state`保存CONTROL、rate、
enhancement-cache与enhancement-progress；同前缀的`-protected`表只保存PROTECTED。
两表各自采用`AWS_MANAGED` KMS加密、on-demand、字符串`pk`、PITR、
deletion protection及RETAIN；不另外创建客户KMS key。只有state表配置
`expires_at` TTL，protected表没有TTL，永久tombstone不得过期删除。

| Handler | 选择条件 | 内存 / Lambda timeout | reserved concurrency |
|---|---|---|---|
| `semantic_service.runtime.search_handler` | 共享包 | 2048 MB / 10s | 不设置，使用账号可用并发；容量按配额工作表验收 |
| `semantic_service.runtime.request_handler` | `request_package: "shared"`（默认） | 1769 MB / 10s | 不设置，使用账号可用并发 |
| `semantic_service.request_runtime.request_handler` | `request_package: "request"` | 1769 MB / 10s | 不设置；REQUEST `live` alias 可另配预置并发 |
| `semantic_service.runtime.indexer_handler` | 共享包 | 1024 MB / 45s | 1 |
| `semantic_service.runtime.enhancer_handler` | 共享包 | 1024 MB / 240s | 1 |

`request_provisioned_concurrency` 默认 `0`，只允许非负整数；大于零时必须选择
REQUEST 独立包，并仅在 REQUEST `live` alias 上生成
`ProvisionedConcurrencyConfig`。它不修改其他函数、预留并发、Gateway 或告警。
生产和示例默认 0；预置并发的持续费用与收益须单独验证。

| 能力 | 北京 `cn-north-1` | 宁夏 `cn-northwest-1` | 部署门槛 |
|---|---|---|---|
| ARN / AWS endpoint | `aws-cn` / `amazonaws.com.cn` | 相同 | 模板使用 `Aws.PARTITION`、`Aws.URL_SUFFIX`；Gateway URL 使用客户确认的真实值 |
| S3、Lambda、DynamoDB、Secrets Manager、KMS、EventBridge | 官方中国区文档列出服务可用 | 相同 | 具体账号、PITR、条件写、Scheduler目标投递、IAM和区域额度待真实验证 |
| AgentCore Gateway | 官方文档列出可用；无 semantic search / Gateway rules / WAF integration | 相同 | 使用 CUSTOM_JWT；不启用上述不可用功能 |
| REQUEST interceptor、Lambda Target、context/MCP绑定 | 须在目标账号验证 | 待真实验证 | 离线样例不代替CUSTOM_JWT、alias及签名字符串端到端授权验证 |
| CloudFormation Gateway / GatewayTarget | 区域 registry 支持待真实验证 | 区域 registry 支持待真实验证 | 默认不创建或修改 Gateway/Target；通用模板参考存在不代表两区可部署 |
| public OIDC discovery/JWKS、OAuth、模型出站 | 客户目标 endpoint 待真实验证 | 相同 | Gateway 服务侧公网可达 IdP；Lambda 连通性不能代替 Gateway 探针 |
| Lambda X-Ray integration / Function URLs | 不支持 | 不支持 | 无相关资源或配置 |
| SQS DLQ managed redrive | 不支持 | 不支持 | 本 stack 无队列；以后可选扩展使用审批后的受控重放 |
| Python 3.12 / x86_64 wheel加载、冷启动、性能 | 待目标环境验证 | 待目标环境验证 | 本地 hash/tag/ABI 静态检查不是 Linux import 或真实运行证据 |

中国区事实沿用[设计第 3.1 节官方来源](design.md#31-中国区事实)。
本地回归使用[合成夹具](../tests/fixtures/README.md)。Search 使用受支持的 metadata 子集，运行时仍执行完整的长度、未知参数、签名和授权检查。官方通用模板类型为
[`AWS::BedrockAgentCore::Gateway`](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-bedrockagentcore-gateway.html)
和
[`AWS::BedrockAgentCore::GatewayTarget`](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-bedrockagentcore-gatewaytarget.html)；
部署前须查询目标区域 CloudFormation registry。确认支持及现有资源所有权后，才能另行
决定是否增加可选创建路径；不能用未验证的自定义资源或猜测资源类型补齐。

资源名使用 `semsearch-` 前缀并包含 stage、部署标识；资源支持标签时统一
`project=semsearch`，另带 stage。Scheduler 的可标记资源是 schedule group，schedule
通过该 group归属；不把未支持 `Tags` 的资源伪装成可独立打标签。stage/部署标识和
Gateway identity是长期身份，变更可能替换资源，必须先检查 change set。

## 前置条件

1. 部署任务具有单独的账号、区域、凭证和资源变更授权。部署身份与运行身份、授权
   管理员、工具发布者分离；部署任务自行验证会话身份，不从本地合成结果推定账号。
2. 为目标区域填写并批准[配额工作表](quotas.md)。确认业务与 Indexer 共享的 Gateway 额度、
   Lambda 并发、DynamoDB 热键、Scheduler、IdP/provider 限额、预算和告警接收负责人。
   不用实际 throttling结果替代内存检索容量实验。
3. 取得已有 Gateway ID、真实 URL、execution role、Search target ID、完整工具名、
   incarnation、真实 descriptor 和显式工具 registry。Search metadata按设计第2.2节
   使用根`type/properties/required`、`query:string`及可选`_auth_context:string`和说明；
   从实际回读描述符计算摘要，不补回Gateway未保留的约束。registry中的 target ID和
   incarnation必须与当前资源匹配；所有业务工具需要审核过的 schema digest与 ACL。
   同名重建不得继承旧身份。MCP target注册前须明确配置DEFAULT或有效静态schema，
   并保存证明模式来源的原始Get回读；旧endpoint-only响应与DYNAMIC仍不准入，
   静态 schema 与 DEFAULT 模式都须按目标账号的实际回读验证。
4. CUSTOM_JWT IdP提供已核验的 issuer/audience、discovery/JWKS、机器 OAuth token
   endpoint、非对称算法、claim 映射、机器 scopes和主体。公网可达前提仍需
   Gateway 服务侧验证；中国区不用 Cognito User Pool或 private IdP configuration。
5. Secrets管理员在同账号/区域准备三个不同的 secret，交付精确名称和实际 ARN后缀，
   以及可选客户 KMS key ID。CDK只引用名称/ARN，不创建、获取或保存值。默认模型名
   `semsearch/model-api`；signing与OAuth分别独立。内容结构见下表。
   另外准备已有、开启versioning的配置bucket，可使用已批准的CDK bootstrap artifact
   bucket；其名称通过`ConfigurationBucketName`参数传入，CDK不会创建或修改该外部bucket。
   部署负责人须确认它同样加密、阻断公共访问、强制TLS，并且lifecycle不会删除当前或
   回退版本引用的配置对象。
6. 初始保持模型和 schedule关闭，待配置、授权、Gateway绑定和 canary通过后启用。
   模型启用前必须核验目标 host、TLS、路径、维度、model echo、请求选项、预算；
   模型协议使用[显式配置](service.md#secrets-与模型)，但rerank保持关闭，
   直到真实Lambda延迟、相关性、预算及启用决定均通过。
7. 现有 Gateway/Target的变更者已准备请求和回滚副本；完成真实 REQUEST overwrite、
   Search event/context、分页和错误包装的生产配置验证后才能开放多权限流量。
   离线 fixture 与 CDK synth 成功均不代替
   CUSTOM_JWT/header、精确alias及生产签名字符串的完整往返门槛。
8. 部署前，按既有部署 Search 和 Enhancer 的配置引用读取各自的
   固定版本 S3 配置，核对 `MODEL_EMBEDDING_TIMEOUT_SECONDS` 是 `(0, 6]` 内的
   有限秒数，并核对 Search 的 `SEMSEARCH_SEARCH_TIMEOUT_SECONDS` 为 `(0.3, 6]`
   内有限秒数。代码默认分别为 `1.0` 和 `3.0` 秒。若现值超界或无效，先核实单位并改为正确秒数，
   发布并引用新的不可变配置版本，再部署代码。仅检查本地配置示例或 S3 当前版本
   不能完成这项核对。

当前源码默认与各函数已部署配置须分别核对；固定 S3 配置可以覆盖代码默认值。

| Secret | 管理员写入的 JSON | 读取角色 |
|---|---|---|
| 模型，默认 `semsearch/model-api` | `base_url`、`api_key` | Search、Enhancer，模型启用时使用 |
| 签名，默认 `semsearch/signing` | `key_b64`、`activated_at` | Search、REQUEST |
| OAuth，默认 `semsearch/indexer-oauth` | `client_id`、`client_secret` | Indexer |

`key_b64`解码后至少32字节；`activated_at`是该版本实际提升为 AWSCURRENT的UTC epoch，
不是 secret创建时间。不要将值放入配置 JSON、命令行参数、CloudFormation outputs或证据。
secret ARN的AWS生成后缀必须来自资源登记，不能用通配符或猜测后缀代替。使用客户 KMS
key时，运行角色只读对应 key，并限定 Secrets Manager的ViaService及secret encryption
context；部署前以实际 key policy核对，不假设 IAM allow能覆盖 key policy拒绝。

## 本地依赖、打包和模板检查

采用 x86_64以对应设计中的Lambda成本基线，并明确选用 CPython 3.12/Linux x86_64
wheel集合。开发机的native wheel不可直接打包。Python依赖仅安装在本worktree
`.venv`，版本和分发物hash锁定；CDK CLI仅使用worktree的`node_modules`。
安装、审计和全量测试命令见[README](../README.md)。

```sh
.venv/bin/python scripts/build_lambda.py
.venv/bin/python scripts/build_lambda.py --offline
.venv/bin/python scripts/build_lambda.py --operation request --output .artifacts/request.zip
.venv/bin/python scripts/build_lambda.py --operation request --output .artifacts/request.zip --offline
.venv/bin/python scripts/audit_dependencies.py
.venv/bin/python scripts/audit_dependencies.py --operation request
```

构建输入是[目标 wheel锁](../lambda-wheels.lock.json)，输出均在忽略目录中。
共享输出是 `.artifacts/lambda.zip` 与 `.artifacts/lambda.manifest.json`，独立
REQUEST 输出是 `.artifacts/request.zip` 与 `.artifacts/request.manifest.json`。
两个 manifest 分别记录 operation、实际选择的 wheel、目标平台、包大小和 ZIP
摘要；REQUEST 还记录已裁依赖与闭包来源，不能只凭完整锁摘要推断发布内容。
REQUEST 专包的模块集合固定，但包内的
[config.py](../src/semantic_service/config.py)、
[deployment_config.py](../src/semantic_service/deployment_config.py)、
[runtime.py](../src/semantic_service/runtime.py) 等共享源码内容改变时，
ZIP 摘要也会改变。REQUEST 运行时不调用 `parse_search_config`，不执行
Search 预算与模型计时路径；这不等于专包字节不变。比较源码清单、内容摘要及当前
manifest，不能要求与旧部署的 ZIP 摘要相同。
REQUEST 构建不能覆盖共享 `lambda.zip`。首次只下载锁定的PyPI分发物；后续`--offline`
只使用经过hash验证的缓存。jieba缺少发行wheel，因此仅用锁定的源分发物和本地
setuptools后端生成纯Python wheel，并校验它的预期hash。构建不从宿主`.venv`
复制native模块。

ZIP文件顺序、时间戳、权限和存储方式固定。同一源码和构建输入的重复构建必须产生
相同 SHA-256；源码改变后应记录新的产物摘要。目标wheel
tag/ABI静态检查、两份产物各自的依赖闭包审计和250MiB解压大小门槛通过后，仍需
在 Linux x86_64 Lambda 同款镜像验证原生扩展、Botocore 模型数据、jsonschema
资源及完整 REQUEST handler。macOS 使用 Finch，挂载源必须为 `/private/tmp`
下的绝对路径；`/tmp` 符号链接可能使挂载失效：

```sh
(
set -eu
SEMSEARCH_REQUEST_CHECK=$(mktemp -d /private/tmp/request-package.XXXXXX)
unzip -q .artifacts/request.zip -d "$SEMSEARCH_REQUEST_CHECK/package"
cp tests/fixtures/request-package-smoke.py "$SEMSEARCH_REQUEST_CHECK/smoke.py"
for SEMSEARCH_INIT_MODE in on-demand provisioned-concurrency; do
  finch run --rm --platform linux/amd64 --network none \
    -v "$SEMSEARCH_REQUEST_CHECK:/opt/request-check:ro" \
    -e AWS_CONFIG_FILE=/dev/null -e AWS_SHARED_CREDENTIALS_FILE=/dev/null \
    -e AWS_EC2_METADATA_DISABLED=true -e AWS_ACCESS_KEY_ID=isolated-fixture \
    -e AWS_SECRET_ACCESS_KEY=isolated-fixture -e AWS_REGION=cn-north-1 \
    -e AWS_LAMBDA_INITIALIZATION_TYPE="$SEMSEARCH_INIT_MODE" \
    --entrypoint python public.ecr.aws/lambda/python:3.12 \
    -I -S /opt/request-check/smoke.py /opt/request-check/package
done
)
```

仿真容器耗时不能用于 Lambda 性能判断。SBOM、两份构建 manifest、ZIP 摘要、
闭包审计及容器验证日志保存在操作者指定的本地输出目录，不加入 Git。

离线模板检查使用[隔离runner](../scripts/synth.py)，它运行本地真实CDK CLI，清空继承
环境，使用临时用户目录，credential/config路径指向`/dev/null`，关闭IMDS、lookup和
CLI通知/版本报告，不读取本机AWS profile：

```sh
.venv/bin/python scripts/synth.py --region cn-north-1
.venv/bin/python scripts/synth.py --region cn-northwest-1
```

默认输入是synthetic示例；可用`--config <file>`、`--stage <stage>`、
`--output <directory>`明确覆盖。默认两区模板分别保存在`.artifacts/cdk-cn-north-1`
和`.artifacts/cdk-cn-northwest-1`。**每次synth会重写`.artifacts/configuration`**，
因此实际部署前必须用最终account/region配置重新synth，将该次assembly与四份配置
manifest成套保存；不能把另一地区的配置VersionId混入。

启用 REQUEST 独立包必须先在版本管理的部署配置中保存
`"request_package": "request"`，再使用该配置重新 synth。默认路径为
`.artifacts/request.zip`；路径变化时传入 `--request-asset`：

```sh
.venv/bin/python scripts/synth.py --config "$SEMSEARCH_DEPLOY_CONFIG" \
  --region cn-north-1 --request-asset .artifacts/request.zip \
  --output .artifacts/cdk-cn-north-1
```

配置为 `shared` 时提供 `--request-asset` 会报错，避免一次性开关在下次部署
静默丢失。空值、目录、非 ZIP、缺少实际入口模块、REQUEST 包误用于共享函数，
以及已有 manifest 与 operation/ZIP 摘要不符，都在 CDK stage 资产前拒绝。
runner 合成结束后从 assembly manifest 读取模板，正向确认 REQUEST `Handler`
恰为 `semantic_service.request_runtime.request_handler`；缺失或仍为旧入口即失败。
不要把 `-c request_asset=...` 追加到下面 `cdk deploy --app <assembly>` 的命令：
该命令使用已经合成的模板和资产，不再执行 app 或解析新的资产选择。

后续普通 synth 继续读取配置中的独立包选择；不需要重复路径参数。若确需回到共享包，
将配置改为 `request_package: "shared"` 且预置并发归零，再对原输出目录使用
`--confirm-request-rollback`。runner 会拒绝缺少该确认的同一 assembly 回退。
这个本地检查不查询已部署栈；使用新的输出目录时仍须按下文核对实时 alias 和模板。

检查两个China region的模板，确认IAM/S3/DDB/Scheduler/日志/标签断言、无明文值、
无Global ARN硬编码及alias限定权限。不运行`cdk context --reset`后再做在线发现。

## 参数及运行配置

CDK配置JSON只放非秘密配置，格式见[示例](../infra/config.example.json)和
[校验器](../infra/config.py)。示例包含`.invalid`域名和synthetic身份，仅用于离线
synth，不能部署。CLI通过`-c config=...`选定文件，`-c stage=...`、`-c region=...`
可覆盖对应字段；account和region必须显式给出，禁止从凭证查询。配置变更和输出应
与已批准的配额工作表绑定同一摘要。

| 参数组 | 必须审核的内容 |
|---|---|
| 部署身份 | `stage`、`deployment_name`均必填、1–12位小写名称；`account`12位、`region`只允许北京/宁夏；stack名为`semsearch-{stage}-{deployment_name}` |
| Gateway绑定 | `gateway_id`、`gateway_url`、`gateway_allowed_hosts`、`gateway_execution_role_name`、`search_target_id`、`search_incarnation`、`search_descriptor`、`registry` |
| IdP/机器身份 | `oidc`映射中的issuer/audience/discovery/JWKS/token endpoint、精确host allowlist、算法/claims、机器scopes；`machine_principals`为issuer/subject数组 |
| Secrets引用 | `secrets.model/signing/oauth`各含实际6字符`suffix`、可选`name`与精确`kms_key_id`；默认名称分别为`semsearch/model-api`、`semsearch/signing`、`semsearch/indexer-oauth` |
| 检索与模型 | `service_settings`/`model_settings`为允许的运行时配置映射；`models_enabled=false`；服务默认`vector_bm25_fallback`，两项拒答门槛省略、rerank关闭；当前代理显式维度1024、batch10，host单独登记 |
| 调度 | `schedules_enabled=false`；固定60s/300s频率，固定0 retry/60s max age |
| REQUEST 打包与预置并发 | `request_package="shared"` 默认共享包；`"request"` 持久选择独立包；`request_provisioned_concurrency=0` 默认关闭，仅在独立包模式允许正整数 |
| 可观测性 | `log_retention_days=30`，允许7/14/30/60/90/180/365；`alarm_topic_arns`可引用同账号同Region已有SNS topic；默认无消息动作 |

以下是CloudFormation部署参数，不是secret值，也不会在本次离线synth时查询：

| 参数 | 值来源 |
|---|---|
| `ConfigurationBucketName` | 部署任务核验的已有、同Region、private/versioned/TLS-only配置bucket |
| `SearchConfigVersionId` | 上传`.artifacts/configuration/search.json`返回的实际VersionId |
| `RequestConfigVersionId` | 上传`.artifacts/configuration/request.json`返回的实际VersionId |
| `IndexerConfigVersionId` | 上传`.artifacts/configuration/indexer.json`返回的实际VersionId |
| `EnhancerConfigVersionId` | 上传`.artifacts/configuration/enhancer.json`返回的实际VersionId |

输出为`IndexBucketName`、`StateTableName`、`ProtectedTableName`、`GatewayArn`、`Partition`、
`RegionalS3Endpoint`、`SearchAliasArn`、`RequestAliasArn`、`IndexerAliasArn`、
`EnhancerAliasArn`和`GatewayProvisioning`。Gateway管理员使用Search/REQUEST alias
输出绑定已有资源；本stack只向已有execution role附加这两个alias的invoke权限。

完整配置契约见[服务配置](service.md)。按配置契约，大registry、descriptor、OIDC、
模型选项放入按角色拆分的不可变S3配置对象，Lambda环境只含身份、存储定位和配置
reference；该reference固定对象key、VersionId、字节数和SHA-256，加载失败即拒绝。
每角色只授予自己的精确配置对象版本读取权限。配置对象不含secret值；模型读取权限
不交给Indexer/REQUEST，OAuth读取权限不交给Search/Enhancer。使用实际descriptor，
不用fixture的工具身份进入生产。当前单角色配置上限1MiB，synth和runtime均验证；
大规模registry按实际JSON字节验收，超限不得截断或声称已支持目标工具数，需调整有界
配置设计后重新评审。

CDK 显式发出 `SEMSEARCH_FUSION_STRATEGY=vector_bm25_fallback` 和
`MODEL_RERANK_ENABLED=false`，除非部署配置显式覆盖。可选
`SEMSEARCH_MIN_VECTOR_SIMILARITY`、`SEMSEARCH_MIN_VECTOR_MARGIN` 分别接受
[-1, 1]、[0, 2] 内有限数的字符串；省略则没有拒答门槛。保持已有词法兜底选择
阈值 0.5，它不用于拒答。运行时和 CDK 共用服务 parser，非法模式、门槛及 Boolean
按字段拒绝；全局模型关闭时仍验证 rerank 开关。S3 v1 格式保持不变，其显式设置
覆盖环境变量，旧配置缺字段时采用合并后的值或服务默认。配置内容变化会改变
对象 SHA-256、不可变 key 及函数配置绑定，须连同对应 Lambda 版本一起审核。
核心库 `SearchConfig` 的 RRF 默认及 Gateway request/response 契约保持不变。

Indexer配置`SEMSEARCH_MCP_VERSIONS=2025-03-26`，保留协商allowlist。所有外部请求及
Indexer机器请求使用字符串JSON-RPC ID；numeric、null和boolean均拒绝，
通知仍不带ID。Search封套进一步要求ID非空且最多256字符。
不得把数值ID转换为字符串来绕过签名的精确绑定。
当前代理base已含`/v1`，可登记经测试的`MODEL_RERANK_PATH=/rerank`、
`MODEL_RERANK_ENVELOPE=flat`、`MODEL_RERANK_SCORE_FIELD=relevance_score`，
并保持`MODEL_RERANK_ENABLED=false`。不要重复拼接`/v1`或从模型名推断其它路由；
维度变更须重建索引。早期 450ms 预算的本地少量样本出现超时，不能单凭该探针决定预算。
[rerank 关闭组](../examples/rerank-off.dev.json)和
[rerank 开启组](../examples/rerank-on.dev.json)提供 dev 配置，唯一差异为 rerank
开关；两组显式设置 embedding 1.0 秒、Search 3.0 秒和 rerank 450 ms，
都启用全局模型，以便隔离 rerank 效果，schedules 均关闭。fixture
身份和 endpoint 为合成占位；部署前必须替换为你自己的已验证配置，并单独批准负载试验。

当前配置中，PROTECTED使用独立物理表，替代此前同表LeadingKeys隔离。
四个函数都由CDK绑定`SEMSEARCH_TABLE`及必填的`SEMSEARCH_PROTECTED_TABLE`，
两个名称必须不同；它们属于基础资源身份，不能由S3配置覆盖。缺少protected绑定、
误绑同表或protected记录不可验证时拒绝服务，不回退读取state表中的旧PROTECTED。
CONTROL、limiter、cache和progress继续只使用state表。

protected表以精确ARN授权：四个函数仅有`GetItem`，Indexer/Enhancer另有
`ConditionCheckItem`；运行角色均无该表的Put/Update/Delete权限，原PROTECTED的
LeadingKeys隔离已移除。state表仍按CONTROL/rate/cache/progress分别限制键和操作。
授权管理员来自独立外部管理渠道，stack不创建或授权该管理员；只有管理员可初始化、
更新policy epoch/ref或永久tombstone。后台CAS与protected条件检查使用同账号、同Region
的跨表事务，不能以catalog写权限改写安全授权。

## 分词配置迁移与召回验收

新构建采用 `synonyms-v2`：五组业务同义词进入已有 `TokenizerConfig.synonyms`，
在文档与查询两侧对称扩展，并随 BM25 对象及 manifest 摘要保存。修改词典或同义词
内容时递增对应版本。旧快照继续按持久化的原配置加载，查询阶段不自动重建或追加别名；
低层显式 `TokenizerConfig()` 仍代表空同义词的 v1 配置。`IndexSnapshot.build` 接受
可选关键字参数 `tokenizer`；Indexer 显式传当前配置，Enhancer 显式传输入快照分词器。
不能把新分词器装到旧 TF/DF 上冒充已迁移；新配置会改变词项、长度、统计及词法排序。
迁移遵循“旧快照不变，新索引重新验收”；新配置的质量变化需单独复审。

Indexer 在目录摘要相同时也读取当前快照并比较完整配置；版本或内容不同均走既有
base 发布，递增 catalog/safety/manifest revision。相同配置再次轮询只 reconcile，
不重复发布。增强任务保留输入快照的配置，同一目录代次更换配置会被拒绝；旧增强
发布由 CAS 拒绝。比较配置新增一次完整快照读取与校验，读取成本及验证边界见下文。
这一次加载仅在确认旧基础词典摘要或 jieba 版本与当前运行环境不兼容时允许重建。
先完成对象完整性、持久化统计、向量、目录及内外快照身份检查，再产生私有的可恢复
异常；Indexer 只捕获这一类别。验证用的临时分词器不会返回给搜索或用于发布。
未知快照格式、畸形配置、运行时词典损坏、对象完整性失配及存储读取失败均外抛，
不发布新代次，不丢弃现有向量。未知格式需要明确的迁移支持，不能假定任意格式都
可自动恢复。目录两次完整读取、target 校验、期限、授权与发布 CAS 检查仍必须通过。
瞬时读取错误由调用方处理，后续轮询可再次读取；当前实现没有为此增加自动重试或专用告警资源。
即使合法的旧配置差异与完整性、统计、向量或身份错误并存，也不得触发自动重建。

### 排序变化与复核

同义词扩展会改变文档词项、长度、统计和词法排序。均值改善不能证明每条查询都改善，
部署前应使用目标目录、固定授权集合和独立标注查询比较旧快照与重建快照，分别报告
BM25、RRF、向量优先及低相似度词法兜底的变化。合成回归见
[版本化召回测试](../tests/test_versioned_recall.py)。

当前五组成员数为 create/read/update/delete/product 的 5/4/4/3/3。
`bm25-v1` 将保留的同义词出现次数计入文档长度，因此较大的动作组会额外受到长度
归一化惩罚，即使查询不含动作词也可能影响排序。数学回归使用三份仅动作不同的合成
文档固定这一行为；这不是已经消除偏差的声明。

同义词仍受每文档 512 个词项、每示例 128 个词项及每词 3 次上限约束，扩展可能使长
描述尾部提前被截断。词表变更、文档长度和各语言的覆盖需要一起复核。
健康向量的低相似度词法兜底也使用快照内持久化的分词配置；这类排序变化与 provider
失败应分开记录。原始余弦不能由 hash mock 的分布推断。

### 稳态轮询读取与词典复用

配置比较需要读取和验证完整快照，部署前应测量对象数量、字节数、S3 时延和完整轮询
耗时，不能把本地替身测试当作线上预算证明。
切词闭包以实际业务词典内容作为有界缓存键；同词典的旧、新同义词版本可复用词典，
不同词典仍隔离，最多缓存 4 组，构造锁防止并发重复加载。同义词映射、版本标签、
快照校验与查询输出仍属于各自分词器实例，不能把新同义词装到旧快照上。

## 获授权后的部署顺序

1. 固定commit、锁文件、配置摘要、Lambda ZIP摘要和两区离线检查结果。部署负责人
   核验目标区域能力、配额工作表、预算、值班和真实探针完成状态，记录本次仅开放canary的主体。
2. 用获授权的部署身份准备/核验CDK bootstrap栈。bootstrap需要独立授权；不把此步骤
   隐藏在synth/测试脚本中。只允许本地锁定CDK CLI，禁止全局安装或未锁定`npx latest`。
3. 先将synth生成的四份非秘密配置JSON上传到已有versioned配置bucket；此步骤属于
   单独授权后的AWS写入。对象key取`.artifacts/configuration/manifest.json`，格式为
   `ACCOUNT/REGION/GATEWAY_ID/configuration/OPERATION/SHA256.json`。逐个使用`put-object`
   返回的实际VersionId，核对S3对象字节数/SHA-256与本地manifest，作为相应
   CloudFormation参数。不得填`null`、空版本或猜测值。上传配置不会初始化ACL或打开流量。
4. 将实际配置VersionId传入，在schedule和模型关闭的配置上运行CDK diff/change set，
   逐项审核替换/删除、IAM、外部role修改、Secrets引用、retention，
   以及计算配置（内存、超时、并发）与新发布版本。确认无Gateway/Target隐式创建或修改。
5. 批准change set后部署该stack。保存outputs、前一个版本的alias/配置/策略摘要，
   以及新旧计算配置和发布版本清单，供回滚与成本复核使用。
   CloudFormation完成不等于服务已就绪，此时无有效控制记录/ACL时服务应fail closed。
6. 对从未初始化的新Gateway，授权管理员使用[初始化接口](service.md#身份和授权)写不可变
   初始策略，并在同一个跨表事务中条件创建state表的`CONTROL`和protected表的`PROTECTED`。
   显式登记机器主体，初始机器allowed_tools可为空；普通canary主体须包含
   Search自身和获准业务工具的完整身份/schema hash。初始化使用条件写，已有记录或
   tombstone不能覆盖。管理员须分别具备两表精确ARN及对应S3 policy写入权限，
   该权限不交给运行角色。已有同表数据必须改走下方迁移流程，不能重新initialize。
7. Gateway管理员把Search target指向输出的Search `live` alias，并按真实descriptor
   配置schema；把REQUEST interceptor绑定到对应alias并配置所需header/context传递。
   execution role只invoke这两个精确alias。复核真实target ID/工具名/incarnation，
   变更后重新生成受控配置；绑定变更由已有Gateway的变更渠道执行。
8. 用已批准机器身份验证初始化、ping和完整分页，执行一次受控Indexer调用`{}`。
   核验所有target READY、前后fingerprint、全量digest、合法空目录/失败行为、S3
   VersionId/hash和DynamoDB条件提交。普通身份仍只得到单页Search bootstrap。
9. 开启60s轮询和5min增强schedule；模型仍关闭时Enhancer应记录`disabled`。按Gateway
   错开启用相位，验证schedule group trust、alias目标、0 retry、60s age和过期tick。
   不把Scheduler精度视作变更后60s内完成的承诺。
10. 通过下方验证清单后逐步开放已批准主体/流量。模型启用分开执行：确认代理契约及
   egress授权，发布增强版本，观察实际基础排序与降级；生成示例默认仍待审核，不自动进入索引。
   需要rerank时另做协议/质量/预算评审。

以下命令只属于**后续获授权的部署任务**，本会话不执行。先在该部署环境准备已审核的
AWS CLI和短期部署身份，并设置非秘密的目标区域、bucket、stack、change set及配置key
变量。key必须从本次manifest读取。以Search为例上传；其余三个operation按参数表逐个
执行并分别保存VersionId。使用`--if-none-match '*'`保护已存在的内容地址；若返回412，
核对已登记的同hash对象版本后复用，不盲目覆盖。

```sh
aws s3api put-object \
  --region "$SEMSEARCH_DEPLOY_REGION" \
  --bucket "$SEMSEARCH_CONFIG_BUCKET_NAME" \
  --key "$SEMSEARCH_SEARCH_CONFIG_KEY" \
  --body .artifacts/configuration/search.json \
  --server-side-encryption AES256 \
  --if-none-match '*' \
  --query VersionId --output text
```

`put-object`在versioned bucket中返回的VersionId就是部署参数值，详见
[AWS CLI参考](https://docs.aws.amazon.com/cli/latest/reference/s3api/put-object.html)。
四个版本验证完成后，使用本次已经检查过的assembly准备change set：

```sh
./node_modules/.bin/cdk deploy "$SEMSEARCH_STACK_NAME" \
  --app ".artifacts/cdk-$SEMSEARCH_DEPLOY_REGION" \
  --method prepare-change-set \
  --change-set-name "$SEMSEARCH_CHANGE_SET_NAME" \
  --previous-parameters false \
  --parameters "ConfigurationBucketName=$SEMSEARCH_CONFIG_BUCKET_NAME" \
  --parameters "SearchConfigVersionId=$SEMSEARCH_SEARCH_CONFIG_VERSION" \
  --parameters "RequestConfigVersionId=$SEMSEARCH_REQUEST_CONFIG_VERSION" \
  --parameters "IndexerConfigVersionId=$SEMSEARCH_INDEXER_CONFIG_VERSION" \
  --parameters "EnhancerConfigVersionId=$SEMSEARCH_ENHANCER_CONFIG_VERSION"
```

REQUEST 独立包的首次启用，审核必须同时满足以下正向条件，空变更集不算成功：

1. 本次 assembly 的 REQUEST 函数 `Handler` 是
   `semantic_service.request_runtime.request_handler`，`Code` 对应已核验的 REQUEST
   ZIP；共享 ZIP 摘要与本次获准保留的基线相同。
2. change set 确实包含 REQUEST 函数、发布版本和 `live` alias 的预期更新。
   若仅为调整已有预置并发容量，可只更新 REQUEST alias；纯 REQUEST 任务出现任何
   Search/Indexer/Enhancer、Gateway、IAM、告警或其他资源变化都停止并报告。
3. 固定配置引用及其余参数与获准部署状态一致。准备后不重新 synth 或替换 asset。

审核该change set与参数后，由获授权变更者执行它。
[`prepare-change-set` / `execute-change-set`](https://docs.aws.amazon.com/cdk/v2/guide/ref-cli-cmd-deploy.html)
为本地锁定CDK CLI的部署方法：

```sh
./node_modules/.bin/cdk deploy "$SEMSEARCH_STACK_NAME" \
  --app ".artifacts/cdk-$SEMSEARCH_DEPLOY_REGION" \
  --method execute-change-set \
  --change-set-name "$SEMSEARCH_CHANGE_SET_NAME" \
  --outputs-file .artifacts/deployment-outputs.json
```

执行后还要回读 REQUEST `live` alias 指向的已发布版本，不能只查看 `$LATEST`。
以下为目标环境的只读验证示例，函数名取本次输出登记：

```sh
aws lambda get-function-configuration --profile "$SEMSEARCH_AWS_PROFILE" --region "$SEMSEARCH_AWS_REGION" \
  --function-name "$SEMSEARCH_REQUEST_FUNCTION" --qualifier live \
  --query '{Version:Version,Handler:Handler,CodeSha256:CodeSha256,State:State,LastUpdateStatus:LastUpdateStatus}'
```

要求 `Handler` 仍为新入口、`CodeSha256` 等于本次 REQUEST ZIP SHA-256 的 Base64
编码，版本等于审核的 alias 目标，状态与最后更新成功。只有 handler/摘要/版本的
正向回读和 smoke 均通过，才记录已启用。预置并发大于零时另回读 alias 配置达到
`READY` 且可用数量符合配置；factory 初始化失败会使预置环境初始化失败。
OIDC 预取的普通失败允许 Init 成功，须按[Init 查询](operations-runbook.md#1-查看失败类别与请求范围)
单独回读 `oidc_prefetch.status/failure_kind`、可用的 `auth_failure` 与首请求日志，
并核对 `request-init-oidc-prefetch-failed` 指标；READY 不能代替预取成功证据。
修复固定版本配置需更新对应引用并建立新环境，不能依赖旧环境重新读取同 key。

发布代码asset、准备change set和执行change set都属于AWS变更；没有任何一步可用
离线 synth 的成功结果代替。更新模型/registry/撤销配置同样先生成新配置hash，
上传并取得VersionId，再让CloudFormation更新environment与Lambda version/alias。

## REQUEST 预置并发验证

示例默认 PC=0。启用独立 REQUEST 包及预置并发前，检查 alias 的 `DependsOn`，
必须先创建监控，再分配 PC。依赖应包含以下两个 `AWS::Logs::MetricFilter` 及两个
`AWS::CloudWatch::Alarm` 的实际 logical ID：

- `request-init-auth-recoveredFilter`
- `request-init-auth-recoveredAlarm`（5 分钟 `Sum > 2`）
- `request-init-oidc-prefetch-failedFilter`
- `request-init-oidc-prefetch-failedAlarm`（5 分钟失败次数 > 0）

审核完整 change set，确认资源与属性变化符合本次批准范围；意外变化时停止部署并重新核对。
不要把历史模板或固定资源数量当作当前栈的完整基线。

预取从 factory 成功后的入口起共用 3.0 秒 deadline，覆盖 discovery、JWKS、唯一共享
补偿及清理。此前 factory、模块导入和平台准备不在这份预算内。REQUEST 的 10 秒函数
timeout 不是预置并发 Init 的固定平台时限，详见[服务配置](service.md#冷启动分段计时)。

部署后分别测量正常首请求、Init 后超过 300 秒的首请求及冻结后的复用请求。
记录缓存年龄和 monotonic 计时，不承诺闲置后仍有预取收益。请求客户端默认
`keepalive_expiry` 为 5 秒；记录 `network/read`、`network/protocol`、补偿次数与延迟，
验证上游静默关闭连接的影响，不用离线替身测试证明真实连接池复用安全。

## 已有同表数据迁移

本节只适用于已有`CONTROL format_version=2`及`PROTECTED format_version=1`
同处state表的部署。没有自动迁移或兼容回读；更早的单CONTROL格式需要另行审阅转换，
不能直接套用此流程。以下AWS变更仅由后续获授权任务执行。

1. 批准维护窗口，阻断用户入口、Search直接调用和旧alias调用路径，关闭两个schedule，
   停止手工Indexer/Enhancer、发布/恢复任务及所有授权管理员写入。排空在途请求与
   异步投递，并确认旧实例不会再处理请求；关闭schedule本身不证明已经静止。
   迁移及验证完成前保持入口关闭。期间新发生的撤销需求必须由授权管理员纳入切换前
   的当前授权状态；不能因迁移窗口丢弃新撤销或解除已有撤销。
2. 在写入停止后强读state表的CONTROL与PROTECTED，核对Gateway identity、
   完整`data`、全部CAS列、policy对象VersionId/hash及安全目录引用。保存受控的恢复
   记录、state表备份定位及新表预定物理名；不把授权内容放入公开日志。必须能证明该PROTECTED是冻结时
   最新版本；证据不足则保持关闭，不拿历史备份补出“可用”授权。
3. 审阅并部署创建`-protected`表的change set，保留现有state表的物理身份及数据。
   核验新表的加密/PITR/deletion protection/RETAIN、无TTL和运行IAM。新函数即使先
   切到新绑定，也因新表尚无有效记录而拒绝服务；此时不能打开入口或schedule。
4. 独立迁移管理员对两表执行**一个**`TransactWriteItems`：ConditionCheck精确匹配
   冻结的CONTROL完整data及CAS列；Put原PROTECTED到新表并要求目标pk不存在；
   Delete旧表PROTECTED并要求其完整data及全部CAS列仍精确匹配冻结值。
   三项操作针对三个不同table/key地址，不对同一item同时做ConditionCheck和Delete。
   复制保留原pk、规范JSON、Gateway identity、`protected_version`、policy epoch/ref、
   tombstone及CAS列，不重置epoch/version、不清除tombstone、不调用initialize。
   临时迁移权限仅覆盖这两表和必要操作，迁移结束后移除。
5. 强读核验新表PROTECTED与冻结值完全一致、旧PROTECTED已不存在、CONTROL未改变，
   并使用新代码读取复合控制视图。事务冲突或结果不明时继续停服，重读三个地址判断
   是否整体提交；不得拆成无条件写重试、保留两份可写权威或忽略新表已有记录。
   与冻结值不一致时交授权管理员核对，不能择较旧版本继续。
6. 核验四个新Lambda版本都绑定正确的两个表，Gateway与Scheduler只引用兼容的新alias，
   旧代码/旧alias及管理员工具不可重新写入state表PROTECTED。用受控canary验证
   跨表CAS竞争、policy更新、撤销拒绝以及运行角色无protected写权限。永久tombstone
   竞争在获准隔离测试资源演练；生产只核对既有tombstone保留，不为试验写入或清除它，
   不恢复旧ACL。冻结期间收到的授权变更先应用并验证，
   再恢复管理员正常变更、schedule和入口。若当前Gateway已tombstone，始终保持关闭。

失败恢复优先修复当前跨表代码和绑定。任何阶段都不能把旧同表runtime重新放上线；
新表提交后也不能把旧PROTECTED复制回去作为快速回退。所有返回服务的决定必须以
最新授权和永久tombstone可证明保留为前提，详见[回退和保留数据](#回退和保留数据)。

## 事后验证清单

以下每项都需要获授权环境的真实结果，不能预填“通过”：

- [ ] 资源在正确account/region/stage，名称/标签正确；两区能力及目标runtime import已验证。
- [ ] S3 versioning、AES256、PublicAccessBlock、TLS deny有效；state/protected两表的
  加密、PITR、deletion protection、RETAIN和强一致读取生效；只有state有expires_at TTL，
  protected无TTL。四函数的两个表绑定精确且不同；配置对象按VersionId/hash/字节数加载，
  当前policy/safety不能被内容恢复覆盖。
- [ ] 四个运行角色独立；角色只访问本Gateway所需prefix/table键/secret；运行角色没有
  `PROTECTED`写、policy管理、target写、secret列举、任意Lambda invoke或部署权限。必要动态对象/日志
  stream通配与源码注释一致。
- [ ] protected权限只含该表精确ARN的GetItem（四角色）和ConditionCheckItem（两个后台）；
  state表没有遗留PROTECTED权威，运行配置不会回读旧表。跨表事务在授权更新或
  tombstone竞争时整体失败，初始化不能覆盖任一已有记录。
- [ ] Gateway execution role只invoke指定Search/REQUEST alias；未绑定、错误Gateway、
  target、tool、MCP ID值、query摘要或过期封套均拒绝，客户端封套被覆盖。
  外部与Indexer请求均只用字符串ID；numeric/null/boolean负例拒绝，字符串`"7"`
  与签名/context逐字匹配；CUSTOM_JWT、header及alias的生产签名链路完成真实往返。
- [ ] Search实际metadata及授权schema hash一致；无长度/additionalProperties关键字时，
  运行时仍拒绝空白或超过2048字符的query、超过8192字符或无效的封套、未知参数，
  并验证HMAC、context绑定和当前ACL。不能用Gateway创建成功代替这些负例。
- [ ] 普通list单页且无nextCursor；未授权主体、撤销token、ACL更新、同名重建及schema
  变化均不能泄露descriptor或模型候选。授权/安全读取失败应拒绝而非回退旧权限。
- [ ] March MCP版本协商成功，未批准版本仍拒绝。full listing两遍/实际分页成本已计入配额工作表；
  应按目标 catalog 的实际分页数计算，不能假设所有 catalog 采用固定页大小。
  缺页、重复cursor、target变更、45s超时
  保留上一完整版本；合法零业务工具发布空快照。上游DEFAULT MCP更新先sync并等待READY。
- [ ] 重复tick只reconcile；旧tick、未来tick拒绝；Indexer与Enhancer各reserved concurrency=1，
  CAS冲突不覆盖新revision；Enhancer在240s预算内保存cursor，下个正常tick恢复。
- [ ] 每个alias的异步0 retry/60s max age有效；Scheduler的60s/300s频率、OFF flexible
  window、0 retry/60s age和group trust生效。漏tick由后续完整读取恢复。
- [ ] 当前catalog/safety/ACL正常，内容刷新≤30s检查；新对象完整校验后交换。测Gateway
  可见变更到用户可检索的端到端分布，不用poll heartbeat冒充新鲜度。
- [ ] 模型关闭/不可用仍可BM25；embedding失败→BM25，rerank失败保留精确的实际基础顺序；模型配置/secret
  切换不混用向量空间，不泄露query/JWT/key/原始descriptor。
- [ ] Search普通对象按 MCP text JSON 响应契约解码，先检查JSON-RPC error和
  `result.isError`；HTTP 200不表示工具成功，不把本地异常种类当成已验证公开错误码。
  实际生产payload、Unicode/转义和interceptor开销另测；不把输入、Lambda提交及MCP
  包装输出视为对称6MiB上限，不放宽512KiB业务输出及6MiB本地list响应预算。
- [ ] key/JWKS正常轮换与紧急撤销通过；旧key仅在允许窗口内验签，未知kid刷新受控。
- [ ] 告警收到真实canary信号；无SNS配置时由值班明确监视CloudWatch，不假定有人接收。
- [ ] 总体/warm/cold/刷新延迟、错误率、RSS、load耗时、provider预算和账号配额通过评测/
  部署负载门槛，未达标先限制入口。完成代码回退、同revision内容恢复和tombstone演练。

## 告警与处置

CDK利用现有服务固定字段建立过滤指标：各handler调用/服务失败，实际检索模式
bm25/rrf/vector/vector_bm25_fallback/rerank，配置的融合模式、最终空结果、
门槛拒答、降级、向量可用状态及 rerank applied/disabled/skipped/failed，
embedding 错误的受控 `kind` 计数、成功完整轮询、expired tick、限流/认证失败、
REQUEST 元数据补偿恢复 `request-auth-recovered`、Init 预取恢复与失败及增强paused。
请求诊断按请求收集，
缓存 handler 不复用上次状态；沿用 stdout 和 CloudWatch metric filters，不增加 sink。
REQUEST 的 JWKS 逐次读取另用 `operation=oidc_fetch`、
`event=attempt_finished` 输出，字段见[逐次读取事件](service.md#jwks-逐次读取事件)。
事件来源按实际部署版本核对。排障时核对逐次事件与所属 `operation=request` 或
`operation=request_init` 摘要。请求指标仍只统计最终请求摘要，
新增 Init 恢复/失败指标只统计预取摘要，不按日志总行数计数。
原请求响应、超时、补偿额度及缓存 TTL 保持原约定，Init 预取另有总预算。
Search 日志另含 `embedding_error`：`null` 或含 `kind`、`http_status`、
`retry_after_seconds`、`rate_limit_headers` 的受控对象。
允许清单响应头按 provider 可控的有界整数写入，不保留原始头文本；
具体边界见[错误诊断](embedding-diagnostics.md)。
新增 embedding 错误指标按 `embedding_error.kind` 分类，指标值固定为 `1`，
不使用 provider 头值作指标值或维度。仅部署 Lambda 代码不会创建这些过滤器；
需部署相应 CDK，再核对实际日志与计数入库。
默认告警包括各函数throttle、
5min原生Lambda Errors>0、5min服务错误率>1%、连续3/10min无成功轮询、
后台AsyncEventsDropped>0、`request-auth-recovered`（5min REQUEST 元数据补偿恢复次数>2）、
`request-init-auth-recovered`（相同阈值）、
`request-init-oidc-prefetch-failed`（5min 预取失败次数>0），
以及Search Lambda执行p95>2s。服务错误率的分子包含
`internal failure`和`index not ready`，不把无效输入或未授权计为内部服务错误。
默认30天日志只保留固定 operation/status/duration 及上述有界诊断，包含 `embedding_error`。`ranking_mode`
始终表示实际完成的排序，检索前失败为 `null`；`configured_fusion` 单独表示配置，
初始化失败导致配置未知时为 `null`。`empty_result`、`below_threshold`、`degraded`
可以同时为真；最终授权检查造成的空列表也记为空结果。日志不含 query、描述符、
凭证、分数或原始 provider 错误，不扩展 Gateway 公共输出。

Indexer 的 `operation=indexer` 汇总另含下表八个整数计数，每次调用从零开始，
缓存客户端不会延续上次调用的计数。对应指标沿用服务 namespace 和 Indexer 日志组，
以字段值作为指标值，按时间窗口查看 `Sum`。这些过滤器在下次部署后生效，
不新增默认告警；部署后需核对实际汇总日志、指标入库和成功轮询信号。

| 日志字段 | CloudWatch 指标 | 计数口径 |
|---|---|---|
| `gateway_401_count` | `indexer-gateway-401-count` | Gateway 返回 HTTP 401 的次数，含第二次请求的响应 |
| `gateway_403_count` | `indexer-gateway-403-count` | Gateway 返回 HTTP 403 的次数，含第二次请求的响应 |
| `token_proactive_refresh_count` | `indexer-token-proactive-refresh-count` | 缓存令牌到刷新期限后启动主动刷新，失败尝试也计数；首次取令牌和 401/403 后强制刷新不计 |
| `gateway_replay_count` | `indexer-gateway-replay-count` | 401/403 后取得替换令牌并通过预算检查，实际启动第二次 Gateway 请求 |
| `gateway_replay_success_count` | `indexer-gateway-replay-success-count` | 第二次请求完成响应校验并正常关闭响应流 |
| `gateway_replay_failure_count` | `indexer-gateway-replay-failure-count` | 已启动的第二次请求发生网络、HTTP、响应校验或流处理失败 |
| `gateway_replay_skipped_count` | `indexer-gateway-replay-skipped-count` | 401/403 后因已知预算不足或刷新准入不可用而未启动第二次请求；包括未知 SDK 预算和刷新锁竞争 |
| `gateway_auth_refresh_failure_count` | `indexer-gateway-auth-refresh-failure-count` | 401/403 后换令牌失败；排除预算截止和刷新准入拒绝，不计首次取令牌或主动刷新失败 |

`gateway_replay_*` 中的 count/success/failure 三项仅统计实际启动的第二次 Gateway 请求，
满足 `gateway_replay_count = gateway_replay_success_count + gateway_replay_failure_count`。
未启动第二次请求时，这三项均不增加：预算或准入阻止恢复计入 skipped，替换令牌获取失败
计入 auth refresh failure，两者按原因区分。OAuth 端点的 HTTP 401/403 不计入 Gateway
状态计数。单次请求重放成功不代表整次 Indexer 发布成功，需同时查看汇总 `status` 和
`poll-success`；主动刷新次数也不代表刷新成功次数。

原生Lambda Errors补充捕获没有最终结构化日志的timeout、OOM和import失败，也会统计
代码主动抛出的输入/认证/授权拒绝。值班必须结合日志分类，不能把原生Errors直接
等同于可用性SLO中的内部故障率。

REQUEST 中实际执行的 discovery 或 JWKS 补偿 GET 正常返回后，才在请求局部
`ContextVar` 收集恢复标记，由 `request_handler` 通过现有字段通道输出
`auth_recovered=true`。它不从任意 `auth.*` 阶段聚合推断，也不要求最终
`status=ok`：补偿恢复后即使 JWT 过期、授权拒绝、限流或下游普通异常导致请求失败，
仍带该字段且没有 `auth_failure`。因此它表示元数据读取恢复，不表示客户端请求成功。
终局 OIDC 依赖或公钥文档错误带有 `auth_failure` 时、出现未正常处理的
`BaseException` 时，以及没有重试或仅命中缓存时，均省略恢复字段。
标记不跨请求、不进入 verifier 实例，仅 REQUEST 输出。
预置并发 Init 使用独立诊断作用域，完整预取成功后的补偿恢复位于
`oidc_prefetch.auth_recovered`；已有补偿正常返回但随后失败时仅保留
`oidc_prefetch.metadata_recovered`，不会把失败预取计入成功恢复。

`request-auth-recovered` 指标仅按顶层 `operation=request` 与
`auth_recovered IS TRUE` 过滤，每个恢复请求计一次。MetricFilter 与 Alarm 名均为
`<prefix>-request-auth-recovered`；指标位于 `semsearch/<prefix>` 命名空间，
名称为 `request-auth-recovered`，按同一部署范围隔离。告警使用 300 秒周期的
`Sum > 2`，即五分钟至少三次恢复，一个评估周期，缺失数据不告警。
这设置统计窗口和触发灵敏度，不按成功请求分母算比率，也不统计 HTTP 尝试次数；
统计窗口不等同于告警评估频率。通知沿用 `alarm_actions` 的既有 SNS 路由，
未配置通知目标时只记录告警状态，不新增 SNS 资源或订阅。

PC Init 的替代过滤器为 `<prefix>-request-init-auth-recovered`，按
`operation=request_init` 与 `oidc_prefetch.auth_recovered IS TRUE` 匹配，
在同一命名空间写入 `request-init-auth-recovered`，每条摘要计 `1`。
同名 Alarm 沿用请求恢复告警的 300 秒周期 `Sum > 2`、一个评估周期、
缺失数据不告警及相同 SNS 路由，Init 与请求计数分别评估。
另有 `<prefix>-request-init-oidc-prefetch-failed` Filter/Alarm，
按 `operation=request_init` 与 `oidc_prefetch.status=failed` 每条计 `1`，
同名指标以 300 秒周期 `Sum > 0` 告警，其他设置相同。
它覆盖顶层 `status=ready` 而预取失败的情况，并保留 `failure_kind` 用于日志分诊。
这四项资源须随 REQUEST 代码一起发布；共享栈的精确允许范围见
[REQUEST PC 部署方法](#request-预置并发验证)。

收到告警后按 `aws_request_id` 关联最终 `status`、discovery/JWKS 的 `count/errors`
及平台记录。恢复请求没有 `auth_failure`，仅凭最终摘要不能还原首次故障类别；
部署逐次读取事件后，按同一 UUID 和 `attempt` 查看 JWKS 的 `outcome`、`failure`、
`compensation` 与单次 `duration_ms`。首错事件在补偿等待前写出，耗时不含等待或
写日志；`outcome=ok` 不表示后续公钥或认证校验成功。没有事件时先核对部署版本、
是否命中缓存及日志是否完整，不能据此补造错误类别或认定没有故障。
后续业务失败仍由原生 Lambda Errors 和适用的服务错误指标覆盖，可能同时计入
恢复次数。终局带 `auth_failure` 的 OIDC 失败只计失败，不计恢复。
该恢复信号依赖最终请求日志，不覆盖缺失日志的进程退出。
Init 没有请求 UUID；按同一日志流和 Init 时间窗口关联
`request_init`、`phase=init` 的 JWKS 事件及 `INIT_REPORT`，查询见
[运维手册](operations-runbook.md#1-查看失败类别与请求范围)。
`failure_kind=structural` 先检查预取接线与程序故障，
`dependency` 检查 IdP/文档，`deadline` 检查预取总预算；
不把所有 `oidc_prefetch.status=failed` 自动解释为 IdP 故障。

值班注意：既有 unknown-kid 刷新失败后的冷却期，可能使合法新 kid 在随后约
30 秒内仍被拒绝，应结合刷新错误和密钥轮换记录判断。
25–75ms 短抖动仍可能让并发补偿触发上游 429；部署前应验证目标流量下的限流行为。

Lambda Duration 不含完整 Gateway 延迟。现有 `first_invocation`、`runtime_module_init_ms`
和 `stage_timings` 可帮助识别首次调用与快照加载，但冷启动仍须关联平台 REPORT 的
Init Duration。日志不能直接度量上游新鲜度、CAS 频率、provider 计费 usage、RSS 和
安全目录年龄，这些仍需专门测量。`vector_availability` 固定为 available/disabled/unavailable/failed/not_attempted；
将 failed、unavailable 与有意关闭模型分开判读，健康词法兜底及 rerank disabled
不算 provider 故障。rerank skipped 也应单报，不能与已尝试失败混为一谈。不要把这些初始告警
当作设计第9.1节全部SLO信号已经验收。
计划内关闭schedule时，poll heartbeat缺失告警可进入ALARM；值班以变更窗口记录该
预期状态，开启schedule后核验其恢复，不将其误报为已观察到的catalog新鲜度故障。

| 信号/场景 | 操作 |
|---|---|
| 原生Lambda Errors>0 | 先区分timeout/OOM/import与有结构化日志的主动拒绝；前者查资源、包与初始化，后者按输入/认证/授权类别处置。不把所有Errors都归为服务不可用 |
| 服务错误率>1% | 检查`internal failure`与`index not ready`；核对配置版本/hash、S3/控制记录、索引完整性和依赖。无有效索引时拒绝，不能以空结果或旧授权掩盖 |
| `request-auth-recovered`：5min 恢复次数>2 | 按请求 UUID 核对 discovery/JWKS 补偿及最终状态，排查重复依赖故障；恢复后业务拒绝也可能计数，不能将恢复次数当作请求成功数或错误率 |
| `request-init-auth-recovered`：5min 恢复次数>2 | 同样阈值，按日志流和 Init 窗口核对预取补偿及 JWKS 事件；不计入业务请求数 |
| `request-init-oidc-prefetch-failed`：5min 失败次数>0 | 检查 `oidc_prefetch.failure_kind/auth_failure`、部分恢复与首请求重读；Init READY 不代表预取成功 |
| 漏tick、poll失败/过期 | 检查Scheduler投递、Lambda并发/AsyncEventsDropped、IAM、IdP和target状态；修复后等待下一轮全量重读。持续>3min警告、>10min升级；不批量重放旧tick |
| Indexer Gateway 401/403 后重放成功 | 对照 `gateway_replay_success_count`、汇总 `status` 和 `poll-success`，区分已恢复的单次请求与整次轮询失败；持续拒绝时核对 Gateway 认证配置和 IdP |
| `gateway_auth_refresh_failure_count` > 0 | 401/403 后换令牌失败，检查 IdP 可用性、OAuth 配置及运行角色读取凭据的权限；不记录令牌或密钥值 |
| `gateway_replay_skipped_count` > 0 | 预算不足或刷新准入不可用，核对本次耗时、截止时间、Secrets SDK 超时/重试配置和刷新锁竞争；不能直接据此认定 IdP 故障。不放宽预算、不无限重试，等待后续正常调度 |
| `gateway_replay_failure_count` > 0 | 已启动的第二次 Gateway 请求失败，结合 401/403 计数和汇总状态核对认证、上游连通性与 MCP 响应；不要把它解释成换令牌失败 |
| CAS冲突 | 读取最新CONTROL/revision，确认正常Indexer/Enhancer竞争；废弃冲突产物并重试下一切片。不改无条件PutItem、不提升writer并发 |
| Enhancer paused/cursor停滞 | 核对当前revision、provider预算/429/deadline和缓存批次；恢复下一5min正常tick。目录revision变化使旧进度失效，基础BM25照常发布 |
| 上游目录不变 | 对DEFAULT MCP target检查发布流程是否Synchronize并等待READY；Indexer只读，不授target写权限补救 |
| 账号/Gateway限流 | 按配额工作表区分search、业务、bootstrap、分页及其它应用；在入口预算内削峰并申请额度。单subject limiter不隔离账号TPS |
| ACL撤销/泄露 | 授权管理员推进policy epoch/disabled/not_before；受影响入口必要时关闭。强读失败拒绝；确认越权立即最高优先级处置 |
| Gateway删除 | 独立确认删除后调用管理员tombstone流程，停止入口与schedule。永久tombstone保留；后续同名资源用新identity |
| RSS>60%或load p95>2s、目录>45s | 评测复现容量拐点，评估内存/模型体量或后端迁移；超出默认预算属于架构决定，不直接增加并发/放宽一致性 |

## 轮换与紧急撤销

模型/OAuth/signing分别轮换；密钥值始终由对应管理员处理。Secrets一般轮换不替模型
供应商创建/撤销key，供应商操作与Secrets version promotion须协调。模型认证失败
仅受控刷新一次并保持总deadline；后续按provider层级降级，不能无限刷新/重试。

```text
previous_window >= signer_secret_cache_TTL + envelope_TTL + max_clock_skew + safety_margin
390 seconds >= 300 + 30 + 30 + 30
```

signing正常轮换时，先准备新版本，在提升AWSCURRENT时写真实`activated_at`，保留旧版
AWSPREVIOUS至少上式窗口，并验证新旧kid在窗口边界的行为。任何相关配置变化必须重验
公式和CDK断言；不要把6分钟当作固定窗口。JWKS缓存最长300s，未知kid刷新有节流；
IdP正常key overlap至少覆盖已签token寿命和缓存传播预算，具体由IdP负责人验证。

泄露是紧急撤销：禁止延用previous宽限。同步更新Search/REQUEST的
`SEMSEARCH_REVOKED_KIDS` 并发布新版本、确认所有 alias 切换。当前 CDK 高层配置尚未分发
此字段，直接加入 `service_settings` 会被拒绝；需要先经评审补齐两端配置注入，不能当作
现成部署开关使用。详见[安全手册的紧急撤销](security-and-data-flow.md)。在旧实例仍可服务期间
停用受影响入口。只改变Secrets stage无法立即清除warm实例缓存。轮换恢复后重新跑
错误kid、过期封套和绑定负例，不记录任何key/JWT原文。

## 回退和保留数据

代码回退与内容恢复分别审批。先停止受影响流量、所有后台和管理写入并排空在途操作，
关闭两个schedule，记录当前alias、两表物理身份、配置对象VersionId/摘要及一致性控制
视图；保留protected表中最新policy/tombstone及state表中当前安全目录。对于服务代码回退，
使用前一个已验证ZIP/配置重新生成change set，把四个`live` alias恢复到兼容版本，
核验IAM和schema未倒退；不要删除stack作为回退手段。若服务基线更新过契约，必须先
验证兼容性，再切回旧包。可回退版本必须已经支持独立protected表与必填
`SEMSEARCH_PROTECTED_TABLE`，且保留当前两表绑定；旧同表基础设施及单CONTROL的服务包
均不兼容，不能用于快速回退。没有兼容已验证包时保持停服并修复，不回滚授权数据。

同catalog revision的内容/模型问题可使用保留的已验证快照，交由独立维护身份调用
`SnapshotStore.publish_enhanced(current, snapshot)`条件发布。该接口校验当前revision、
完整descriptor集合和当前safety身份/hash，写新不可变manifest后才CAS推进指针，
保留最新policy epoch、安全目录和tombstone；并发变化即失败并重新评估。恢复后验证
新reader载入及已撤销身份仍被拒绝，再恢复schedule与canary。

跨catalog revision的历史快照不能直接覆盖CONTROL/PROTECTED或把`catalog_revision`拨回：
服务的manifest与revision绑定。使用已知良好代码/模型profile，对**当前**已验证完整
catalog重建并正常发布；不使用历史ACL、安全目录或已删除tool恢复内容。不要手写
无条件DynamoDB更新、复制旧CONTROL/PROTECTED、恢复旧policy epoch，或清除永久tombstone。

S3 bucket、state/protected两表、可选 ECS 重放表及日志组（含可选 VPC Flow Logs）使用RETAIN；
各表启用deletion protection/PITR，protected无TTL，S3无自动时间删除规则。已发布Lambda versions
在函数存续期间保留，但删除整个函数会删除全部版本及 alias，Version 的保留策略不能保住它们。
删除stack不会删除全部保留数据，后续重建可能需要显式资源导入及IAM重连，不能盲目同名重建。

两表PITR独立，选相同时间点也不证明得到同一个事务视图；备份恢复先落到隔离表，
不能将任一历史表直接切为在线权威。内容恢复保持当前protected表绑定，并用当前
CONTROL/safety与最新授权重新校验和条件发布；不能恢复旧CONTROL来复活已删除工具。
若protected表损坏或丢失，停止入口和全部写入，由授权管理员依据可验证的最新授权
记录补齐备份之后所有policy变更和永久tombstone，验证policy对象版本/hash及单调
generation后，才可评审新的表绑定与恢复。无法证明最新状态、撤销完整性或tombstone
未丢失时继续拒绝服务；较旧PITR、旧同表影子item和重新initialize都不是安全替代。

运维GC须由独立角色执行，并保护active/previous、至少1h reader窗口以及“最近10个
成功版本或7天，取保留范围更宽者”。当前没有自动GC，默认保留更多版本；先由引用
关系和成功发布记录形成删除清单，再审核删除。不能设置简单7天lifecycle以替代
引用保护。跨区复制、灾备切换、SQS/CloudTrail提示与重放均是另行批准的扩展。
