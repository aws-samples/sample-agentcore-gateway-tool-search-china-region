# Embedding 错误诊断

本文解释查询 embedding 失败时的日志字段和分类，供值班运维及维护者定位降级原因。Embedding 是模型把文本转换为检索向量的过程，在线查询时发生；预算与实测见下文及[已知限制](known-limitations.md)。它支撑[项目总览](overview.md)中模型不可用时仍能提供授权词法搜索的目标。读完后，你可以区分超时、HTTP 错误和响应校验失败，而不依赖原始敏感错误正文。

Search 的结构化日志新增 `embedding_error`。它属于可信诊断回调和 stdout 日志，
不会加入 Gateway 的 `{"tools":[...]}` 响应，也不改变工具 schema、授权、排名、降级或
错误的原有 `ErrorCode`。没有 embedding 异常时为 `null`，每次请求独立初始化。
日志默认 30 天留存。允许清单响应头按 provider 可控的有界整数写入，不保留原始头文本。

## 字段与含义

| 字段 | 含义 |
| --- | --- |
| `kind` | 下表列出的受控类别；不记录异常类名、原始 message 或 traceback |
| `http_status` | 已观察到的实际 HTTP 状态；JSON 或语义校验失败仍保留实际 2xx 状态。未观察到状态时为 `null` |
| `retry_after_seconds` | 仅 HTTP 429 和 503 的整数形式 `Retry-After`；不可接受或缺失时为 `null` |
| `rate_limit_headers` | 仅 HTTP 429 的六个允许清单头，键为固定小写名称，值为有界整数；默认 `{}` |

| `kind` | 判断依据 |
| --- | --- |
| `http_rate_limited` | 收到 HTTP 429；只能确认状态，不能确认 RPM/TPM 或错误来自百炼 |
| `http_client_error` / `http_server_error` | 其它 4xx / 5xx；例如 408 和 504 仍保留原来的 TIMEOUT 错误码 |
| `http_status_error` | 其它非成功状态，例如不跟随的重定向 |
| `connect_timeout` / `read_timeout` | HTTP 客户端报告连接 / 读取超时 |
| `write_timeout` / `pool_timeout` | HTTP 客户端报告写入 / 连接池超时 |
| `deadline_exceeded` | 原有本地绝对期限检查失败；若 `http_status` 非 `null`，表示已收到响应，但本地总预算已耗尽，不表示上游返回了超时状态 |
| `network_error` | HTTP 客户端报告其它请求或网络错误 |
| `response_parse` | JSON、内容解码、响应身份、向量或其它结构校验失败 |
| `timeout` | HTTP 客户端报告无法进一步细分的超时；HTTP 路径可达，应计入超时统计，不能仅凭该类别定位具体阶段 |
| `authentication` / `rate_limited` | 为兼容自定义 provider 保留的通用类别；普通 HTTP 401/403 使用 `http_client_error`，429 使用 `http_rate_limited` |
| `invalid_request` / `unavailable` | provider 只提供原错误码、没有更精确的传输信息 |
| `unknown` | 其它异常；不记录异常文本 |

例如下面是**合成示例**，不是现场观察结果：

```json
{
  "embedding_error": {
    "kind": "http_rate_limited",
    "http_status": 429,
    "retry_after_seconds": 2,
    "rate_limit_headers": {
      "x-ratelimit-remaining-requests": 0
    }
  }
}
```

允许清单为 `x-ratelimit-limit-requests`、`x-ratelimit-remaining-requests`、
`x-ratelimit-reset-requests`、`x-ratelimit-limit-tokens`、
`x-ratelimit-remaining-tokens`、`x-ratelimit-reset-tokens`。所有头值必须为 1–10 个
ASCII 十进制数字，数值在 0–2,147,483,647 内；否则整个值被忽略，不截取。
日期、小数、单位、符号、重复头拼接、超长字符串及其它头均不记录；
例如 `reset` 返回 `1s` 或 `6m0s` 时会被忽略。
[HTTP 标准](https://www.rfc-editor.org/rfc/rfc9110.html#name-retry-after)
允许日期形式的 `Retry-After`；本诊断有意只接受整数形式，不代表日期响应不合法。

不猜测 `reset` 的单位或时间起点，不根据头值改变等待或重试。
未找到百炼保证返回或代理保证透传这些头的契约；头缺失不能排除限流，
头存在也不能证明它来自百炼。请求正文、query、密钥、完整 URL、任意响应头和错误正文
均不进入新增字段。错误响应在原状态检查处被拒绝，不为诊断额外读取正文。

## 传递范围与运行成本

[共享 HTTP 层](../src/semantic_core/_http.py)构造受控错误信息；
[ProviderError](../src/semantic_core/providers.py)保留不可变的诊断对象。
embedding、chat、rerank 和评测 selector 在补充 usage、重新包装异常时保留该对象。
生产凭证轮换包装器在认证失败后的 secret 刷新或配置校验失败时，也保留此前已采集的
诊断；原有 `ErrorCode` 不变，后续请求不会继承该对象。未采集的响应头不会被补猜。
Search 仅捕获 embedding 异常并写入请求内诊断，Lambda 日志出口将头转换为新的数值字典。
其余转发用于一致保留元数据，不新增 rerank 错误输出；Search 的 rerank 仍只记录
粗粒度 `rerank_state`。

错误诊断本身没有新增 HTTP 请求、重试、等待、secret 访问或连接池。429 仍按原路径降级；
已有 401/403 凭证刷新机制及其次数不变。当前源码的 embedding 默认为 1000 ms，
Search 总预算默认为 3.0 s；配置优先级、实际生效值核验及授权收尾预留见[可配置预算与授权收尾](#可配置预算与授权收尾)。
错误分类本身不解决模型出口的额外耗时，也不构成冷 p95 4 s / 热 p95 1.5 s 达标证明。

CDK 为每个受控 `embedding_error.kind` 定义计数过滤器，指标值固定为 `1`，
不把 provider 头值用作指标值或维度。仅部署 Lambda 代码不会创建这些过滤器；
部署相应 CDK 后需核对日志匹配与计数入库。

部署后应按 `aws_request_id` 和日志流关联平台 REPORT，再比较冷/热调用的错误类别、
向量可用率及端到端 p95。开发环境可能同时处理其他调用，不能仅按空闲时间判断冷启动，也不应
修改 Lambda 配置来制造冷启动。只有用户批准后才能部署并采集新增字段。

## Embedding 阶段与 HTTP 出口计时

Search 的可信诊断回调和结构化日志还包含 `embedding_timing`，不加入工具响应、
schema 或业务返回值。没有进入 embedding 预算检查时为 `null`；预算检查已发生但
余额不足时，保留零余额及空 `attempts`，该路径不会发起 HTTP 请求。
每次查询独立收集，嵌套或并发查询不会共享记录；计时故障只设置
`diagnostics_failed`，不改变业务结果。

不能仅凭空 `attempts` 推断未发 HTTP：自定义 provider 跨线程调用时，如果没有传播
contextvar 诊断上下文，也可能没有采集到尝试，且 `diagnostics_failed` 仍为 `false`。
当前出厂 provider 在同一线程同步调用；替换 provider 后，应同时核对其执行路径、
`stage_budget_ms` 和外部记录，不能把未采集等同于未调用。

| 字段 | 含义 |
| --- | --- |
| `search_remaining_ms` | 进入 embedding 预算检查时的 provider 预算余额；仅 Lambda 剩余期限分支已扣除收尾余量 |
| `stage_budget_ms` | 同一时刻 embedding 实际可用余额；受 Search 剩余预算和 embedding 配置两层限制 |
| `configured_timeout_ms` | 本次配置的 embedding 子预算，默认 1000；不是实得额度 |
| `attempts` | 最多两次已采集的 HTTP 尝试，各自独立计时；不含模型 secret 读取，空列表的两种含义见上文 |
| `attempts_truncated` | 自定义 provider 产生超过两次 HTTP 尝试时为 true；不增加或重试请求 |
| `diagnostics_failed` | 计时器或记录器异常导致诊断不完整；不能以缺失阶段推导连接复用 |

每个 `attempts` 元素只含下列固定字段。所有时间为有界非负毫秒；
缺失或不可观测为 `null`，不填零，不保存 URL、host、凭证、请求/响应正文、
任意响应头、网络对象或异常文本。

| 字段 | 含义 |
| --- | --- |
| `attempt` / `effective_timeout_ms` | 尝试序号 1 或 2；本次 HTTP 的 connect/read/write/pool 超时额度 |
| `http_status` | 已看到的 HTTP 状态；不表示已收到完整有效向量 |
| `tcp_ms` | HTTPcore 的 TCP 建连阶段，包含 DNS；失败时也保留已观察时长 |
| `tls_ms` | TLS 握手阶段，失败时也保留已观察时长 |
| `response_headers_ms` | 从本次 HTTP 开始到完整响应头可用，包含建连和服务器等待；不是纯网络首字节时间 |
| `first_body_chunk_ms` | 从本次 HTTP 开始到应用拿到首个非空解码正文块；压缩正文可能先经过解码 |
| `body_ms` | 正文迭代开始到完成或中断，包含网络等待、解码和本地检查 |
| `total_ms` | 本次 HTTP 包装器的全部耗时，含响应读取与 JSON 解析，成功失败均记录 |
| `connection_state` | `new_connect_observed`：看到新 TCP；`existing_connection_inferred`：看到发送但无新建连事件；`unknown`：不足以判断 |
| `body_complete` | 正文迭代正常结束；不保证 JSON、模型身份或向量校验通过 |
| `diagnostics_failed` | 此次尝试的诊断发生异常 |

HTTPX 0.28.1/HTTPcore 1.0.9 的 trace 不提供 wire 首字节事件，也不单独提供
连接池等待时间。这里分别记录完整响应头和首个应用正文块，避免把它们伪称为 wire
TTFB。`response_headers_ms`、`first_body_chunk_ms` 是从同一起点累计的时间，
`tcp_ms`/`tls_ms`/`body_ms` 是包含在其中或 `total_ms` 中的阶段，不能全部求和。
无 trace 的自定义 transport 仍可记录响应头/正文，连接状态保持 `unknown`。
401/403 刷新后的尝试使用第二条记录，继续沿用原期限；secret 刷新耗时不归入 TCP/TLS。

## 可配置预算与授权收尾

`MODEL_EMBEDDING_TIMEOUT_SECONDS` 默认 `1.0` 秒，允许 `(0, 6]` 内有限秒数，
同时控制检索核心子预算和模型 HTTP timeout。`SEMSEARCH_SEARCH_TIMEOUT_SECONDS`
默认 `3.0` 秒，允许 `(0.3, 6]` 内有限秒数，仅分发给 Search。
固定版本 S3 配置覆盖环境变量；其中显式 `0.45`/`1.5` 秒仍覆盖新默认。
只有 S3 和环境变量都省略相应字段时，新代码才采用 `1.0`/`3.0` 秒。
CDK 使用同一 parser 验证，但不会将省略的这两个字段自动序列化为默认值。
800/1000 ms 档应分别填
`0.8`/`1.0` 秒；`800`/`1000` 超过上界，会被拒绝。

Search 在 handler 内开始计时。先从 Lambda 实际剩余期限扣除 **300 ms**，
为 BM25 与最终授权收尾留余量，再与配置期限取较早者；配置期限本身不扣这段余量：

```text
provider_deadline = min(started + search_timeout, started + Lambda_remaining - 0.3)
embedding_deadline = min(provider_deadline, embedding_started + embedding_timeout)
HTTP_timeout = min(embedding_remaining_after_secret, embedding_timeout)
```

正常非 Lambda 调用缺少 remaining 方法（`get_remaining_time_in_millis`）时，
provider deadline 为 `started + search_timeout`，请求级布尔诊断
`lambda_deadline_ignored=false`。该属性存在但不可调用、返回值不可解析
（包括非有限或负数），或属性读取/方法调用抛错时，也沿用配置期限，并记
`lambda_deadline_ignored=true`，不因此把请求改成 500。
工厂构造在计时起点前，仍会消耗 Lambda 剩余时间；签名、授权和快照加载在起点后，
模型 secret 读取在 embedding 子阶段内。Lambda 余量充足时，默认 provider
软预算为 3.0 秒，默认 embedding 子预算为 1000 ms。
Rerank 默认仍为 450 ms，并共享 provider 总期限。预算用尽也始终执行原有最终授权，
不返回未经复核的缓存结果。

这是协作预算，**不是硬取消或授权耗时保证**。SDK 读操作与 HTTP 分操作 timeout
仍可能超过绝对期限，现有截止检查会拒绝迟到结果，最终授权不能跳过。
比较 450/800/1000 ms 时应使用相同的 Search 总预算，并核对 `stage_budget_ms`
与 `effective_timeout_ms`；只放宽子预算、却让前置加载吃光总余额，不能测到子预算效果。
对照算例：前置 1.3 s、Search 总预算 2.5 s，且处理起点的 Lambda 剩余时间不少于 2.8 s 时，
embedding 检查时还有 1.2 s 总余额，三个档位分别可得 450/800/1000 ms 子预算；
首次模型 secret 读取仍可能缩短实际 HTTP 额度。若 Lambda 余额更短，应按上式另算，
不能把被截断的样本当作完整档位。
当前代码默认 embedding 1.0 秒、Search 3.0 秒；部署时分别核对实际资产与固定配置。设计预算不构成生产 SLO 达标证明。

## 429 后续处理

[百炼错误码](https://help.aliyun.com/zh/model-studio/error-code)及
[兼容接口说明](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope)
将 429 用于请求或 token 速率、突发、并发、平台容量，以及部分预算、账单或订购问题。
代理还可能自行生成或重写状态。实际配额与同上游账号的所有调用方仍需核对，
不能把单个 429 当成自动提额、重试或迁移模型的依据。

确认原因后的选项和代价见[托管选择](embedding-hosting.md#限流确认后的选择)。
