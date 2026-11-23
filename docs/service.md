# 服务配置与存储边界

本文是运行配置、授权状态、调度和日志字段的详细参考，供开发者与运维使用。它解释[项目总览](overview.md)中的四个函数如何获得配置并共享一致的索引与权限状态。读完后，你可以核对字段来源、配置优先级、凭据轮换和阶段计时；操作步骤从[运维手册](operations-runbook.md)进入。

服务入口在 [runtime.py](../src/semantic_service/runtime.py)。这些是供部署流程使用的
Python Lambda handlers；[CDK 基础设施](deploy.md)创建每 Gateway 的 Search、REQUEST、
Indexer、Enhancer及各自最小权限角色。运行身份来自 Botocore 的 Lambda 凭证链；
代码不接受用户请求提供的账号、区域、Gateway 或 secret 名称。基础设施
增加按角色读取的版本化S3配置，并把授权/删除状态存入独立物理DynamoDB表，具体契约如下。

## 配置与入口

资源身份和secret引用保留在小型环境配置中；大型非秘密配置由S3提供。按需环境在
首次使用相应入口时初始化；REQUEST PC 环境在 Init 预建 factory 并预取 OIDC。
预置并发在示例中默认 0；ECS 后端另由默认关闭的 `ecs_search` 控制。
以下是合并后的服务配置名，CDK会将descriptor、registry、
OIDC及模型选项放入对应角色的S3配置，而不是把这些大对象直接放入Lambda环境：

| 配置 | 含义 |
|---|---|
| `SEMSEARCH_ACCOUNT` | 12 位部署账号 |
| `AWS_REGION` / `SEMSEARCH_REGION` | 北京或宁夏；后者可显式覆盖 |
| `SEMSEARCH_GATEWAY_ID` | 固定 Gateway ID |
| `SEMSEARCH_SEARCH_TARGET_ID` | 控制平面实际分配的 Search target ID |
| `SEMSEARCH_SEARCH_TOOL_NAME` | 完整前缀名称，例如 `discovery___search_tools` |
| `SEMSEARCH_SEARCH_INCARNATION` | 审核后绑定的 incarnation，不复用被删除身份 |
| `SEMSEARCH_SEARCH_DESCRIPTOR_JSON` | 实际 Search descriptor，必须与 Gateway schema/授权摘要一致 |
| `SEMSEARCH_BUCKET` / `SEMSEARCH_TABLE` | 版本化S3 bucket和保存CONTROL/rate/cache/progress的state表 |
| `SEMSEARCH_PROTECTED_TABLE` | 必填，保存PROTECTED的独立物理表；必须与SEMSEARCH_TABLE不同，两表均以string `pk`为分区键 |
| `SEMSEARCH_TOP_K` | 默认 5，上限 20，仅服务端配置 |
| `SEMSEARCH_MODELS_ENABLED` | 默认 `false`；`true` 才初始化模型配置 |
| `SEMSEARCH_FUSION_STRATEGY` | 默认 `vector_bm25_fallback`；只接受 `rrf`、`vector`、`vector_bm25_fallback` |
| `SEMSEARCH_MIN_VECTOR_SIMILARITY` | 可选，省略时不设拒答门槛；设定时为 [-1, 1] 内有限数的字符串 |
| `SEMSEARCH_MIN_VECTOR_MARGIN` | 可选，省略时不设拒答门槛；设定时为 [0, 2] 内有限数的字符串 |
| `MODEL_RERANK_ENABLED` | 默认 `false`；字符串 Boolean，模型关闭时仍验证该字段 |

服务默认 `SEMSEARCH_FUSION_STRATEGY=vector_bm25_fallback`、
`MODEL_RERANK_ENABLED=false`，两项拒答门槛均省略。运行时初始化、直接构造的
服务 handler 及 CDK 验证使用同一服务配置 parser。核心库 `SearchConfig` 的默认
`fusion_strategy="rrf"` 保持不变。词法兜底的选择阈值仍为 0.5；它决定基础排序，
不是拒答阈值。全局模型开关和既有 egress 限制保持独立，开启 rerank 不会绕过它们。
非法模式、非有限数、越界门槛或非法 Boolean 产生带字段名的配置错误，经既有
Gateway 错误处理返回，不把错误伪装成成功的空工具列表。

### 版本化 S3 配置

[deployment_config.py](../src/semantic_service/deployment_config.py)只读取一个精确S3
对象版本，五个引用字段必须同时出现在Lambda环境中：

| 环境变量 | 含义 |
|---|---|
| `SEMSEARCH_CONFIG_BUCKET` | 已有private/versioned配置bucket，与索引bucket可以不同 |
| `SEMSEARCH_CONFIG_KEY` | `account/region/gateway_id/configuration/operation/sha256.json`，必须与固定身份、operation和hash逐字匹配 |
| `SEMSEARCH_CONFIG_VERSION_ID` | 实际对象VersionId；空值和`null`拒绝 |
| `SEMSEARCH_CONFIG_SHA256` | 小写64位SHA-256 |
| `SEMSEARCH_CONFIG_BYTES` | 预期UTF-8字节数，范围1–1,048,576 |

对象是JSON v1，顶层字段恰好为`version`、`gateway`、`operation`、`settings`。
`gateway`为当前`account`、`region`、`gateway_id`，`operation`必须精确对应
`search`、`request`、`indexer`或`enhancer`。`settings`是允许配置名到字符串的映射；
其中descriptor、registry或extra-body仍是JSON字符串。资源身份、secret引用及任何
secret值都不能从此对象覆盖，包括两个DynamoDB表绑定。重复JSON key、非有限数字、未知配置名、非字符串配置
值、错误版本/身份/operation及超过1MiB均拒绝。

新增三项检索设置仍使用 version 1 和字符串值。S3 中显式提供的值覆盖同名环境变量；
旧 v1 配置缺少新字段时使用合并后配置或上述默认值。未在 S3 或环境中设置的门槛
保持 unset，不能写成 JSON `null`、空字符串或字符串 `"None"` 代替省略。

```json
{
  "version": 1,
  "gateway": {
    "account": "111122223333",
    "region": "cn-northwest-1",
    "gateway_id": "fixture-gateway"
  },
  "operation": "enhancer",
  "settings": {
    "SEMSEARCH_MODELS_ENABLED": "false"
  }
}
```

这是synthetic结构示例，不是可部署身份。CDK用同一parser在synth前验证配置，并按
角色生成实际文件及manifest；发布和五个CloudFormation参数见[部署runbook](deploy.md)。
runtime用`GetObject`的`VersionId`参数读取，核对响应VersionId、ContentLength、
实际读取字节数及SHA-256，再建立只读mapping；不会修改`os.environ`。成功配置按
reference/Gateway/operation在warm进程中缓存，变更应发布新的引用及Lambda version。

只要五个引用字段中出现任意一个，就必须完成整组校验和S3加载；缺字段、S3失败或
内容错误均拒绝，不能退回环境中的旧配置。只有五个字段**全部缺席**时才保留显式
环境配置的服务调用方式。此兼容方式不代表旧DynamoDB存储格式也能自动兼容。

Search descriptor 的 `inputSchema` 按设计第2.2节使用Gateway支持的metadata子集：
根为`type/properties/required`，`query`为string，`_auth_context`为可选string并保留
服务端管理说明。metadata 子集不要求保留`minLength/maxLength/additionalProperties`，
配置与授权schema hash使用实际描述符，不补回这些关键字。
`inputSchema` 内的说明文字也参与授权摘要，必须原样保留。当前部署校验只接受已批准的
metadata 或完整约束形状：根和 `query` 不带说明，`_auth_context.description` 精确为
`Server-managed signed context. Clients must omit this field.`，缺失、改写或增加说明均拒绝。
客户端可见的 schema 因此较宽松，不能依赖 `tools/list` 的长度关键字做前置校验；
服务端仍检查全部限制。不得为补回客户端约束而修改配置中的实际回读 schema，否则摘要
不匹配会拒绝授权；改变 Gateway 工具定义需另行授权。
可选 `_auth_context` 由入口删除客户端值后覆盖。不要让客户端填写身份信息。
运行时强制非空白query、最多2048 Unicode字符、封套最多8192字符，拒绝未知参数；
HMAC验签、当前ACL及工具名、Gateway、target和MCP request ID绑定仍为必检项。
外部客户端和内部 Indexer 的请求 ID
只接受字符串；numeric、null、boolean均拒绝，不做字符串强转。通知不带 ID。
Search封套进一步要求ID非空、最多256字符，并与Lambda context逐字相等。
本服务的外部与后台 MCP 请求只接受字符串 ID，不在字符串和数字之间转换。
REQUEST 接受缺席或 null 的 gatewayResponse，拒绝已填充的响应；覆盖客户端的签名上下文后继续验证。
客户端依次检查 HTTP、JSON-RPC error 和 MCP isError，再解析 text JSON 中的 tools。
这些契约由合成测试覆盖，实际 Gateway、IdP、alias 和签名链路仍需在目标环境验收。

## 身份和授权

REQUEST 使用以下显式配置，不从 JWT 中提供的 URL 下载密钥：

| 配置 | 含义 |
|---|---|
| `OIDC_ISSUER`, `OIDC_AUDIENCE` | 精确 issuer 与 audience |
| `OIDC_DISCOVERY_URI`, `OIDC_JWKS_URI` | 已核验的公网 HTTPS 地址，discovery 值必须匹配配置 |
| `OIDC_ALLOWED_HOSTS` | 逗号分隔的精确主机 allowlist |
| `OIDC_ALGORITHMS` | 默认 `RS256`，只支持显式非对称算法 |
| `OIDC_SCOPE_CLAIM`, `OIDC_GROUPS_CLAIM` | 默认 `scope`、`groups` |
| `OIDC_TOKEN_USE_CLAIM`, `OIDC_TOKEN_USE` | 默认要求 `token_use=access`；须按已验证 IdP claim 配置 |
| `SEMSEARCH_MACHINE_PRINCIPALS_JSON` | `[["issuer","subject"]]`，只允许机器初始化、ping、列目录 |
| `SEMSEARCH_SUBJECT_CALLS_PER_MINUTE` | 默认 600，固定分钟计数 |
| `SEMSEARCH_MACHINE_CALLS_PER_MINUTE` | 默认 600，机器独立上限 |

Token lifetime 不超过 300 秒，时钟偏差最多 30 秒；JWT scopes/groups 只是与服务端
策略匹配的输入，不直接授予工具权限。每个请求读取强一致控制指针，策略包含完整
身份、已批准 schema hash、服务端禁用开关和 subject `not_before`，支持紧急撤销。

策略 JSON v1：`version=1`、`epoch`、`tools`、`principals`。`tools` 是完整名称到
`{"id": ToolIdentity.key, "schema_digest": tool.schema_digest}` 的映射；身份 key 是
`[account, region, gateway_id, target_id, incarnation, full_name]` 的规范 JSON 字符串。
`principals` 的每项必须包含 `issuer`、`subject`、`scopes`、`groups`、`allowed_tools`
（身份 key 列表）、`allow_query_egress`、`not_before`（epoch 秒）、`disabled`。
未列出的主体默认拒绝。机器身份也必须有策略项，可使用空 `allowed_tools`。

管理流程用`SnapshotStore.initialize(policy)`创建不可变初始策略，并在一个跨表事务
中创建state表的CONTROL及protected表的PROTECTED；
以后使用`update_policy(expected, policy)`，epoch严格加一且事务条件写。只有授权
管理员可调用。
新目录尚未发布时，已批准机器可列目录，普通身份仍被拒绝。Indexer 收录 Search
到安全目录但排除检索文本。普通主体须显式获得 Search 自身权限才能 bootstrap。

### CONTROL 与 PROTECTED

每Gateway有独立的state和protected两张物理表，均使用
`account/region/gateway_id/` namespace。当前配置中，
物理分表替代此前同表PROTECTED的LeadingKeys隔离；内容表其余键的限制仍保留。

| 表 / Item后缀 / 格式 | 数据与写入方 |
|---|---|
| `SEMSEARCH_TABLE` / `CONTROL`，`format_version=2` | 内容manifest、catalog revision/digest、安全目录revision/ref、对账时间和manifest version；Indexer/Enhancer仅可更新自己的内容状态 |
| `SEMSEARCH_PROTECTED_TABLE` / `PROTECTED`，`format_version=1` | policy epoch/ref、永久tombstone和单调递增的`protected_version`；只有授权管理员可写 |

state表还保存rate、enhancement-cache及enhancement-progress。两表分别使用
AWS_MANAGED KMS加密、on-demand、PITR、deletion protection与RETAIN；只有state
配置expires_at TTL，protected表没有TTL。SnapshotStore和ServiceSettings均要求
显式提供不同的两个表名；缺少绑定或同表绑定立即拒绝，不回退到共享表布局。

两个item的`data`为规范JSON，并带相应CAS列；读取时要求CAS列与JSON内容完全一致。
`read_control()`依次强读`PROTECTED → CONTROL → PROTECTED`，前后受保护记录不一致
就拒绝该次读取，不使用混合视图或旧授权。一次控制视图消耗三个强读，完整调用链
还会重新授权；[配额工作表](quotas.md)要求按实际次数计量，不继续沿用拆分前的读成本估算。

后台发布使用同账号、同Region的一个跨表DynamoDB事务：在protected表检查
`PROTECTED`的预期完整值、protected version、policy epoch及`tombstoned=false`，
同时条件写state表`CONTROL`。四个运行角色仅对protected表精确ARN获`GetItem`，
后台两角色另有`ConditionCheckItem`；没有该表Put/Update/Delete权限，也不再用
LeadingKeys分离PROTECTED。条件冲突使整次事务
失败，不能覆盖更新的授权或让已删除Gateway复活。

初始化在一个跨表事务中以`attribute_not_exists(pk)`创建两个item。管理员更新策略时，
先写不可变policy对象，再用一个事务检查state表当前`CONTROL`并条件替换protected表
`PROTECTED`；
policy epoch和protected version各加一。独立确认删除后调用`tombstone(expected)`，
只提升protected version并置永久tombstone，保留policy epoch/ref和内容状态。
管理员与后台的事务相互检查，不能通过内容回退改变受保护状态。

管理员从独立外部管理渠道获得两表精确ARN及不可变policy对象的必要权限；
CDK不创建该管理身份，不向运行角色授予其权限。

旧共享表中的PROTECTED和更早单CONTROL格式均不自动升级或回读。新protected表
缺记录、数据格式不一致或无法证明当前授权时fail closed；旧表留有数据也不成为fallback。
已有同表数据需按[迁移runbook](deploy.md#已有同表数据迁移)停入口、schedule、
全部后台/手工/授权写入并排空在途操作，条件事务同时检查CONTROL、复制最新
PROTECTED到新表并删除旧item。完整data、CAS列、epoch/version与永久tombstone保持，
不以initialize覆盖既有CONTROL。旧单CONTROL格式另行评审转换。

回退只允许支持两个显式表绑定的兼容代码，并保留当前protected权威。两表独立PITR
不证明历史备份是同一事务视图；恢复先隔离校验，再依据最新授权/撤销/tombstone
重建可证明的状态，不能直接换成旧表。无法证明当前授权完整时保持停服，
不恢复旧ACL、重置generation或删除tombstone。

## Secrets 与模型

运行时支持 `SEMSEARCH_REVOKED_KIDS`，但当前 CDK 配置生成器未将它分发给 Search 和 REQUEST；直接加入 `service_settings` 会被拒绝。紧急撤销先停用受影响入口，再经评审补齐同步配置注入与发布，详见[安全与数据流](security-and-data-flow.md)。

仅调用指定 secret 的 `GetSecretValue`，不使用 `ListSecrets`。三类 secret 必须不同：

| 环境变量 / 默认名 | JSON 字段 |
|---|---|
| `MODEL_SECRET_ID` / `semsearch/model-api` | `base_url`, `api_key` |
| `SEMSEARCH_SIGNING_SECRET_ID` / `semsearch/signing` | `key_b64`（至少 32 字节随机 HMAC key 的 base64）, `activated_at`（实际版本提升的 UTC epoch 秒） |
| `SEMSEARCH_OAUTH_SECRET_ID` / `semsearch/indexer-oauth` | `client_id`, `client_secret` |

值仅在内存中使用；repr、日志、错误不含 key。缓存最长 300 秒。模型 401 只受控刷新
一次并保留原 deadline；embedding 持续失败降级为 BM25，rerank 持续失败保留实际
基础排序。签名以 VersionId 为 kid，
未知 kid 有刷新节流；AWSPREVIOUS 窗口至少为 cache TTL + envelope TTL + skew +
safety margin，默认 390 秒。提升版本必须更新真实 activated_at。`SEMSEARCH_REVOKED_KIDS`
当前不随配置分发，不能作为即时可用的撤销开关；紧急事件先停用受影响入口，
再按本节开头的流程处理配置发布，不能等待旧缓存。

启用模型后，必须显式配置`MODEL_ALLOWED_HOSTS`；`MODEL_EMBEDDING_DIMENSIONS`默认1024，
对应当前代理已测空间，部署配置仍应记录此值。维度不从模型名称推断。
可选 `MODEL_BASE_URL` 覆盖 secret 的 base URL；API key 永远
只取 secret 字段，不读取环境中的 API key。配置变更应重新启动实例，变更模型空间时
完整重建索引。warm 实例拒绝 secret 悄悄切换 endpoint，避免混用模型空间。

| 选项 | 默认 |
|---|---|
| `MODEL_EMBEDDING_MODEL`, `MODEL_EMBEDDING_PATH` | `text-embedding-v4`, `/embeddings` |
| `MODEL_CHAT_MODEL`, `MODEL_CHAT_PATH` | `qwen-plus`, `/chat/completions` |
| `MODEL_EMBEDDING_REVISION`, `MODEL_CHAT_REVISION`, `MODEL_CONFIG_VERSION` | 显式配置身份 `configured-v1`, `configured-v1`, `v1`，不是供应商不可变版本承诺 |
| `MODEL_EMBEDDING_TIMEOUT_SECONDS` | 1.0 秒，query 的核心子预算及 HTTP 上限；允许 `(0, 6]` 内有限秒数；文档使用 `MODEL_DOCUMENT_TIMEOUT_SECONDS=30` |
| `SEMSEARCH_SEARCH_TIMEOUT_SECONDS` | 3.0 秒，Search 总预算；允许 `(0.3, 6]` 内有限秒数；provider 截止时间取配置期限与 Lambda 剩余期限减 0.3 秒的较早者 |
| `MODEL_CHAT_TIMEOUT_SECONDS` | 30，每次尝试；总时间受 240 秒切片约束 |
| `MODEL_EMBEDDING_MAX_BATCH`, `MODEL_EMBEDDING_MAX_TEXT_BYTES` | 10、8192，后者是 UTF-8 字节预算 |
| `MODEL_EMBEDDING_SEND_DIMENSIONS`, `MODEL_EMBEDDING_SEND_FLOAT_FORMAT` | `false`；启用前确认配置的 endpoint 支持这些字段 |
| `MODEL_EMBEDDING_EXTRA_BODY`, `MODEL_CHAT_EXTRA_BODY` | JSON 对象，默认 `{}`，不能覆盖受保护字段 |
| `MODEL_RESPONSE_ALIASES`, `MODEL_VALIDATE_MODEL_ECHO` | 精确别名列表为空、回显校验 `true` |
| `MODEL_RERANK_ENABLED` | `false`；获准开启时使用下列当前代理profile，其他endpoint须覆盖并验证 |
| `MODEL_RERANK_PATH`, `MODEL_RERANK_ENVELOPE`, `MODEL_RERANK_SCORE_FIELD` | `/rerank`、`flat`、`relevance_score`；路径附加到已含`/v1`的base，另两项指定codec |
| `MODEL_RERANK_MODEL`, `MODEL_RERANK_TIMEOUT_SECONDS` | `qwen3-rerank`、0.45 |

通用 `HTTPConfig.timeout_seconds` 默认 `1.0` 秒，也影响直接构造的
`OpenAICompatibleConfig`、`BailianConfig` 以及使用默认传输的低层 rerank 调用。
服务层为各阶段显式构造独立传输，因此 rerank 仍为 0.45 秒，document/chat 仍各为 30 秒。
表中是当前源码默认；各已部署函数仍须核对实际版本与配置。固定版本 S3 显式 `0.45`/`1.5` 秒仍优先，
只有 S3 和环境变量均省略字段时才使用新默认。配置优先级与预算边界见
[预算说明](embedding-diagnostics.md#可配置预算与授权收尾)。

部署示例显式指定 `MODEL_EMBEDDING_DIMENSIONS=1024`、batch 为 10。
改变维度须完整重建，并确认配置的服务支持对应请求参数。
示例模型配置使用保留 `/v1` 前缀的 base URL；embedding/chat/rerank 路径分别为
`/embeddings`、`/chat/completions`、`/rerank`。各模型的维度、批量、长文本、JSON 输出及 usage
必须按实际服务验证，不能以模型名称推断传输协议。rerank 默认关闭。



全部模型网络调用使用独立预算的 httpx client，`trust_env=False`，不自动发现 endpoint。
生成固定请求 `json_object`，本地强校验、最多一次 schema 修复重试。默认生成示例保留
为待审核缓存；只有显式提供带 `review_version` 的可信回调选择已生成文本后，才会进入
检索。失败不影响基础目录，embedding 仍可索引真实描述。

`CatalogEnhancer` 调用 LLM 生成并本地验证示例。默认将生成结果存为待审核
缓存，仅真实描述进入向量索引；要接入已审核示例，显式提供版本化 review 回调。
向量缓存另绑定实际批准文本及顺序的摘要；即使 `review_version` 不变，重新选择、
撤回或调整顺序也会重新匹配缓存，生成结果仍可复用。发布前复核已处理工具的当前
选择；内部缓存/进度采用新版本键，不复用旧版仅按审核版本绑定的向量。
仅生成内容的 JSON/schema 错误允许一次带固定修复指令的重试，提示版本为
`tool-queries-v2`；核心 adapter 不对模型身份、usage、HTTP 或响应外壳错误做 schema
修复，也不回显原始响应。运行时仅对 401/403 认证失败执行一次受控 Secrets
刷新重试，并保留原始总 deadline。

## 目录与调度

`SEMSEARCH_REGISTRY_JSON` 是条目数组，每项为 `name`、`target_id`、`incarnation`、
`role`（business/discovery）。名称必须来自实际 catalog；target ID 来自控制平面，
不从名称猜测。Search 注册为 discovery，其他配置的 discovery target 也排除检索；
原生 search 工具始终排除。READY 检查还验证实际 target ID 与名称归属，旧注册不能使
同名重建工具继承权限。未知工具不发布，需注册后重读。schema hash 改变需重批授权。

固定 SDK 尚未建模 MCP server target 的 `listingMode` 和 `mcpToolSchema`。
已按用户选择通过 Botocore 客户端的公开 `provide-client-params` / `after-call` 钩子，
仅为 `GetGatewayTarget` 保留原始响应里的这两个官方字段；不修改 SDK service model。
适配器将响应绑定到本次请求的 Gateway ARN、target ID，并核对 SDK 已解析的 endpoint
及字段值。不同请求不共享原始响应，未知字段不复制，错误响应保留 SDK 处理。

接受显式 `DEFAULT`，或明确有效的静态 `mcpToolSchema` union（`inlinePayload` 字符串
或 `s3` 配置）。DYNAMIC、未知/畸形模式、无法验证模式或静态配置均拒绝；不把缺失
字段推断为 DEFAULT。所有已观察字段参与前后 fingerprint；工具描述符仍逐项校验。
原始响应只在内存中处理；重复 JSON key、非有限数字、字段冲突或超过 16 MiB 均拒绝
兼容层解析。这个大小限制仅约束再次解析，botocore 在钩子之前已缓冲并解析 HTTP，
不是初次网络缓冲上限。适配器只恢复明确支持的字段；缺失 listing-mode 来源或 DYNAMIC 模式仍拒绝进入共享目录。测试从锁定 SDK 构造缺字段模型。

依据：[官方 target 配置](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_McpServerTargetConfiguration.html)、
[静态 schema union](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_McpToolSchemaConfiguration.html)
和 [SDK 事件扩展说明](https://docs.aws.amazon.com/boto3/latest/guide/events.html)。
该说明使用 Boto3；运行实现直接使用其底层 Botocore 客户端的相同事件接口。

机器 OAuth 另需 `OIDC_TOKEN_ENDPOINT`、`OIDC_MACHINE_SCOPES`；Gateway 地址由
`SEMSEARCH_GATEWAY_URL` 和 `SEMSEARCH_GATEWAY_ALLOWED_HOSTS` 指定。
`SEMSEARCH_MCP_VERSIONS` 默认 `2025-03-26`，只接受列表内的协商结果；支持 JSON
及 SSE 响应。opaque cursor 全部分页，重复 cursor、冲突名称、缺页或超时均拒绝。
缺少一致性 token 时完整读两遍并比对 digest，同时核对前后 READY fingerprint。
分页按 opaque nextCursor 顺序读取直至结束，拒绝重复游标和不完整目录；不能假设固定页大小。

调度由基础设施配置：Indexer 默认每 60 秒、45 秒预算，Enhancer 每 300 秒、240 秒预算；
两个后台函数各 reserved concurrency=1。`SEMSEARCH_POLL_SECONDS` 默认 60（不自动创建
Scheduler）。调度传递 `time`，超过 60 秒或来自未来的 tick 丢弃；受控直接调用可用 `{}`。
Scheduler/Lambda 异步重试默认均为 0，最大事件年龄 60 秒。发布流程负责上游 sync，
Indexer 不调用 Synchronize 或修改 target。缺失 tick 由下一轮全量重读恢复。

S3 必须开启 Versioning、加密和禁止公开访问。不可变键使用条件 PUT，每个对象按
VersionId/字节数/SHA256 回读校验；只有全部成功且仍在预算内才 CAS 更新活动指针。
同 digest 仅更新对账时间，不改 revision；同 revision 增强会保留最新对账时间。
删除 Gateway 后经独立确认写永久 tombstone，旧发布不能复活。当前策略变更不会回滚。

控制视图每请求按三个强读校验；内容manifest最长30秒检查一次，安全/策略变化触发同步刷新。
代码不因内容超过 30 分钟自动停服，仍要求当前安全目录与授权可验证。无效索引、身份
或策略不能降级为全目录或伪装成空结果。

核心 `SearchEngine.search` 和服务 `SearchHandler.__call__` 可接收 keyword-only
`diagnostics` 回调，内容为 `SearchDiagnostics`。它不改变 `SearchResult` 的形状、
相等性或现有 `observer(mode, abstained)`。runtime 为每个请求独立收集诊断；
handler 缓存不会复用上次请求的结果。现有结构化 stdout 日志和 CloudWatch metric
filters 使用以下字段，不增加日志目的地，也不改变 Gateway 的 `{"tools":[...]}`：

| 字段 | 含义 |
|---|---|
| `configured_fusion` | 配置的融合策略，与实际排序分开记录；初始化失败而配置尚未知时为 `null` |
| `ranking_mode` | 实际 `bm25`、`rrf`、`vector`、`vector_bm25_fallback` 或 `rerank`；未完成排序时为 `null` |
| `empty_result` | 最终成功响应的工具列表为空，包含返回前撤权导致的空列表 |
| `below_threshold` | 可选余弦或分差门槛触发拒答，不等同于所有空列表 |
| `degraded` | 所需检索能力不可用或失败；可与拒答、空结果同时出现 |
| `vector_availability` | 固定枚举 `available`、`disabled`、`unavailable`、`failed`、`not_attempted` |
| `rerank_state` | 固定枚举 `applied`、`disabled`、`skipped`、`failed` |
| `embedding_error` | `null` 或含 `kind`、`http_status`、`retry_after_seconds`、`rate_limit_headers` 的受控错误对象；详见[错误诊断](embedding-diagnostics.md) |
| `embedding_timing` | `null` 或查询内的剩余预算和最多两次 HTTP 出口计时；TCP 含 DNS，分别记录 TLS、完整响应头、首个应用正文块及正文耗时；详见[计时语义](embedding-diagnostics.md#embedding-阶段与-http-出口计时) |
| `lambda_deadline_ignored` | 请求内独立记录的布尔诊断标志，默认 `false`；`true` 表示期限源校验失败后忽略该 Lambda 剩余时间并沿用配置期限，不改变最终 `status` |

日志默认 30 天留存。允许清单响应头按 provider 可控的有界整数写入，不保留原始头文本。
新增 embedding 错误指标仅按受控 `kind` 分类计数，指标值固定为 `1`，
不把头值用作指标值或维度；过滤器须部署相应 CDK 后生效。

`search-lambda-deadline-ignored` 匹配 `operation=search` 且
`lambda_deadline_ignored=true` 的日志，每条固定计数 `1`，包括最终 `status=ok`
的请求。过滤器须部署相应 CDK 后生效；当前未配置该指标的告警阈值。

诊断中的 `ranking_mode` 指本请求最近完成的排名：授权集合在排名前已为空时为
`null`；排名完成后再全部撤权，保留最近完成的模式。它与 `SearchResult` 中为兼容
而保留的空集 `bm25` 模式区分。`rerank_state=applied` 记录阶段成功；若随后撤权
触发基础排名重算，最终排名模式仍以 `ranking_mode` 为准。服务只有在最终成功响应
构造完成后才设置 `empty_result`；核心返回空集后若服务授权检查失败，不会被记作
成功空响应。

健康的词法兜底、有意关闭模型及关闭 rerank 不计为 provider 故障。启用 rerank
但本次没有执行时记 `skipped`；执行失败后保留精确的基础顺序。检索前的配置、
身份或索引失败没有实际排名，`ranking_mode=null`；不能把它们计为成功 BM25。
日志不含 query、JWT、secret、描述符、分数、原始 provider 错误或隐藏工具统计。

### 冷启动分段计时

jieba 的基础前缀词典在构建阶段生成，wheel 和 Lambda ZIP 均携带同一份固定摘要的
JSON 产物。运行时直接读取并校验，不调用 jieba 初始化或写入临时词典缓存；
产物缺失、损坏或安装的基础词典不匹配时明确失败，需要重建安装包。
词典预编译只改变初始化方式；业务词条和同义词由每份快照的分词配置决定。
相同业务词典内容在进程内共享已初始化的切词器，最多缓存 4 组词典；
同义词、版本标签和快照配置仍由各分词器实例分别保存。
固定词典资源使用两列数组存储词条和词频，解码阶段包含词频表恢复；分词配置与
持久化快照格式保持不变。运行客户端直接使用 Botocore，可测收益来自避免导入
Boto3/s3transfer；旧 Boto3 也共享 Session，Session 缓存本身不是新增收益。
共享包仍保留这些依赖。REQUEST 独立包的裁剪范围见[独立包构建](developer-guide.md#request-独立包)。
User-Agent 由 Boto3 前缀变为 Botocore，
协议等价测试不比较此字段；外部日志查询若过滤 Boto3 前缀需同步调整。
配置阶段包含首次 SDK 初始化和 S3 客户端创建，共享预热记为 `runtime.aws_session`，
服务子阶段只计其后的客户端构造。基础词典摘要、jieba 导入和词典恢复分别计时，
直接构造与快照恢复使用相同口径，阶段名称见下表。
阶段计时包含子阶段，不能把配置和 AWS 客户端阶段直接相加。
运行时按需导入检索和后台索引模块，REQUEST 路径不加载这些模块。

REQUEST 专包入口为 `semantic_service.request_runtime.request_handler`。
仅当 Lambda 函数名、该 handler 名及 `provisioned-concurrency` 标记同时匹配，
才在模块 Init 中构造配置和客户端；普通本地导入与按需模式不构造、不产生 Init 遥测。
factory 成功后在父进程中创建独立 HTTP 客户端及独立 verifier，预取
discovery/JWKS；预取复用请求路径的刷新和公钥校验逻辑。独立客户端禁用环境代理、
重定向及保留的空闲连接，并在预取结束时关闭，不预热请求客户端的 HTTP 连接池。
signing secret 仍按需读取。成功后沿用 300 秒 TTL，失败不缓存；
discovery/JWKS 共享一次补偿，后续认证拥有独立的补偿额度，
未知 kid 冷却不被预取消耗。已有有效缓存时重复预取直接返回。

预取入口使用单调时钟设置 **3.0 秒**总预算，默认前 **2.75 秒**供客户端构造、
discovery、JWKS、抖动、补偿及尝试日志使用，保留最多 **0.25 秒**清理客户端。
各步骤共用绝对截止时间，清理不重新获得一份预算。仅预取使用首尝试各
**0.8 秒**、唯一补偿各 **0.35 秒**的连接、读取、写入及连接池等待 idle 限制，
补偿前等待一次 25–75ms 抖动；常规请求仍为首尝试各 2 秒、补偿各 0.5 秒。
这些是逐操作 idle 限制，不是墙钟总上限，流式响应和多个阶段仍可能累计等待。
3.0 秒为这组较短的预取尝试、SSL 与元数据处理及清理留余量，
同时明显小于仓库 REQUEST 的 10 秒函数 timeout；该 timeout 不是预置并发 Init
的固定平台时限。

父进程只在主线程可独占 `SIGALRM`/`ITIMER_REAL` 时用 POSIX 定时器监督剩余预算。
非主线程、信号已屏蔽、已有 pending 信号、现有定时器或自定义 handler 均跳过预取，
记 `failure_kind=structural`，保留原信号设置；结束时取消自己的计时器并恢复 handler
与信号屏蔽状态。定时器可中断通常的 socket 等待、慢流与日志 FD 写入，
到期记 `failure_kind=deadline` 并停止新尝试和日志输出。
这是软截止而非硬实时保证：原生 DNS `getaddrinfo`、不可中断的原生清理和 OS 调度
可能延迟 Python 信号处理；超过截止时间也不会接纳缓存。此前 factory、
模块导入、平台准备和随后父进程的 Init 摘要输出不包含在预算内，
不能据此承诺整个 Lambda Init 或冷请求在 3.0 秒内完成。

公钥材料使用与请求刷新相同的校验器，按配置允许算法检查并归一化。
只有全部返回的 key 都可接受时才采用预取缓存；无可用公钥或混合有效/无效 key
都记失败，不接纳部分缓存，首请求仍可读取恢复后的源端。
`jwks_validation` 仅在有拒绝项时输出 `accepted`、`rejected` 计数及
`rejection_reasons` 分类计数。分类限定为 `usage`、`key_ops`、`private_material`、
`algorithm`、`missing_material`、`encoding`、`material`，不含 kid 或公钥材料。
`rejected > 0` 使 `oidc_prefetch.status=failed`，计入现有
`request-init-oidc-prefetch-failed` 过滤器，不以部分可用掩盖源端文档问题。
客户端关闭后还须确认未超时、缓存非空且未过期，才一次采用归一化缓存。
TTL 从成功刷新时起算，采用时最多保留当前时刻之后 300 秒且不延长原到期时间；
异常、迟到、部分结果均保留调用方原缓存，过期内容不能用于认证。

预建记录使用 `operation=request_init`，`status` 为 `ready` 或 `failed`。
`request_entry_import_ms` 只计完整入口导入，不包含随后 factory 和 OIDC 预取；
`stage_timings["handler.factory"]` 记录预建的时长、次数和错误，`factory_ms` 与
该阶段时长相同；`duration_ms` 则计 factory 和预取的总墙钟时间。
预取使用 `auth.oidc`、`auth.discovery`、`auth.jwks` 阶段。
它与 invocation 中的同名阶段必须按 operation 分开，
不能用缓存命中的接近零时间替代 Init 的实际成本。
普通构造异常会在脱敏日志后抛出 `REQUEST initialization failed`，让平台 Init
失败；factory 的进程控制异常保持其退出语义。可选预取边界捕获
`SystemExit`、`KeyboardInterrupt` 等 `BaseException`，将其记为结构性失败并让 Init
继续。固定版本配置及成功的子 factory 可能已缓存，
坏配置需要正确的新配置引用与新环境，不承诺在原进程内重试即可恢复。
`oidc_prefetch.status` 分别为 `ready`、`failed`、`not_started` 或 `interrupted`；
预取失败仍允许顶层 `status=ready`。其受控字段如下，不会延续到首请求：

| `oidc_prefetch` 字段 | 含义 |
|---|---|
| `status` | `ready` 表示有效元数据缓存就绪；`failed` 表示已放弃本次预取；`not_started` 表示 factory 未完成、预取未开始；`interrupted` 仅为调用可选边界前的防御性哨兵，不是预取 `SystemExit`/`KeyboardInterrupt` 的正常结果 |
| `failure_kind` | 失败时区分 `dependency`（身份依赖读取或文档校验失败）、`structural`（预取接线或本地程序结构故障）和 `deadline`（预取总预算耗尽）；不记录任意异常类型或正文 |
| `auth_failure` | 有可用分类时保留既有受控五字段；`failure_kind=structural` 不能仅据此归因为 IdP 故障 |
| `auth_recovered` | 可选 `true`；实际补偿读取成功且本次预取最终成功，语义对应请求侧的成功补偿标记 |
| `metadata_recovered` | 预取最终失败前已有补偿 GET 正常返回时的可选 `true`；成功使用 `auth_recovered`，未观察到成功补偿时省略 |
| `jwks_validation` | 存在拒绝项时的 `accepted/rejected/rejection_reasons` 有界分类计数；不带 key 身份或材料，任何拒绝项均使本次预取失败 |

Init 的 JWKS attempt 日志带 `phase=init` 和 `aws_request_id=null`，
与请求日志分开统计；READY 不证明元数据预取成功，也不保证首次调用时缓存尚未过期。
完整排障查询见[运维手册](operations-runbook.md#我要排查首条请求慢)。

每条 runtime 请求日志另有以下内部字段，不改变工具 schema 或 Gateway 响应：

| 字段 | 含义 |
|---|---|
| `aws_request_id` | 仅保留格式正确的 Lambda 请求 UUID，用来关联同一次调用的 START/REPORT；不使用客户端 MCP ID |
| `first_invocation` | 当前 Python 进程第一次进入 handler，包括初始化失败；后续调用为 false |
| `runtime_module_init_ms` | 本次执行环境导入 runtime 模块的墙钟耗时，作为环境属性在每条请求日志重复出现 |
| `stage_timings` | 固定阶段名到 `count`、`duration_ms`、`max_ms`、`errors` 的映射 |
| `auth_recovered` | 仅 REQUEST 可选的 `true`：实际 discovery/JWKS 补偿 GET 正常返回，未产生终局 `auth_failure`，也未出现未正常处理的 `BaseException`；不要求后续请求成功，其他情况省略 |

`duration_ms` 是该阶段本次请求内累计的墙钟毫秒；`errors` 是离开该阶段时抛出
异常的次数，不含错误正文。嵌套阶段互相包含，不能将所有阶段相加当作总延迟；
例如 `handler.work` 包含快照加载，而 `snapshot.load` 又包含字典初始化。
没有执行的阶段不出现；缓存命中不伪造远程读取或字典加载记录。聚合按请求隔离，
仅接受固定名称，不记录 secret ID、S3 key、地址、查询或描述符。

| 阶段 | 计时范围 |
|---|---|
| `handler.factory` / `handler.work` | 缓存 handler 的获取/首次构造，与本次业务执行分开 |
| `runtime.config` / `runtime.aws_client` / `runtime.http_client` / `runtime.models` | 首次配置加载、AWS/HTTP 客户端构造、模型配置和适配器准备 |
| `runtime.aws_session` | 共享 Session、凭证链、端点解析和服务索引预热；发生在首个 `runtime.aws_client` 内，独立于服务子阶段 |
| `runtime.aws_client.s3` / `runtime.aws_client.dynamodb` / `runtime.aws_client.secretsmanager` / `runtime.aws_client.bedrock_agentcore_control` | 完成共享预热后，对应服务的客户端构造和适配器安装；包含在 `runtime.aws_client` 内 |
| `auth.oidc` / `auth.discovery` / `auth.jwks` | JWT 验证及需要刷新时的两个身份端点读取 |
| `auth.envelope` / `auth.authorize` / `auth.rate_limit` | 签名封套、完整授权视图构建和限流 |
| `aws.secrets` / `aws.control` / `aws.policy` / `aws.safety` | 实际 secret 获取、强一致控制读取、不可变策略及安全目录读取 |
| `snapshot.refresh` / `snapshot.manifest` / `snapshot.objects` / `snapshot.load` | reader 检查、远程 manifest、并行下载整体、完整本地索引校验/恢复 |
| `tokenizer.import` | 显式 jieba 导入或模块缓存查找；两种构造路径都在资源定位之前计时，累计同请求内的调用 |
| `tokenizer.base_dictionary` | 已导入 jieba 后的资源定位、基础词典读取及摘要；只记摘要缓存未命中，不包含在 `tokenizer.dictionary` 内 |
| `tokenizer.dictionary` | 预构建词典加载及自定义词应用；与 jieba 导入、基础词典摘要为兄弟阶段 |
| `tokenizer.dictionary.read` / `tokenizer.dictionary.decode` | 包内词典读取与摘要校验、JSON 解码；包含在 dictionary 内 |
| `model.setup` / `model.embedding` / `model.rerank` | 传输配置构造、实际 query embedding / rerank 调用；后两项包含调用内的 secret 刷新与受控重试 |
| `search.authorize` / `search.core` / `search.bm25` / `search.vector` / `search.ranking` / `search.response` | Search 授权复核、检索总体、词法/向量打分、基础排名、最终授权过滤及响应对象构建 |

点号名称不单独定义包含关系，以上表格和实际调用范围为准。服务子阶段仍包含该服务
首次模型加载与客户端模块导入，不能当作远程 AWS API 延迟；共享预热单列可避免
把整份 Session 成本误归给第一个服务。运行时不再导入 Boto3/s3transfer，但两者仍为
锁定依赖；User-Agent 前缀随之改变为 Botocore，外部按 Boto3 前缀过滤的查询需检查。

平台 **Lambda Init Duration** 在按需冷启动中以同一请求的 REPORT 为准；
预置并发的 Init 则查看同一执行环境的 INIT_REPORT，不要求它关联一次 invocation。
应用无法测到 Python 运行前的环境分配和运行时启动，所以不把
`runtime_module_init_ms` 冒充平台 Init，也不能把它与 Init Duration 重复相加。
比较首调用时，先以 `aws_request_id` 关联 REPORT，再看 `handler.factory` 与
`handler.work` 及其子项；预置并发预建成本另取 `request_init` 记录。
应用 `duration_ms` 与平台 Duration 的小差额还包括日志输出和运行时收尾。

OIDC 依赖失败时，同一条请求日志另带 `auth_failure` 对象，可用现有
`aws_request_id` 关联平台记录；成功请求和调用方 JWT 无效不带此字段。
它不进入 Gateway 响应，原 `ServiceError` 类型及 `internal failure` 消息保持不变。

| 字段 | 含义 |
|---|---|
| `endpoint` | `discovery`、`jwks` 或公钥解析阶段 `jwk`，不记录实际 URL |
| `category` | 固定分类：`http`、`timeout`、`network`、`document`、`unexpected` |
| `reason` | 固定原因代码，按分类的完整枚举见下表 |
| `http_status` | 已收到响应头时保留状态码；未收到或仅解析缓存公钥时为 `null`。状态 200 仍可能发生后续读取超时或文档错误 |
| `cause` | 只有 `timeout`、`network` 检查异常显式因果链中的具体类型，取值为 `dns`、`tls_certificate`、`tls`、`connection_refused`、`connection_reset`，未匹配时为 `null`；`http`、`document`、`unexpected` 恒为 `null` |

| `category` | `reason` 完整枚举 |
|---|---|
| `http` | `status` |
| `timeout` | `connect`、`read`、`write`、`pool`、`unknown` |
| `network` | `connect`、`read`、`write`、`close`、`protocol`、`unknown` |
| `document` | `endpoint_invalid`、`content_encoding`、`json_encoding`、`json_invalid`、`body_too_large`、`not_object`、`discovery_mismatch`、`jwks_keys`、`jwk_entry`、`jwk_kid`、`duplicate_kid`、`jwk_material` |
| `unexpected` | `unknown` |

`content_encoding` 表示 HTTP 内容编码解压失败，`json_encoding` 表示 JSON 字节的
Unicode 解码失败。`endpoint_invalid` 是读取前端点复核的防御性错误码；正常配置
已在构造时校验，不能因日志中没有这个码而排除配置问题。

诊断不读取或输出异常正文、隐式异常上下文、响应正文/头、URL、JWT、kid 或密钥。
`connect` 超时可能覆盖 TCP/TLS，不能据超时类别推断具体网络环节；缺少 `cause`
不代表已经排除 DNS/TLS。元数据字段与公钥结构校验仍位于原 fetch 计时之外，
因此 `auth_failure` 的文档错误可能同时具有 `auth.discovery/jwks.errors=0`。

#### JWKS 逐次读取事件

当前源码为 runtime REQUEST 请求和预置并发 Init 预取诊断作用域内实际发生的
每次 JWKS GET 输出一条独立事件。Init 由父进程直接写日志 FD（默认 stdout 的 FD 1），
绕开 Python 日志锁和 Python stdout 对象，尝试事件沿用同一日志流。
父进程在预取返回后输出 Init 摘要，不等整段预取结束才打印尝试事件。
在预取工作截止时间内，每次尝试结束即写出，首错在补偿等待和补偿 GET 之前记录；
工作预算已耗尽时不再启动日志写入，因此终局超时尝试可能没有逐次事件。
缓存命中、
discovery 读取和后续公钥结构校验不生成此事件。各入口按实际资产核对事件；
不能用新字段补全历史请求缺失的首次错误类别。

| 字段 | 含义 |
|---|---|
| `operation` | 固定为 `oidc_fetch`，与最终 `operation=request` 或 `operation=request_init` 摘要分开 |
| `event` | 固定为 `attempt_finished` |
| `endpoint` | 固定为 `jwks`，不记录实际 URL |
| `phase` | 仅 Init 预取带固定值 `init`；请求期省略该键。查询请求期应使用 `not ispresent(record.phase)` |
| `aws_request_id` | 请求期为格式正确的 Lambda 请求 UUID，缺失或格式不合法时为 `null`；Init 固定为 `null`，因为没有 invocation |
| `attempt` | 同次 `verify` 或 `_prefetch` 内实际 JWKS GET 的顺序，从 1 开始；请求期最多 3，Init 预取最多 2 |
| `compensation` | 是否使用现有唯一补偿额度；unknown-kid 正常刷新不因此标为补偿 |
| `duration_ms` | 本次读取尝试的有限非负毫秒数；不含抖动等待或事件写出耗时 |
| `outcome` | 固定为 `ok`、`failed` 或 `interrupted`；请求期中断继续传播，预取中断由可选边界转为失败 |
| `failure` | `failed` 时使用上文受控的 `endpoint/category/reason/http_status/cause` 五字段；其余为 `null` |

`outcome=ok` 只表示此次读取及 JSON 对象解析完成；后续 `keys` 结构、公钥材料、
JWT 和授权校验仍可能失败。逐次事件不带 `auth_recovered`，不改变最终摘要的
`auth_failure`、`auth_recovered`、`stage_timings` 或业务响应。计时和日志写出的普通
故障不得替换原结果或异常。查询业务请求数、终局请求错误或请求恢复次数时筛选
`operation=request`；Init 预取的结果与恢复次数另筛选 `operation=request_init`，
读取 `oidc_prefetch`。Init 没有请求 UUID，按 `@logStream` 和 Init 时间窗口关联
`request_init`、`phase=init` 事件及 `INIT_REPORT`；不能将所有 null UUID 聚成一次调用，
也不能把独立读取事件计作额外请求。

#### 冷路径认证恢复

每次 JWT 验证最多共享一次补偿 GET，只重发失败的 discovery 或 JWKS 请求。
首尝试保留连接、读取、写入及连接池等待各 2 秒的超时；补偿 GET 各为 0.5 秒，
之前仅等待一次 25–75ms 随机抖动。HTTP 500/502/503/504、连接或读取超时可触发
补偿；有明确 TLS 因果证据、其他 HTTP 状态（包括 429）、其他网络错误、
文档校验错误与未知错误均不重试。第二次失败继续以 `ServiceError` 拒绝请求。

discovery 必须精确匹配配置后才能在本次验证内复用，不能跨请求复用该状态。
JWKS 仍使用实例内最多 300 秒的缓存，整个 JWKS 的结构校验成功后才替换；
获取或结构校验失败不覆盖已有内容，过期缓存不能用于认证。
unknown-kid 的第二次 JWKS 刷新和 30 秒冷却期保留，
与初次刷新共享补偿次数及已验证的 discovery。因此通常冷验证最多三个 GET，
冷缓存遇到 unknown kid 时最多四个；热缓存命中不增加网络调用。
JWT 的有效期在网络等待结束后使用当前时钟重新校验。

`auth.discovery`、`auth.jwks` 的 `count/errors` 按每次 GET 尝试计数。
抖动等待计入 `auth.oidc`，不计入这两个端点阶段的时长。
REQUEST 的恢复标记只在 `_fetch_json` 的补偿 GET 正常返回后收集，使用请求局部
`ContextVar`，再由 `request_handler` 经现有字段通道输出顶层
`auth_recovered=true`。它不依据任意 `auth.*` 的计时聚合，也不要求最终
`status=ok`：补偿恢复后 JWT 过期、授权拒绝、限流或下游普通异常，仍保留该字段，
且不带 `auth_failure`。这个标记表示依赖读取已恢复，不表示最终认证或业务执行成功。

终局 OIDC 依赖或公钥文档错误带有 `auth_failure` 时，省略恢复标记；
未正常处理的 `BaseException`、没有重试及缓存命中也不带该字段。
标记不跨请求、不保存在 verifier 实例，仅 REQUEST 输出，不进入 Gateway 响应。
PC Init 使用独立诊断作用域，成功预取的补偿位于
`oidc_prefetch.auth_recovered`；已有补偿成功但预取最终失败时，只保留
`oidc_prefetch.metadata_recovered` 与失败分类，不产生成功恢复计数。
最终 OIDC 失败的 `auth_failure` 描述最后一次失败，不保留远端正文；
恢复标记本身不保留首次故障的具体类别；部署逐次读取事件后，可按同一
`aws_request_id` 查看请求期 JWKS 尝试的 `failure`；Init 则用同一 `@logStream`
和时间窗口关联。`request-auth-recovered` 告警统计每五分钟
恢复请求数，`Sum > 2` 即至少三次触发；一个评估周期、缺失数据不告警，
沿用既有 SNS 路由，不使用成功请求分母或按 HTTP 尝试次数计数。
`request-init-auth-recovered` 独立统计成功预取的恢复次数，使用同一阈值和路由；
`request-init-oidc-prefetch-failed` 统计预取失败，五分钟内 `Sum > 0` 即告警。
这两个 Init 指标不进入请求数或请求内部错误率。

这些 HTTP 超时是逐操作限制，不是 OIDC 的墙钟总上限；分阶段连接与流式读取
可能累计超过单项超时，不能将策略解释为“认证最多 2.575 秒”。
冷 p95 4 秒仅为设计规划；已有 2 秒读取等待的请求还会增加抖动及
补偿 GET 耗时，factory 和其他阶段也占用同一预算。应关联同一请求的计时，
不能相加不同样本的 p95，也不能据本地恢复测试宣称已经达到该目标。

远程快照在开始资产 GET 前，先对全部引用检查 Gateway 命名空间、各格式文件上限，
再检查加权恢复预算 `64 × JSON 字节数 + 8 × vectors.npy 字节数 <= 256 MiB`。
JSON 系数为对象解析与校验副本留余量，向量系数包含恢复时的 float64 检查；
这是服务准入策略，不是进程 RSS 的数学上界。最多三个下载线程，每次读取至多
256 KiB，写入临时文件并更新 SHA-256 后释放该块；短读继续读取，提前 EOF、多余尾字节、
摘要或元数据错误均拒绝。线程全部结束后才清理临时目录，完整性、索引及当前安全目录
校验成功后才替换 resident。

同一保护覆盖 Search reader、Enhancer 初次加载及增强发布时的原索引复核。
发布器也在任何资产上传前应用相同大小/预算检查。原本格式允许而超过服务预算的快照
将受控失败。共同预算取配置中最小索引函数内存 1024 MiB 的四分之一；
500 工具、每工具 3/6 条已批准例句、1024 维的读写有回归覆盖，完整加载、
两份增强索引共存及 Enhancer 生成/发布的本地峰值见[容量测量](capacity.md)。
校准目标是最小函数仍留至少 256 MiB 安全余量，双区域合成测试核对配置与预算。
这不证明任意自定义词典、模型输出或目标 Lambda 的峰值；不能由下载块上限
推断整条处理链的内存。

`model.embedding.count` 表示确实进入了 provider 调用。若不存在该阶段，并且
原有诊断显示向量不可用，可能是前处理已耗尽 provider 可用预算、模型/向量未配置或
profile 不匹配；不能称为一次模型请求超时。Search 总预算默认 3.0 秒，embedding
子预算默认 1000 ms；仅在 Lambda 剩余期限分支扣除 300 ms 收尾余量，
再与配置期限取较早者。缺少或不可解析的 Lambda 剩余时间沿用配置期限；
`lambda_deadline_ignored` 区分正常缺少方法与异常值。
两个预算均可配置，且属于协作式 deadline，不能保证阻塞调用按时中断。
空 `embedding_timing.attempts` 也可能是自定义 provider 跨线程丢失诊断上下文，
不能单凭空列表断言没有模型 HTTP 请求；
具体边界见[embedding诊断](embedding-diagnostics.md)。
Gateway 外部传输和响应时间仍需客户端与平台的配对证据；本地测试不证明
部署后的冷 p95 已达到目标。
指标覆盖边界及运维处置见[部署 runbook](deploy.md#告警与处置)。
