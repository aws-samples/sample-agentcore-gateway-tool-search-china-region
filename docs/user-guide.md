# Agent 开发者使用手册

本手册介绍 Agent 如何通过 Gateway 搜索自己有权使用的工具，再按工具定义发起业务调用。这样可以减少 Agent 每次加载的工具数量，同时保留每次调用的权限检查；整体目的见[项目总览](overview.md)。本文适合编写 Agent、MCP 客户端或应用接入层的开发者。读完后，你可以完成令牌获取、协议初始化、工具搜索和结果解析，并判断空结果、调用错误与检索降级。

## 先认识调用链

MCP 是客户端与工具服务交换工具定义、调用请求和结果的协议。本项目的 Search target 是 Gateway 后面的搜索工具入口；target 表示 Gateway 将请求转发到的后端。REQUEST 拦截器是在转发前执行的函数，负责验证身份、检查权限、限制调用频率，并为搜索请求加入服务端签名。

客户端先取得访问令牌，再连接 Gateway。普通 Agent 的 `tools/list` 只返回搜索工具。Agent 调用搜索工具后得到已授权业务工具的描述符，再用描述符中的完整工具名和参数定义调用业务工具。搜索不会执行业务动作；搜索结果也不会替代后续调用的权限检查。实现见[REQUEST 拦截器](../src/semantic_service/interceptor.py)和[Search handler](../src/semantic_service/search.py)。

启用 ECS 主路后，Gateway 地址、工具名、参数及返回格式保持相同。客户端继续访问 Gateway，
不直连容器、不选择后端，也不提交 `_auth_context` 或额外的 MCP ID 绑定头。
ECS 是默认关闭的部署选项；REQUEST 预置并发也由运维配置，示例默认关闭。
这两个开关都不改变 Agent 的调用方式。
客户端使用相同搜索契约；后端配置由部署负责人核对。

应用应为搜索设置与业务体验相符的等待预算，并区分超时、MCP 错误和空结果。
样例不承诺固定延迟或生产 SLO；边界见[已知限制](known-limitations.md)。

## 复用客户端连接

在同一应用进程中复用 MCP 会话及 HTTP 客户端/连接池，保持 HTTPS keep-alive；不要为每个
Search 新建客户端、重复初始化协议或重复获取仍有效的 token。并发调用共享有界连接池，
连接池大小按应用的实际并发上限设置，并在程序结束时关闭。服务端或网络关闭闲置连接后，
客户端应允许正常重新建连；连接复用不保证任意闲置时长后 TCP/TLS 仍然存在。

每次调用仍使用独立的字符串或整数 JSON-RPC ID，正常执行令牌到期刷新和 MCP 错误检查。
数字 ID 的搜索调用须先完成[部署验收](service.md#mcp-id-deployment-acceptance)，本文示例使用字符串。
连接复用不能替代授权，也不能在请求失败后盲目重复工具调用。性能测试应分别记录复用连接、
新连接和闲置后的首批请求，避免把 TCP/TLS 建连时间当作 Search 计算时间。

## 我要接入 Gateway

### 1. 取得接入信息

向服务管理员领取下表中的非秘密配置，并通过客户既有凭证渠道取得登录方式。IdP 是客户签发和管理身份令牌的服务；主体是令牌代表的用户或应用。scope 表示令牌声明的访问范围，group 表示身份分组，服务仍会将它们与自己的授权策略匹配。不要使用 Indexer 的后台目录身份作为 Agent 身份。

| 信息 | 从哪里取得 | 使用方式 |
|---|---|---|
| Gateway 的 HTTPS 地址 | 部署负责人提供的实际 Gateway 地址，对应 `SEMSEARCH_GATEWAY_URL` | 请求路径为 `/mcp` |
| 令牌签发方与受众 | 身份管理员确认的 `OIDC_ISSUER`、`OIDC_AUDIENCE` | 获取签发给本服务的访问令牌 |
| OIDC discovery 与 JWKS | 身份管理员确认的 `OIDC_DISCOVERY_URI`、`OIDC_JWKS_URI` | 由 Gateway 和拦截器使用，客户端不能自行指定验签地址 |
| Agent 的主体及授权范围 | 授权管理员配置的 `issuer`、`subject`、`scopes`、`groups` | 必须获准使用搜索工具和所需业务工具 |
| 搜索工具完整名称 | 后续 `tools/list` 的 `name` | 保留 target 前缀，不能只写 `search_tools` |
| MCP 协议版本 | 初始化响应中的 `protocolVersion` | 当前配置默认允许 `2025-03-26` |

OIDC 是身份服务发布签发方和登录配置的标准方式。JWKS 是该身份服务公开的验签公钥集合，不包含客户端私钥。配置及权限结构的完整说明见[服务身份配置](service.md#身份和授权)。

### 2. 获取短期访问令牌

令牌由客户 IdP 签发，本仓库不提供通用登录页面。访问令牌证明调用者获准访问目标服务；ID token 用于表示登录身份，不能代替本服务要求的访问令牌。交互式用户按客户已有登录流程取得访问令牌；该流程的授权端点、登录界面和 SDK 在本仓库中**未核实**。

如果客户为应用接入批准了 `client_credentials`，即用应用自己的凭证获取令牌，可使用下面的协议形状。它与仓库 [OAuthTokenSource](../src/semantic_service/catalog.py) 的请求一致：凭证放在 HTTP Basic 认证中，正文传授权类型和 scope。下面所有尖括号都是占位符；实际端点、scope 和客户端凭证必须来自客户批准的配置，不要把凭证写入源代码、命令历史或日志。

`/token` 是[配置示例](../infra/config.example.json)中的路径；实际调用时，应连同 `Host` 一起替换为客户批准的令牌端点。

```http
POST /token HTTP/1.1
Host: <客户批准的令牌端点主机>
Authorization: Basic <由客户端在内存中生成的凭证编码>
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&scope=<已批准并经过表单编码的scope>
```

下面是响应结构示例，令牌为占位符：

```json
{
  "access_token": "<访问令牌>",
  "token_type": "Bearer",
  "expires_in": 300
}
```

`300` 表示该示例令牌在签发后 300 秒到期，不是客户 IdP 的已验证默认值。服务允许的令牌寿命最多为 300 秒，即 `exp - iat` 不能超过 300；允许的时钟偏差最多为 30 秒。默认还要求 `token_use=access`，并检查签名、签发方、受众和时间字段。客户 IdP 的字段不同，须由管理员先核对配置，不能由客户端略过验证。上述限制来自 [Principal 与 OIDCConfig](../src/semantic_service/auth.py)。

应用使用客户端凭证并不自动获得 Indexer 身份。被配置进 `SEMSEARCH_MACHINE_PRINCIPALS_JSON` 的主体只允许初始化、`ping` 和列目录，不能调用搜索或业务工具。Agent 必须使用具备相应工具权限的独立主体。

### 3. 完成协议初始化

每个请求都携带 `Authorization: Bearer <访问令牌>`。声明可接受 JSON 或事件流响应；如果 Gateway 返回 `Mcp-Session-Id`，后续请求由 MCP 客户端继续携带。不要在故障报告中记录该头或访问令牌。

```http
POST /mcp HTTP/1.1
Host: <实际Gateway主机>
Authorization: Bearer <访问令牌>
Content-Type: application/json
Accept: application/json, text/event-stream

{
  "jsonrpc": "2.0",
  "id": "initialize-1",
  "method": "initialize",
  "params": {
    "protocolVersion": "2025-03-26",
    "capabilities": {},
    "clientInfo": {
      "name": "agent-client",
      "version": "1"
    }
  }
}
```

检查响应的 `result.protocolVersion` 在管理员批准的版本列表中，并确认 `result.capabilities` 包含 `tools`。然后发送不带 `id` 的初始化完成通知：

```json
{
  "jsonrpc": "2.0",
  "method": "notifications/initialized",
  "params": {}
}
```

后续请求使用已协商的 `MCP-Protocol-Version`。当前默认值来自[运行配置](../src/semantic_service/runtime.py)，握手和头部处理见 [MCPClient](../src/semantic_service/catalog.py)。实际协商结果须在目标 Gateway 验证。

### 看到什么算成功

初始化返回协议结果，初始化完成通知被接受；下一节的 `tools/list` 能返回搜索工具。仅拿到令牌不能证明工具授权已经配置。

### 出错怎么办

HTTP 401 或 403 时，先核对令牌的用途、签发方、受众和到期时间，再请管理员区分 Gateway 认证失败与拦截器授权拒绝。不要反复请求新令牌掩盖持久配置错误。初始化失败的分类见[错误处理](#我要处理错误或降级)；OIDC 和 JWKS 的值班步骤见[运维手册](operations-runbook.md#我要排查认证失败或-gateway-403)。

## 我要搜索并调用工具

### 1. 发现搜索工具

初始化完成后发送以下 JSON 正文。请求头沿用上一节；`id` 接受字符串或整数，本文使用字符串示例。

```json
{
  "jsonrpc": "2.0",
  "id": "list-1",
  "method": "tools/list",
  "params": {}
}
```

普通 Agent 只会得到单页搜索工具。不要为这个列表添加 `cursor`、`limit` 或 `pageSize`；当前拦截器要求此处的 `params` 为空。以下是[部署示例](../infra/config.example.json)采用的完整名称，实际部署应使用本次返回的 `name`：

```json
{
  "name": "discovery___search_tools",
  "description": "Discover authorized tools",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": {
        "type": "string"
      },
      "_auth_context": {
        "type": "string",
        "description": "Server-managed signed context. Clients must omit this field."
      }
    },
    "required": ["query"]
  }
}
```

`inputSchema` 是工具参数的机器可读定义，也称参数 schema。上述结构是最小参数 metadata 示例，完整名称的前缀由 Gateway 添加。示例没有长度约束和“禁止额外字段”的关键字；这不代表服务会接受任意输入。

| 字段或约束 | 客户端应当怎么做 | 服务端实际检查 |
|---|---|---|
| `query` | 传非空白字符串，说明想找的动作和资源 | 最多 2048 个 Unicode 字符，超过即拒绝 |
| `_auth_context` | 始终省略 | 拦截器删除客户端同名值，重新写入签名字符串；Search 再验签和绑定 |
| 其它搜索参数 | 不发送 | 运行时拒绝未知字段，包括客户端指定的 `top_k` |
| JSON-RPC `id` | 使用非空白、最多 256 字符且可编码为 UTF-8 的字符串，或绝对值小于 `10**256` 的整数；数字 ID 搜索须部署验收 | 拒绝浮点、布尔值、`null` 和复合类型；仅额外允许已验签整数匹配 canonical 十进制字符串 context，反向或非 canonical 写法拒绝 |
| 结果数量 | 由服务管理员配置 | `SEMSEARCH_TOP_K` 默认 5、上限 20；返回数量可能更少 |

这些是[参数检查](../src/semantic_service/_search_contract.py)、[签名绑定](../src/semantic_service/auth.py)和[服务配置解析](../src/semantic_service/config.py)的限制，不是性能测量。不要修改回读 schema 来“补齐”长度关键字：schema 内容参与授权摘要，修改后可能导致拒绝。

### 2. 发送搜索请求

```http
POST /mcp HTTP/1.1
Host: <实际Gateway主机>
Authorization: Bearer <访问令牌>
Content-Type: application/json
Accept: application/json, text/event-stream
MCP-Protocol-Version: 2025-03-26

{
  "jsonrpc": "2.0",
  "id": "search-1",
  "method": "tools/call",
  "params": {
    "name": "discovery___search_tools",
    "arguments": {
      "query": "查看客户记录"
    }
  }
}
```

查询内容可能发送到你配置的模型服务（或其代理）。默认示例不应包含访问令牌、个人秘密或业务记录正文；只写工具发现所需的动作与资源。实际出站规则由服务端 `allow_query_egress` 控制，详见[安全与数据流](security-and-data-flow.md)。

### 3. 先检查错误，再解析结果

以下是独立编写的合成空结果示例，展示成功响应的包装形式；空列表本身不是异常。

```json
{
  "jsonrpc": "2.0",
  "id": "search-1",
  "result": {
    "isError": false,
    "content": [
      {
        "type": "text",
        "text": "{\"tools\":[]}"
      }
    ]
  }
}
```

客户端按以下顺序处理：

1. 检查 HTTP 状态、JSON-RPC 的 `error`、响应 `id` 和 `result.isError`。任一检查失败都不要继续执行工具。
2. 成功时，从 MCP `content` 中读取文本内容，再把 `text` 解析为 JSON。下面的示例使用一个文本块，不要把 `content` 直接当工具数组。
3. 读取解码后的 `tools`。只把这些工具的描述符提供给 Agent；不要猜测或补回未返回的工具。

下面是一个通用库存工具描述符，用于展示非空结构。它是结构示例，**不是上述查询的实测结果**，工具名称也不是客户环境的可调用地址：

```json
{
  "tools": [
    {
      "name": "fixture-dev-tools-crm___read_customer",
      "description": "Read a customer record in the customer relationship system. Only this resource is affected, not related resources.",
      "inputSchema": {
        "properties": {
          "record_id": {
            "description": "Exact record identifier / 记录ID",
            "type": "string"
          }
        },
        "required": ["record_id"],
        "type": "object"
      }
    }
  ]
}
```

### 4. 按返回定义调用业务工具

从真实搜索结果取完整 `name`，按其 `inputSchema` 准备业务参数，再发起一次独立的 `tools/call`。下例继续使用上面的夹具名称与合成记录值，仅说明请求结构：

```json
{
  "jsonrpc": "2.0",
  "id": "call-1",
  "method": "tools/call",
  "params": {
    "name": "fixture-dev-tools-crm___read_customer",
    "arguments": {
      "record_id": "example-record"
    }
  }
}
```

### 看到什么算成功

搜索响应通过错误检查，解码后包含 `tools` 数组。非空结果具有可读取的 `name`、`description` 和 `inputSchema`；实际业务调用仍须按该工具自己的结果契约判断成功。不要把“搜到了工具”当作“业务操作已完成”。

### 出错怎么办

先检查完整工具名和参数是否来自同一份最新结果，再检查权限和令牌。空列表时尝试下节的查询写法，并请管理员确认业务工具已注册、已进入索引且对当前主体可见。业务调用失败时不能改用其他主体的搜索结果绕过权限。

## 我要提高搜索命中率

embedding 是把文本转换成数字向量，以比较语义接近程度；BM25 则根据词语重合程度排序。模型已启用且允许查询外发时，默认策略先使用向量，低相似度时可选择有正匹配的 BM25；向量不可用时也可降级为 BM25。具体策略和边界见[设计决策](design.md)与[已知限制](known-limitations.md)。

### 1. 写清动作和资源

| 意图 | 建议写法 | 原因 |
|---|---|---|
| 查看客户 | `查看客户记录` 或 `read customer record` | 动作和业务资源同时出现 |
| 更新商品 | `修改商品信息` 或 `update product` | 可利用当前动作及商品同义词 |
| 删除联系人 | `删除联系人记录` 或 `delete contact` | 减少“删除”单独匹配其它资源的机会 |
| 发现某类能力 | `查询订单状态的工具` | 写出要完成的业务动作，避免只写“查找工具” |

### 2. 必要时补充另一种语言

当前新索引的 `synonyms-v2` 对文档和查询对称扩展创建、读取、更新、删除，以及商品这一组资源词。例如“修改”与 `update`、“商品”与 `product` 可以相互补充。这是有限词表，不是完整翻译器；资源名称、缩写和复合词仍可能不匹配。词表见[新索引配置](../src/semantic_core/_index_config.py)。

先用一种语言把意图写完整。若目录主要使用英文，可以追加精确英文资源名，例如“查看客户 read customer”。不要连续堆叠大量同义词，也不要假设“查找工具”中的“查找”等同于读取业务记录。旧索引保留旧分词规则，升级词表后须由管理员重建索引。

### 3. 用业务样例检验结果

选择当前主体确实有权使用的工具，分别测试中文、英文、同义表达和跨资源表达。检查返回的描述符是否支持用户要做的动作，再决定是否调用。仓库的离线评测主要使用合成数据，不能代替客户实际目录验收。

### 看到什么算成功

返回工具的动作、资源和参数都符合需求，并通过真实业务工具的权限及输入检查。单次排名靠前不证明该工具一定合适。

### 出错怎么办

换成更明确的动作和资源。仍未命中时，向管理员提供脱敏后的查询意图、发生时间和客户端错误类别；管理员可核对索引、分词版本和降级情况。不要为排障发送完整令牌或秘密业务数据。

## 我要理解权限可见性

1. 普通 Agent 的 `tools/list` 只显示搜索工具。搜索工具本身也要单独授权。
2. 搜索在选取候选前过滤未授权工具，并在外发候选和返回前复核权限。最终结果只包含当前仍获准的完整工具描述符。
3. Gateway、target、工具完整名称、工具 schema 摘要和主体授权必须同时匹配。工具同名重建或 schema 改变后，可能需要重新注册和审批。

**成功标准：** 不同权限主体看到各自允许的结果；已撤销权限不能通过旧搜索结果继续调用。**出错怎么办：** 请授权管理员检查当前策略、工具摘要及安全目录，不能依靠修改 JWT 的 scope 或搜索参数自行授权。详细链路见[安全与数据流](security-and-data-flow.md)。

## 我要处理错误或降级

降级是服务在某项检索能力不可用时继续使用可用能力。例如 embedding 超时后使用 BM25，可能影响语义匹配质量，但权限检查仍然执行。认证、授权或索引安全校验失败时会拒绝请求，不会用降级绕过。

### 1. 区分协议错误与服务端诊断

非 200 响应（包括 HTTP 500）或超时不能视为成功或空结果，也不能据此判定某个后端是否执行。
保留发生时间、HTTP 状态及响应中已有的请求 ID，去除令牌、认证头和敏感业务内容后联系运维。
只读搜索可按应用等待预算采用有界退避重试；认证或授权拒绝应先核对身份与权限。
有副作用的业务调用须先确认执行结果与该工具的幂等要求，不得仅因错误或超时自动重发。

REQUEST/Target 抛出的本地异常可能在客户端表现为 Gateway 通用错误/HTTP 500；本项目没有为每个内部异常定义稳定的公开错误码。工具错误可能通过 `result.isError=true` 表示，因此 HTTP 200 不能单独证明工具成功，客户端也不能按错误文本猜测具体内部异常。

下表是供开发者与运维沟通的**本地异常及日志状态**，不是承诺会原样出现在 Gateway 响应中的错误码。

| 本地异常 / `status` | 含义 | 下一步 |
|---|---|---|
| `InputError` / `invalid input` | 参数、请求形状或标识不合法 | 检查字符串或整数 `id`、非空 `query`、长度和未知参数；修正后再调用 |
| `AuthenticationError` / `unauthenticated` | 令牌或内部签名验证失败 | 检查令牌用途及有效期；若数字 ID 搜索全部 unauthenticated、字符串 ID 正常，排查 Gateway 丢失 ID 类型及混合版本部署；当前版本支持有界 canonical 单向匹配。ECS/fallback admission 或 Search 执行日志的 `binding_match="mcp_id_str_coerced"` 证明该次封套匹配成功，不代表整个搜索成功；facade 和私有响应不带此标记 |
| `AuthorizationError` / `unauthorized` | 当前主体或工具不可用 | 请管理员核对搜索权限、业务权限及 schema 摘要；重试不能增加权限 |
| `IndexNotReadyError` / `index not ready` | 没有可验证的当前索引 | 等待运维修复或发布索引；不能视作成功空列表 |
| `RateLimitError` / `rate limited` | 主体当前分钟调用已达上限 | 降低并发和调用频率，在客户端设置有限重试 |
| `ServiceError` / `internal failure` | 配置、依赖或内部处理失败 | 保留时间和脱敏错误类别，按运维流程定位 |

固定异常定义见 [auth.py](../src/semantic_service/auth.py)。MCP 之外出现的 HTTP 401、403、429 也可能来自 Gateway 或上游基础设施，不能仅凭状态码定位到某个函数。

### 2. 正确理解空结果与降级

| 现象 | 能确认什么 | 不能据此推断什么 |
|---|---|---|
| `tools` 为空 | 这次成功响应没有返回工具 | 不能断言整个目录为空，或当前主体没有任何权限 |
| 运维日志 `degraded=true` | 所需能力不可用或失败 | 不能直接断言本次请求失败 |
| `vector_availability=failed` | 尝试 embedding 时发生错误 | 具体原因要看 `embedding_error` |
| `vector_availability=unavailable` | 需要的向量能力未可用 | 不一定调用了模型，也不一定是模型服务故障 |
| `vector_availability=disabled` | 当前请求没有启用向量 provider | 可能是模型关闭或主体禁止查询外发，不等于异常 |
| `ranking_mode=bm25` | 本次实际采用词法排序 | 可能是配置允许的正常路径，也可能是降级 |

这些字段只在服务日志中出现，不加入 `{"tools":[...]}` 公共响应。客户端不能从返回结构识别降级，也不能选择关闭权限检查或强制外发查询。日志口径见[服务诊断字段](service.md#目录与调度)。

### 3. 控制重试与调用频率

应用限流按主体、按固定分钟计数。普通主体和后台机器主体各自的默认上限都是每分钟 600 次，分别由 `SEMSEARCH_SUBJECT_CALLS_PER_MINUTE` 和 `SEMSEARCH_MACHINE_CALLS_PER_MINUTE` 配置；含初始化、通知、`ping`、列工具和工具调用。600 是配置默认值，表示同一主体这一分钟内最多被入口接纳 600 次，并不是吞吐实测或客户获批的 Gateway 配额。

该实现不是平滑限速，分钟交界处仍可能出现突发。Agent 应复用正常连接，避免为每次搜索重复初始化，并设置应用级并发和有限重试。内部 `RateLimitError.retry_after` 不保证映射为公开 `Retry-After` 响应头。Gateway、账号、Lambda 和模型侧另有上限，见[配额工作表](quotas.md)。

**成功标准：** 客户端有可控的重试次数，限流后调用速率下降，不重复执行有副作用的业务动作。**出错怎么办：** 持续限流时请运维核对各层配额和实际流量；不要用更换主体或循环取令牌绕过限制。

## 常见问题

| 问题 | 回答 |
|---|---|
| 为什么 `tools/list` 只有一个工具？ | 这是普通 Agent 的预期入口。用搜索工具获取当前需要且已授权的业务工具。 |
| 为什么 schema 有 `_auth_context`，却要求不填写？ | 该字段承载拦截器写入的内部签名，客户端值不会被信任。 |
| 能传 `top_k`、账号、Gateway ID 或权限列表吗？ | 不能。搜索客户端只提交 `query`；身份、资源边界和结果数量由服务端配置。 |
| 搜索返回工具后，可以一直缓存并使用吗？ | 可以按应用策略缓存定义，但每次业务调用仍会重新检查权限。定义更新或调用被拒绝时应重新搜索，不能依赖缓存绕过。 |
| 中英文一定有相同结果吗？ | 不保证。有限同义词、目录描述、向量可用性和索引版本都会影响排序。 |
| 第一条请求为什么比后续请求慢？ | 冷启动时 AWS 需要准备运行环境，应用也可能首次构造客户端和加载索引。当前实测及优化代价见[已知限制](known-limitations.md)。 |
| 搜索是否会泄露无权使用的工具？ | 实现要求在候选截取前过滤，外发及返回前复核；上线仍须通过多主体授权验收。完整边界见[安全与数据流](security-and-data-flow.md)。 |
| 返回空列表后应自动调用一个相近的已知工具吗？ | 不应。先澄清意图或重新搜索，避免误选资源或使用已撤销的工具。 |
