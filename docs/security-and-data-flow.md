# 安全与数据流

本项目让 Agent 按需搜索工具，同时用当前权限限制工具发现、模型候选和工具调用；整体目标见[项目总览](overview.md)。本文说明身份怎样被验证、权限怎样被复核，以及哪些数据会发送给外部服务。读者是客户架构师、安全负责人、授权管理员和运维人员。读完后，你可以核对部署的信任边界、安排密钥轮换，并判断一份目录或查询是否适合进入模型搜索。

## 先区分三类检查

认证回答“请求来自哪个主体”，授权回答“这个主体可以使用哪些工具”，数据外发许可回答“这个主体的查询能否发送给模型”。这三项不能互相替代。合法 JWT 不自动获得工具权限；工具可见也不自动允许查询外发。

JWT 是身份服务签发的短期令牌。OIDC 提供身份元数据，JWKS 提供验证令牌签名所需的公钥。REQUEST 拦截器在 Gateway 分派请求前运行；它验证令牌、检查当前权限，再把服务端签名的身份信息交给 Search。整体图见[系统架构](architecture.md#系统总览)。

工具 schema 定义参数名称、类型和必填项，服务用它的摘要检查当前定义是否仍与批准版本一致。内部身份封套的 HMAC 签名由 REQUEST 用共享密钥计算，Search 用同一密钥验证内容是否被改动。这里的签名密钥与 IdP 用来签发 JWT 的私钥分开管理。

## 认证与授权链路

| 顺序 | 检查内容 | 失败时的行为 | 实现依据 |
|---|---|---|---|
| 1. Gateway 入口 | 已有 Gateway 配置 `CUSTOM_JWT`；调用方发送 Bearer JWT | 由 Gateway 拒绝；具体 HTTP/MCP 错误以实测契约为准 | [绑定与验收步骤](deploy.md#获授权后的部署顺序) |
| 2. REQUEST 验证 JWT | 使用显式 `OIDC_ISSUER`、`OIDC_AUDIENCE`、`OIDC_DISCOVERY_URI`、`OIDC_JWKS_URI`、`OIDC_ALLOWED_HOSTS`；检查算法、签名、必要 claims 和有效期 | 无效身份拒绝；身份依赖故障按内部故障拒绝，不放行过期公钥缓存 | [配置入口](../src/semantic_service/runtime.py)、[OIDC 验证](../src/semantic_service/auth.py) |
| 3. 读取当前权限 | 按 issuer 与 subject 精确匹配策略；验证所需 scopes/groups、`disabled`、`not_before`、工具身份和 schema 摘要 | 未登记主体、撤销主体或不匹配工具拒绝 | [DDBAuthorizer](../src/semantic_service/auth.py) |
| 4. REQUEST 改写 | 无条件删除客户端 `_auth_context`；Search 调用写入重新签名的封套；其他工具不携带此字段 | 不接受调用方自报身份 | [RequestInterceptor](../src/semantic_service/interceptor.py) |
| 5. Search 验证封套 | 校验 HMAC 签名、期限和策略版本；逐字绑定 Gateway、target、工具名、字符串 MCP 请求 ID、查询摘要与可信 Lambda context | 缺失、伪造、过期或错绑封套均拒绝 | [EnvelopeCodec](../src/semantic_service/auth.py)、[SearchHandler](../src/semantic_service/search.py) |
| 6. Search 多次授权 | 加载前后检查权限；候选外发前和返回前继续复核；结果始终取允许集合的交集 | 权限缩小则过滤或拒绝；授权读取失败直接失败 | [服务搜索](../src/semantic_service/search.py)、[检索核心](../src/semantic_core/search.py) |
| 7. 执行业务工具 | Agent 使用搜索返回的完整工具名发起新调用，仍经过 REQUEST 工具授权 | 无工具权限拒绝；业务系统还应验证具体资源权限 | [调用分派](../src/semantic_service/interceptor.py) |

服务端配置限制访问令牌的 `exp - iat` 最长为 300 秒，时钟偏差最多 30 秒；内部封套最长为 30 秒。这些是代码准入限制，不是 IdP 已正确配置的证明。IdP 需要按这些限制签发令牌，部署时再验证。算法默认 `RS256`，只接受明确配置的非对称算法，不接受令牌头中的 `jku`、`x5u`、`jwk`、`crit` 来改变密钥来源。[验证约束](../src/semantic_service/auth.py)给出完整判断。

封套采用签名，不是加密。它携带主体、scopes/groups 和绑定信息；业务查询只写入摘要。不得把 `_auth_context` 作为普通调试信息记录。封套含 `nonce`，但当前没有跨实例的一次性消费记录，不能宣称它完全防重放；在有效期内仍依赖绑定、当前授权与入口限流。

### 权限为什么与索引分开保存

索引是工具内容的检索副本，不能作为当前权限的权威。当前权限指针和永久删除标记存放在独立 protected 表的 `PROTECTED`；内容指针存放在 state 表的 `CONTROL`。每次控制读取按 `PROTECTED → CONTROL → PROTECTED` 做强一致读取，前后受保护状态不一致就拒绝该次读取。[存储实现](../src/semantic_service/storage.py)保证读取不会静默混用两次管理变更中的状态。

工具授权绑定 `[account, region, gateway_id, target_id, incarnation, full_name]`，并核对 `schema_digest`。`incarnation` 区分同名工具的不同生命周期。因此，同名重建或 schema 变化不会自动继承旧批准。管理员需要核对实际描述符，再更新授权。字段定义见[工具身份](../src/semantic_core/catalog.py)和[策略格式](service.md#身份和授权)。

普通身份必须显式获得 Search 自身权限，才能看到入口和搜索业务工具。机器 Indexer 身份也必须登记策略，但仅允许初始化、ping 和完整列目录；不能因它能读取目录就授予业务调用能力。[列表处理](../src/semantic_service/interceptor.py)没有“仅记录但不拦截”的开放模式。

权限变化在下一次授权读取时生效。已经发往模型的数据不能撤回；最终权限检查之后发生的撤销也存在时间窗口。工具自身必须继续执行资源级授权。目录轮询还存在观察上游变化的窗口，不能把本地安全目录称为上游实时事实。具体新鲜度限制见[已知限制](known-limitations.md)。

## 哪些数据会离开 AWS

当前模型路径是受控 OpenAI-compatible 代理，再由代理访问所选百炼模型。模型名称不能证明实际处理地域、代理托管地点或数据保留方式。现有设计已允许查询和工具描述外发，但代理及后端的处理地点、保留和训练条款仍须按客户登记核实；本文不作未经核实的数据驻留承诺。[设计中的外发决定](design.md#64-跨云数据与降级)保留批准范围。

| 发送方 → 接收方 | 发出的内容 | 触发条件与限制 |
|---|---|---|
| Search → 模型代理 → 百炼 embedding | 用户的完整查询文本 | `SEMSEARCH_MODELS_ENABLED=true`，当前主体策略 `allow_query_egress=true`，且模型、向量版本与预算可用 |
| Search → 模型代理 → rerank | 查询和当前已授权候选的工具摘要 | 另需 `MODEL_RERANK_ENABLED=true`，并通过候选数量、文本长度和剩余预算检查；默认关闭 |
| Enhancer → 模型代理 → 生成模型 | 工具摘要、要求生成的语言和条数 | 模型启用；生成结果通过结构校验后进入缓存，默认不自动获准使用 |
| Enhancer → 模型代理 → embedding | 工具摘要与可信审核回调批准的示例文本 | 模型启用；当前部署未配置审核回调，默认只使用真实工具摘要 |
| Indexer → 客户 IdP 的 OAuth 端点 | `client_id`、`client_secret`、请求的 scope 等认证参数 | 使用独立机器客户端换取短期令牌；发送到显式配置的 token endpoint |
| REQUEST → 客户 IdP | 配置的 OIDC discovery/JWKS 地址的读取请求 | 需要身份元数据或公钥刷新时；本项目验证器不把原始 JWT 发给 IdP |
| Search / Enhancer → 模型代理 | 模型认证凭据，默认放入 `Authorization` 请求头 | 只发往通过精确主机允许清单校验的 HTTPS 模型端点 |

外发字段来自[服务搜索](../src/semantic_service/search.py)、[模型适配器](../src/semantic_service/models.py)、[增强器](../src/semantic_service/enhancer.py)、[机器 OAuth](../src/semantic_service/catalog.py)和[身份验证器](../src/semantic_service/auth.py)。Gateway 自身怎样获取和缓存身份元数据属于 Gateway 配置与服务行为，需按部署验收核对，不能用本项目 REQUEST 的行为代替。

工具摘要由完整工具名、描述、参数名和参数说明组成。`ToolRecord.parameters` 不把 schema 默认值或任意元数据放入模型摘要；它也不会自动识别文本中误写的个人信息或商业秘密。原始描述符仍会保存在索引并返回给授权调用者。因此，工具登记时就应去掉秘密默认值、令牌、个人标识和真实业务样本。[摘要提取](../src/semantic_core/catalog.py)是字段限制，不能替代内容审核。

查询参数校验只检查类型、长度和允许字段，没有自动脱敏功能。Agent 应避免把实际业务 payload、凭证或敏感个人信息拼入查询。若一个主体不允许查询外发，将其 `allow_query_egress` 设为 `false`；Search 仍可按当前权限进行 BM25 检索。这个开关不控制后台 Enhancer 的目录外发。目录整体不允许外发时，应关闭该部署模型，或采用另行验证的内部模型方案。

### 网络边界能保证什么

`MODEL_ALLOWED_HOSTS` 和 `OIDC_ALLOWED_HOSTS` 限定明确的 HTTPS 主机；用户输入不能指定目标 URL。运行时 HTTP 客户端设置 `trust_env=False`、`follow_redirects=False`，不会从环境自动采用代理，也不自动跟随重定向。[HTTP 配置](../src/semantic_core/_http.py)与[客户端构造](../src/semantic_service/runtime.py)给出实现。

这是应用层端点限制，不是防火墙或数据防泄漏系统。默认 CDK 没有创建 VPC、NAT 或出口检查资源。若客户需要网络层限制，应在部署设计中另行落实；给 Lambda 配置私网并不能证明 Gateway 服务可访问客户 IdP。现有资源边界见[部署说明](deploy.md#资源和区域边界)。

## 密钥清单与轮换

三类 Secrets Manager secret 必须分别保存，不能复用一个 secret。CDK 只引用已有 secret 的精确身份，不创建或读取值；各运行角色只获得需要的 `GetSecretValue` 权限。采用客户 KMS key 时，解密权限同时限定 key、Secrets Manager 服务和对应 secret 的加密上下文。[权限生成](../infra/stack.py)是准入依据。

| 凭据 | 配置引用 | 保存字段 | 使用方及发送范围 |
|---|---|---|---|
| 模型代理凭据 | `MODEL_SECRET_ID` | `base_url`、`api_key` | 模型启用时的 Search、Enhancer；认证值发送到配置的模型端点 |
| 内部签名密钥 | `SEMSEARCH_SIGNING_SECRET_ID` | `key_b64`、`activated_at` | REQUEST 签名、Search 验签；密钥本身不发送到 IdP 或模型端点 |
| Indexer OAuth 凭据 | `SEMSEARCH_OAUTH_SECRET_ID` | `client_id`、`client_secret` | Indexer；发送到配置的 IdP token endpoint |
| IdP 签名私钥 | 由客户 IdP 管理，本 stack 无对应 secret | 由 IdP 决定；本项目只读取 JWKS 公钥 | 私钥留在 IdP 管理范围 |

字段校验和缓存见[密钥实现](../src/semantic_service/secrets.py)。`key_b64` 解码后至少为 32 字节；`activated_at` 必须表示实际提升版本的时间，不能用创建 secret 的时间代替。非秘密 S3 配置只存受控配置值，不能用它传递 secret 值或覆盖资源身份与密钥引用。[版本化配置](service.md#版本化-s3-配置)描述了允许字段。

### 我要正常轮换凭据

1. 按凭据类型准备新版本。模型和 OAuth 凭据还需要对应供应商或 IdP 的创建、启用和撤销操作；仅改 Secrets Manager 不能完成供应商侧轮换。
2. 依据[轮换与紧急撤销](deploy.md#轮换与紧急撤销)提升新版本。签名密钥以 Secrets Manager `VersionId` 作为 `kid`，提升 `AWSCURRENT` 时记录真实 `activated_at`。
3. 验证新请求、新令牌和签名封套能正常工作，再按批准窗口撤销旧凭据。失败时检查 secret 版本、读取权限、实际端点和缓存，不打印凭据原文。

签名旧版本的 `AWSPREVIOUS` 接受窗口至少覆盖“密钥缓存期限 + 封套期限 + 时钟偏差 + 安全余量”。默认是 `300 + 30 + 30 + 30 = 390` 秒，即 6.5 分钟；含义是正常轮换时兼容仍持有旧签名密钥的运行环境，不是泄露后的继续使用许可。[SigningSecretsKeys](../src/semantic_service/secrets.py)执行这个约束。IdP 的公钥重叠窗口还需结合已签发令牌和缓存传播验证，不能直接套用内部签名密钥窗口。

### 我要紧急撤销

1. 主体撤销由授权管理员更新 `disabled` 或 `not_before`，并通过正常条件事务推进策略版本。不要等待旧 JWT 自然过期。
2. 内部签名密钥泄露时，运行时可通过 Search 与 REQUEST 的 `SEMSEARCH_REVOKED_KIDS` 拒绝指定密钥版本。但当前[CDK 配置生成器](../infra/stack.py)尚未分发该字段，不能直接把它加入普通部署配置文件就宣称撤销完成。需要经评审的部署变更同步注入两端、发布配置和版本，并确认所有入口指向新 alias 版本；完成前保持受影响入口停用。只改变 secret stage 不能立即清除热实例缓存。
3. 在旧实例仍可能服务或撤销状态无法证明完整时，停用受影响入口。恢复前验证错误密钥、过期封套、错绑请求和已撤销主体均被拒绝。

成功判据是新授权状态生效且负例被拒绝，不是只看到 secret 版本提升。详细恢复步骤见[运维手册](operations-runbook.md)；内容回滚、PITR 恢复和清理均不能恢复旧权限或清除永久删除标记。

## 日志记录什么，避免记录什么

本项目采用固定字段日志。错误分类在本地生成，不把底层响应正文或异常文本直接作为诊断输出。日志默认保留 30 天；这是[CDK 默认配置](../infra/functions.py)，实际部署可按允许选项调整，并需确认告警通知目的地。

| 可以用于排查的字段 | 含义与范围 |
|---|---|
| `operation`、`status`、`duration_ms` | 操作类别、固定结果和耗时 |
| `aws_request_id`、`first_invocation`、`stage_timings` | 合法 Lambda 请求 UUID、是否首次调用及固定阶段计时；不用客户端 MCP ID 关联日志 |
| `ranking_mode`、`degraded`、`vector_availability`、`rerank_state` | 实际检索模式和模型状态；不含分数或候选文本 |
| `embedding_error` | 受控错误类别、HTTP 状态及有界允许清单数字；不保留原始响应头或错误正文 |
| `auth_failure`、`auth_recovered` | 受控 OIDC 失败分类或元数据补偿读取恢复标记；不含 JWT、URL、kid 或密钥 |
| `oidc_prefetch` | 仅 `request_init` 的预取摘要；固定 `status`，失败时的 `failure_kind` 为 `dependency/structural/deadline`，可选受控 `auth_failure`、成功预取的 `auth_recovered=true`，或终局失败前已有补偿返回的 `metadata_recovered=true`；有 key 拒绝项时输出 `jwks_validation` 计数 |
| `operation=oidc_fetch` 的独立事件 | REQUEST 的实际 JWKS GET 尝试，含固定 `event/endpoint`、`attempt`、`compensation`、耗时、固定 `outcome` 和受控五字段 `failure` 或 `null`；请求期 UUID 合法或为 `null`、`attempt` 为 1–3，省略 `phase`；Init 固定 `phase=init`、UUID 为 `null`、`attempt` 为 1–2 |

具体结构见[运行时日志](../src/semantic_service/runtime.py)、[服务说明](service.md#冷启动分段计时)和[embedding 诊断](embedding-diagnostics.md)。`auth_recovered` 只表示依赖读取恢复，不等于整个请求成功。Init 按日志流与时间窗口关联，不把 `null` 请求 UUID 当成关联键；结构性预取故障不自动归为 IdP 故障。

预取在父进程内使用独立 HTTP 客户端和独立 verifier，以已校验的公共 OIDC 配置读取
discovery/JWKS，不使用 JWT、signing secret 或 AWS 凭证。客户端禁用环境代理、
重定向和保留的空闲连接，结束时关闭，不复用请求客户端的连接池。
Init 逐次事件直接写日志 FD，绕开 Python 日志锁和 Python stdout 对象。
工作截止时间已到时停止新日志写入；预取只记录固定字段，不打印底层异常。
`SystemExit`、`KeyboardInterrupt` 等 `BaseException` 在可选预取边界转为失败，
不使 Init 失败；必需的 factory 构造失败仍保留其拒绝就绪的行为。

公钥材料使用请求路径的同一校验器，按配置允许算法检查并归一化。
含无效 key 时不接纳部分缓存，即使其余 key 可用也放弃本次预取，首请求仍可重新读取。
`jwks_validation` 只含 `accepted`、`rejected` 和 `rejection_reasons` 分类计数；
原因集合固定为 `usage`、`key_ops`、`private_material`、`algorithm`、
`missing_material`、`encoding`、`material`，不带 kid、密钥材料或原文。
`rejected > 0` 使预取失败，进入现有 `request-init-oidc-prefetch-failed` 指标。
只有独立客户端关闭后、预算未到且缓存仍有效，才采用归一化缓存；
TTL 不因采用而延长，异常、迟到或不完整结果都不会覆盖调用方缓存。
这些改动不新增外部 API、依赖或 tool schema。

3.0 秒是 POSIX `SIGALRM` 监督的软截止，默认工作 2.75 秒、清理最多 0.25 秒，
覆盖 discovery/JWKS、一次共享补偿、日志和客户端清理。已有定时器、信号冲突或
非主线程会跳过预取并记 `structural`。原生 DNS `getaddrinfo` 或不可中断的 OS 调用
不能保证被 Python 信号硬抢占；也不承诺整个 Init 或首请求在该时间内完成。
信号所有权与计时边界见[预取计时说明](service.md#冷启动分段计时)。

JWKS 逐次事件是服务端诊断日志；各入口按实际版本核对，不进入 Gateway 响应。
预算允许时首错在补偿等待前输出；不记录 URL、响应体、token、kid、密钥、任意异常类名、
异常正文或隐式异常上下文。`failure` 复用受控分类及显式因果链规则；
没有 `cause` 仍不能排除 DNS/TLS。其 `outcome=ok` 不等于后续公钥或认证校验成功。

应用日志不输出查询原文、完整描述符、JWT、封套、secret 值、候选分数或隐藏工具统计。排查时不要临时打印完整事件、请求头、模型响应或异常对象；这些内容可能绕过现有固定字段约束。客户 IdP、模型代理、客户端和业务工具的日志不由本仓库控制，需各自核对其记录和保留策略。

## 多租户隔离要点

| 隔离层 | 当前措施 | 仍需注意 |
|---|---|---|
| Gateway 身份 | 账号、区域、Gateway、target 和生命周期共同组成工具身份；外部查询不能指定另一 Gateway | 配置或目录迁移不能复用旧身份 |
| 资源与角色 | 每 Gateway 独立计算角色、索引桶和两张状态表；S3 前缀与 state 键继续限定访问范围 | 共享账号仍共享账号级 Gateway 和 Lambda 等配额 |
| 主体权限 | 服务端主体映射、schema 摘要、安全目录和多次强一致授权检查 | JWT scopes/groups 只是匹配条件，不是独立的工具授权来源 |
| 检索与模型候选 | 无权工具在候选选择前被屏蔽，rerank 只接收当前已授权摘要 | BM25 的词频统计来自同一 Gateway 完整目录；不承诺主体之间的统计或时延完全隔离 |
| 缓存 | 快照、密钥和客户端可复用；授权每请求复核，当前没有跨主体查询结果缓存 | 新增结果缓存必须重新设计权限、版本和失效边界 |
| 管理权限 | 运行角色不能写 protected；后台发布条件检查最新授权和删除状态 | 授权管理员属于独立外部管理渠道，不能把管理凭据交给运行函数 |
| 限流 | REQUEST 以 Gateway 与主体的固定分钟窗口计数，普通主体与机器身份可配置不同上限 | 不是秒级平滑限流；不能阻止同账号其他应用耗尽配额 |

这些措施有代码与离线回归覆盖，仍需在具体部署中验证 IdP、Gateway 配置、IAM 和撤销负例。参考[本地完整链路测试](../tests/test_service_roundtrip.py)、[基础设施断言](../tests/infra/test_stack.py)和[部署后验证清单](deploy.md#事后验证清单)。工具描述与模型生成文本应始终作为不可信数据供 Agent 阅读，不能提升为系统指令。
