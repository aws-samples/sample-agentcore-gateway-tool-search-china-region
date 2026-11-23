# 配额工作表

本文是部署前填写的配额工作表，供账号管理员、服务所有者与部署负责人使用。它把[项目总览](overview.md)中的规模目标转换为账号与区域的资源需求。读完后，你可以记录实际额度、其他业务占用和申请状态；公开默认值不能替代目标账号的有效额度。

此工作表对应[设计第 1.1、9 节](design.md)，是部署放行输入。公开默认值不等于
目标账号额度；请自行查询目标账号额度并记录依据。
部署负责人须为每个目标账号、每个 Region 分别复制填写，记录查询时间、额度证明、
其它业务占用和批准单。以下责任人为岗位，具体接手人由部署任务指定。

| 放行字段 | 当前值 |
|---|---|
| 账号、stage、Gateway 清单 | 待部署负责人填写 |
| Region | 北京 `cn-north-1` / 宁夏 `cn-northwest-1`，逐区填写 |
| 规模与预算确认 | 待业务负责人确认；以下使用设计假设 |
| 实际额度、区域适用性和已有占用 | 全部待真实验证；不得把公开默认值填入实际值 |
| 提额申请与生效证明 | 未申请；实际值满足需求且留足余量才标记“不需申请” |
| 负载试验与限流阈值 | 待离线评测与单独批准的部署验证 |
| 放行责任 | 部署负责人汇总；服务所有者、账号管理员、预算负责人签认 |

## 计算口径

`G` 为 Gateway 数，`Q_s` 为总搜索 QPS，`b` 为每次搜索后的业务调用数，`Q_b=b×Q_s`，
`Q_boot` 为 bootstrap、初始化和 ping 的实测 TPS，`P` 为每 Gateway 每轮所有
`tools/list` HTTP 请求数，`r` 为重试预留。缺少一致性 token 时 服务 会完整读取两遍，
因此 `P` 必须计入两遍分页，不能只填每遍页数。
以下计算以每遍七页、两遍共 `P=14` 为规划示例，分页大小不是服务保证。
部署时记录实际 `nextCursor` 和每页数量；不要假设请求的 limit/pageSize 会生效。
500 工具或其他规模须重新量测 `P`。

```text
T_gateway = (Q_s + Q_b + Q_boot + G × P / 60) × (1 + r)
quota_required = ceil(T_gateway / 0.70)
T_scheduler_average = G / 60 + G / 300
monthly_scheduler_invocations = (G / 60 + G / 300) × 30 × 86400
```

30% 余量、10% 重试预留、每次搜索后 2 次业务调用均为设计假设。预算按实际分页、
协议往返、流量峰值和其它应用占用重算。Indexer 的控制面读取单独计数。
Scheduler/Lambda tick 重试为零，不把 10% 业务重试预留解释成允许后台旧 tick 重试。

| 情景 | 假设和计算 | 规划目标 |
|---|---|---|
| 基线：20 Gateway，各 2 search QPS | `Q_s=40, b=2, Q_boot=1, P=14, r=0.1`；约 138 TPS | 账号 250 TPS；待部署目录实际`P`修正 |
| 突发：20 Gateway，总 200 search QPS | 同上；约 666 TPS | 账号 1,000 TPS；未获批先限制入口预算 |
| 扩展：100 Gateway，各 2 search QPS | `Q_boot=5`，其它同上；约 691 TPS | 账号 1,000 TPS；各 Gateway 单独限制 |
| 单 Gateway 20 search QPS | 搜索、业务、bootstrap、分页、重试共享该 Gateway 配额 | 按实际公式核对，不能仅比较 20 与 200 |

## Gateway 与协议

公开值依据 [AgentCore quotas](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html)；
中国区能力边界依据[设计第 3.1 节](design.md#31-中国区事实)。调整范围和目标账号
其它应用占用仍须逐区确认。

| 配额 / 作用域 | 公开默认值 | 目标账号实际值 | 需求值 / 检查方法 | 申请状态 / 责任人 |
|---|---|---|---|---|
| tool-call / tool-list，单 Gateway | 200 TPS，可调整 | 待真实验证，含剩余额度 | 每 Gateway 按 `T_gateway` 公式计算，所有调用合计不超过可用值的 70% | 未申请 / 服务所有者、账号管理员 |
| tool-call / tool-list，账号/Region | 200 TPS，可调整 | 待真实验证，含其它 Gateway/应用 | 基线建议 250，突发/扩展建议 1,000 TPS；扣除其它应用占用 | 未申请 / 账号管理员 |
| Global 原生 search-based tool-call | 25 transactions/min，可调整；公开表未明确 account/Gateway 作用域 | 待真实验证；China 无原生 semantic search | 2 QPS 已为 120 TPM；若每 Gateway，建议 200 TPM/Gateway；若账号共享，20 Gateway 建议 4,000 TPM/account/Region | 作用域及提额均待确认 / Global 服务所有者；禁止承诺两后端同吞吐 |
| targets / Gateway | 100，可调整 | 待真实验证 | 业务 target + Search target + 其它 discovery target 全部计数；20,000 工具至少 20 个业务 target，另加 Search | 未申请 / Gateway 管理员 |
| tools / target | 1,000，可调整 | 待真实验证 | 每 target 独立计数；工具总数不能掩盖单 target 超限 | 未申请 / 工具发布负责人 |
| 同 Gateway 在途 Create/Update/DeleteTarget | 合计 5，可调整 | 待真实验证 | 发布器最多 4 个在途，保留 1 个余量；100 次/min 突发排队 | 未申请 / 工具发布负责人 |
| inline schema / target | 1 MB，可调整 | 待真实验证 | UTF-8 序列化后逐 target 测字节；包括 Search `_auth_context` schema | 未申请 / 工具发布负责人 |
| S3 schema / target | 10 MB，可调整 | 待真实验证 | 测实际对象和 API 包装，不从工具数推算 | 未申请 / 工具发布负责人 |
| tool-call/list/search payload | 公开配额6 MB，可调整；不等于对称HTTP输入/输出上限 | 目标账号与生产payload待验证 | 分别验证请求输入、Lambda 输出和最终 MCP 包装的边界；descriptor≤128KiB、业务响应≤512KiB、本地list响应≤6MiB保持 | 未申请 / 服务所有者 |
| 控制面 GetGateway / GetGatewayTarget / ListGatewayTargets | 本工作表未取得足够的独立公开默认值 | 待真实验证；逐 action 填 | 每轮前后 READY/fingerprint、分页和重读成本：`G × 每轮该 action 次数 / 60`，再加发布和运维读取 | 待查询并申请 / 账号管理员 |
| SynchronizeGatewayTargets | 本工作表未取得独立速率配额；不能套用上面的 5 个在途操作 | 待真实验证 | 每请求 1 target；100 DEFAULT targets 为 100 次同步加 READY 轮询；独立发布通道每 Gateway 最多 1 个在途 | 待查询并申请 / 工具发布负责人 |
| OIDC、MCP initialize / ping、token 刷新 | 按 IdP 和实际 Gateway 计量合同核实 | 待真实验证 | 填实际请求数、token/cache TTL、失败重试；不能作为免费流量省略 | 待核实 / 身份负责人、服务所有者 |

输入 JSON-RPC、Lambda 响应提交和最终 MCP 包装属于不同字节边界，不能视为对称的
HTTP 上限。保留应用预算，并在目标环境分别验证大 tools/list、Unicode/转义、
streaming、interceptor 与实际 schema；不要根据单个成功样本提高预算。

## Lambda

依据 [Lambda quotas](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html)
和[reserved concurrency](https://docs.aws.amazon.com/lambda/latest/dg/configuration-concurrency.html)。
新账号可能低于公开默认并发值。`L_s`、`L_i` 分别表示与当前配置一致的 Search、
REQUEST 平均执行秒数，必须由对应负载测量取得。平均值公式只用于初估，最终须覆盖
实测并发峰值、冷启动和刷新。

REQUEST 当前尺寸为1769 MiB，已经替代旧256 MiB配置。旧尺寸下的 `L_i` 及依赖它的
`C_request`、`C_reserved_total`、`C_account_required` 均失效，不能用于本次配置放行。
GB-s表示配置内存的GB数乘计费秒数。成本中的内存倍率或GB-s倍率也不能直接代入执行时长。须按新尺寸重新取得冷、热及
拒绝请求的执行分布和并发峰值，再重算需求；来源见[函数配置](../infra/functions.py)。
REQUEST 的内存选择应同时核对平台计费时长、应用初始化、请求耗时、峰值内存和 GB-s。
检索快照的 60% 内存门槛不适用于不加载快照的 REQUEST；初始化缩短也不必然降低费用。
当前尺寸不是成本最优承诺，部署前按目标流量测量并核实并发需求。

当前[监控定义](../infra/monitoring.py)没有REQUEST专用的内存、初始化或Duration告警；
唯一默认Duration告警针对Search。REQUEST已有错误、节流及认证恢复等信号，不能代替
尺寸和成本验收。增加REQUEST专用监控及设置回退判据仍需评审，不在本工作表中宣称已部署。

## DynamoDB

依据 [DynamoDB quotas](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/ServiceQuotas.html)、
[partition key capacity](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-partition-key-design.html)
及[事务计量](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transactions.html)。
每Gateway两张独立物理表：state保存CONTROL/rate/cache/progress，
protected保存PROTECTED。两表均为on-demand、字符串`pk`、AWS_MANAGED KMS加密、
PITR、deletion protection及RETAIN；只有state有`expires_at` TTL，protected无TTL。
授权/安全控制来自强一致基表读取，不使用GSI。每次控制视图读取按
`PROTECTED → CONTROL → PROTECTED`执行三个强读，前后受保护状态变化即拒绝。
管理员专写的`PROTECTED`保存policy epoch/ref和tombstone；`CONTROL`保存内容指针，
writer仅能在写事务中检查受保护状态，不能更新它。计算成本时须计入三个强读及
跨表事务写入/ConditionCheck的单位倍数，并分别计入各表峰值。

```text
RRU/s = sum(strong_read_requests/s × ceil(item_bytes / 4096))
WRU/s = sum(write_requests/s × ceil(item_bytes / 1024))
transactional_units = 2 × equivalent_nontransactional_units
table_requirement = ceil(measured_peak_units / 0.70)
```

失败条件写、重试、缓存失效及控制记录重复读取成本以实际 consumed capacity 核对。
on-demand 不消除单键热点；不能通过把强读改为最终一致或缓存旧授权来降低负载。
设计第9.3节的8 RRU/search是拆分前的估算，不能直接作为本分支的容量或账单值；
部署预算需按当前三个强读和调用链中的重新授权次数重算。

| 配额 / 作用域 | 公开默认值 | 目标账号实际值 | 需求值 / 检查方法 | 申请状态 / 责任人 |
|---|---|---|---|---|
| on-demand 单 table | 40,000 RRU/s 和 40,000 WRU/s，可调整 | 待真实验证，分别记录state/protected自设上限 | state计CONTROL强读、limiter/CAS/progress写；protected计两次强读、事务检查及管理员写；逐表填峰值和余量 | 未申请 / 数据平台负责人 |
| provisioned 账号/Region（可选） | 80,000 RCU/s、80,000 WCU/s；on-demand 不适用该账号项 | 待真实验证；当前不使用 | 切换 provisioned 前汇总所有 table/GSI | 不适用；扩展时重开 / 数据平台负责人 |
| 热分区 | 每分区 3,000 read units/s、1,000 write units/s | 待真实负载验证分布 | 单Gateway的CONTROL/PROTECTED强读（后者每次视图读两次）与单subject每分钟limiter键写分别计算；单键峰值不以全表容量替代 | 无默认架构变更 / 服务所有者 |
| limiter 固定分钟键 | 应用默认普通/机器主体各 600 calls/min；不是 AWS quota | 待客户确认最终值 | 条件 UpdateItem，分钟边界可双窗口突发；每次入口调用计一次，不是全账号 token bucket | 待流量预算确认 / 服务所有者 |
| table 数，账号/Region | 初始 2,500，可调整 | 待真实验证，含已有表 | 本方案`2G`张table；20/100 Gateway对应40/200张，另计隔离恢复表 | 待核实 / 账号管理员 |
| PITR 与恢复 | 服务配置/恢复任务限制，不能替代内容回滚 | 待真实验证 | 两表独立计存储/备份/恢复需求；相同恢复时间不证明事务一致。恢复到隔离表，保留最新授权/撤销/tombstone，禁止旧表直接替换在线权威 | 待恢复演练 / 数据平台负责人、授权管理员 |

## Scheduler 与可选队列

依据 [Scheduler quotas](https://docs.aws.amazon.com/scheduler/latest/UserGuide/scheduler-quotas.html)、
[SQS message quotas](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/quotas-messages.html)
和 [Lambda SQS 配置](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-configure.html)。
China 的实际生效 quota 必须单独填写；Global 文档的“其它支持 Region”默认值不能作为
目标账号已经获批的证据。默认没有 SQS、DLQ、CloudTrail 事件链或托管 redrive。

| 配额 / 作用域 | 公开默认值 | 目标账号实际值 | 需求值 / 检查方法 | 申请状态 / 责任人 |
|---|---|---|---|---|
| Scheduler target invocation / account/Region | 主要 Global Region 1,000 TPS；其它支持 Region 500 TPS，可调整 | 北京、宁夏各待真实验证 | 平均 `G/60+G/300`，20 Gateway=0.4 TPS、100=2 TPS；同相位最坏 `2G` 次突发需验证，错开启用相位 | 未申请 / 平台负责人 |
| Scheduler schedule 数 / Region | 10,000,000，可调整 | 待真实验证，含其它 schedule | 每 Gateway 2 个；20=40、100=200 | 待核实 / 平台负责人 |
| Scheduler group 数 / Region | 500，可调整 | 待真实验证 | 每 stack 一个 group，含环境和其它应用 | 待核实 / 平台负责人 |
| Create/Update/DeleteSchedule TPS | 主要 Global Region 各有不同值；其它支持 Region各 250 TPS，可调整 | 待真实验证 | 部署变更速率与回滚合计，部署器限速；不影响 60s 业务轮询预算 | 待核实 / 部署负责人 |
| Scheduler Input | 256 KB，不可调整 | 待部署配置核对 | 仅 `{"time":"<aws.scheduler.scheduled-time>"}`，不携带可控制 namespace 的字段 | 不需提额 / 平台负责人 |
| Scheduler retry / event age | 资源配置项 | 待部署配置核对 | 两 schedule均 0 retry、60s max age；FlexibleTimeWindow OFF；旧 tick 不积压 | 不需提额 / 平台负责人 |
| Lambda 异步 retry / event age | 资源配置项 | 待部署配置核对 | 两后台 alias均 0 retry、60s max age；监测 AsyncEventsDropped/EventAge，漏 tick由下一轮恢复 | 不需提额 / 服务所有者 |
| SQS 吞吐与队列数（可选） | Standard 高吞吐；FIFO 非高吞吐每 partition/action 300 TPS，batch 最多 10 条 | 未启用；具体 China 类型和额度待真实验证 | 扩展设计填 producer/consumer TPS、合并率、in-flight、队列/消费进程上限；不能直接给 Indexer 加并发 | 不适用；启用前单独批准 / 平台负责人 |
| SQS 消息大小 / retention（可选） | 1 MiB；默认 4天，可配 1分钟–14天 | 未启用；China 实际能力待真实验证 | 只发送重读提示；过期提示丢弃，不能重放旧 catalog；retention按故障恢复时限定 | 不适用 / 平台负责人 |
| SQS visibility / batch / in-flight（可选） | visibility默认30s，0–12h；batch≤10；in-flight按队列类型另核 | 未启用 | Lambda触发需 `visibility ≥ 6×function_timeout + batching_window`；填最大积压、超时、消费进程数和 DLQ retention | 不适用；启用前申请 / 服务所有者 |
| SQS DLQ redrive（可选） | 中国区不支持托管 DLQ redrive | 不适用 | 仅可审批后受控重放提示、去重、过期检查；不依赖 StartMessageMoveTask | 不适用 / 平台负责人 |

## 完成条件

部署负责人逐行填写实际值、验证来源、剩余容量、申请编号及明确负责人；不适用的行
写明原因。必须先批准提额和预算，再执行真实负载；单 Gateway 与账号共享额度同时
满足公式。内存检索拐点评测与 Gateway/account/provider throttling 分开报告。
签认后的工作表与[部署 runbook](deploy.md)绑定同一配置摘要，并记录函数内存、超时、
并发及版本。函数内存、超时或并发配置变化后，应重算受影响的需求行；
在新 `L_i`、并发峰值及实际账号额度填齐前，不能沿用旧签认结果放行负载。
成本与预算同时按[设计第9.3节](design.md#93-两区月度情景与预算提案)由预算负责人复核；
应以目标配置、实际用量与当前单价确定预算基数。
