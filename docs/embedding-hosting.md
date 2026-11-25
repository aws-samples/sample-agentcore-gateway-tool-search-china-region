# Embedding 托管选择与迁移边界

本文比较当前模型代理与其他 embedding 托管选择，供架构师评估迁移条件。Embedding 是把查询或工具描述转换为向量的模型计算，发生在在线搜索和后台索引增强时。本文服务于[项目总览](overview.md)中的延迟与数据边界目标，读完后可明确迁移需要验证什么、增加哪些运维责任。文中的早期样本和公开报价只保留其记录时点，不作为当前采购或性能承诺。

核对日期：2026-10-12。价格、实例供给和服务条款均须在采购或迁移前重新核对。

## 建议与性能依据

按数据处理要求、实测延迟、吞吐、配额与运维能力选择模型服务。先用[错误诊断](embedding-diagnostics.md)区分连接、响应、限流及本地预算问题，再决定是否迁移。

[Search 总预算](../src/semantic_service/search.py) 从请求处理开始计时，默认 3.0 s，
包含授权和快照加载。总预算由 `SEMSEARCH_SEARCH_TIMEOUT_SECONDS` 配置，
允许 `(0.3, 6]` 内有限秒数；`MODEL_EMBEDDING_TIMEOUT_SECONDS` 同时控制核心子预算
和 HTTP 上限，默认 1.0 s，允许 `(0, 6]` 内有限秒数。仅在 Lambda 剩余期限分支
扣除 0.3 s 收尾余量：

```text
provider_deadline = min(started + search_timeout, started + Lambda_remaining - 0.3)
embedding_deadline = min(provider_deadline, embedding_started + embedding_timeout)
```

代码默认与部署配置分别核对；固定配置可以覆盖默认值。
固定 S3 显式旧值的覆盖规则见[预算配置](embedding-diagnostics.md#可配置预算与授权收尾)。

缺少或无法解析 Lambda 剩余时间时沿用配置期限；诊断标志和预算起点见
[embedding 诊断](embedding-diagnostics.md#可配置预算与授权收尾)。
[核心检索](../src/semantic_core/search.py) 的有效子预算还会被前置工作消耗，
首次模型 secret 获取也在子阶段内，不能把配置值当成实得 HTTP 额度。
[HTTP client](../src/semantic_service/runtime.py) 已缓存复用。换到 EC2 不会消除这些本地
预算及初始化成本，也不能直接保证冷 p95 4 s、热 p95 1.5 s。

迁移评测应分别记录连接/TLS、等待响应、响应读取/解析、推理排队和实际计算时间，使用
同一查询集测试新连接、复用连接、空闲后调用和并发负载。这些测量应在你的模型服务与网络条件下完成。

## 限流确认后的选择

按模型服务的文档和账号配置核对 RPM、TPM、并发与突发限制，明确配额是否按账号、模型、项目/租户或凭证共享。更换凭证不一定增加配额；公开限额也不能证明代理的实际限额。

诊断字段能区分收到 HTTP 429 与连接/读取超时；单凭状态码不能确定具体额度、预算或计费原因，也不能判断响应由代理还是上游生成。

| 确认原因后的措施 | 收益与代价 |
| --- | --- |
| 申请提升实际配额 | 无需在 Search 内等待，但需代理/账号所有者申请并确认生效。不能解决欠费、平台过载或本地期限不足 |
| 调用侧限速与有界退避 | 平滑请求、token 和并发突发；须覆盖共享额度的调用方，单容器限速不能保证总量。退避消耗在线延迟预算，应优先让后台任务延期，Search 保留及时 BM25 降级 |
| 错开或约束 Enhancer | 只有它实际调用同模型且与 Search 争用时才有效，代价是增强更新变慢；不能控制其它租户。不能仅凭时间重叠推断故障归因 |
| 合并文档 embedding | 批大小必须符合模型服务上限；减少 RPM，不减少总 token。大批次可能增加单次延迟和瞬时 token，用于后台比在线凑批更合适 |
| 缓存成功 query 向量 | 重复查询命中时减少请求及 token；需有界容量/TTL、完整模型与前处理版本、适当安全命名空间。首次或新查询不受益；保留全部授权检查，不缓存错误或记录原始 query |

模型服务的 token 上限不替代本服务更严格的字节限制。异步批处理若由模型服务支持，仍需额外的状态管理和结果发布流程，不适用于在线 Search，也不能假定代理已支持。

若最终确认为可提高的额度或可平滑的突发，优先验证这些远程调用措施，再评估 EC2。
只有持续容量确实无法满足或数据必须留在 AWS 等独立条件成立时，才推进私网试验。
自托管仍需限速、排队和容量规划，且不能消除现有本地预算，也不能直接复用另一模型的向量。

## 三档选择

| 方案 | 适用条件与实施方式 | 必须验证的结果 |
| --- | --- | --- |
| 配置远程模型服务 | 第三方处理符合数据要求；先补错误类型、确认代理路由与计费，再验证有界预算和降级策略 | 能区分 HTTP 状态、连接/读取超时与解析失败；修复后重新测 Gateway 冷/热 p95 和向量可用率 |
| EC2 单节点试验 | 已有数据留在 AWS 的要求，或远程链路经诊断仍不满足目标；建独立私网测试服务，先不切共享 Gateway | 固定模型版本、输入长度和 batch 后，比较端到端延迟、排队、吞吐、显存与检索质量；接受单节点中断 |
| 跨 AZ 双节点生产 | 单节点试验通过且有明确容量、可用性和运维负责人 | 两个 AZ 各有已加载模型的节点；内部负载均衡、健康检查、滚动更新；单节点故障时仍能承担约定峰值 |

三档都保留 BM25 兜底。生产节点应提前加载模型并完成就绪检查，不能让 Search 请求等待
下载模型权重。双节点不等于容量足够：故障转移、模型加载、GPU OOM、突发排队和版本更新
都要单独验证。持续维护还包括驱动和镜像升级、模型版本回滚、容量补充、证书/凭证轮换及告警响应。

## GPU 与成本

部署前查询目标账号与区域的实例供给和配额，实例规格可用不保证即时容量。
候选可先比较单 GPU 的 G4dn/G5，不因 embedding 输出为 1024 维就推断所需显存：
模型权重、计算精度、输入长度、batch 和并发才是容量评测的输入。

| 实例 | GPU 与官方显存规格 | 元/小时 | 单节点 730 小时/月 | 双 AZ 各一节点/月 |
| --- | --- | ---: | ---: | ---: |
| `g4dn.xlarge` | 1 × NVIDIA T4，16 GiB | 5.223 | 3,812.79 | 7,625.58 |
| `g5.xlarge` | 1 × NVIDIA A10G，22 GiB | 9.51389 | 6,945.14 | 13,890.28 |

口径为北京区 Linux 普通按需、人民币、税前实例费；月费按未舍入小时价乘 730，
双节点再乘 2 后保留两位小数。来源为
[AWS 中国 Linux 价格](https://www.amazonaws.cn/en/ec2/pricing/ec2-linux-pricing/)、
[税费说明](https://www.amazonaws.cn/en/ec2/faqs/)和
[官方加速计算规格](https://docs.amazonaws.cn/en_us/ec2/latest/instancetypes/ac.html)。
显存容量以公开实例规格与目标区域 API 为准；部署前核对所选实例的技术规格，
营销页的标称 24 GB 不代表可用 24 GiB。

表中未包含 EBS、模型制品存储、ALB、NAT/endpoint、公网地址、网络流量、日志监控、税费与
人工运维，也未使用预留、节省计划或 Spot 折扣。G4dn 可作为成本较低的试验候选；G5
提供更多显存，但是否缩短本项目延迟必须实测，不能由型号推导吞吐或 SLA。

远程调用费用按模型服务的公开价目及实际合同计算。应比较完整计费周期的查询与文档 token、峰值并发、重试和代理费用；38 次 Search 样本不足以推导 GPU 利用率或成本盈亏点。

## 数据离开 AWS 的要求

任何兼容 OpenAI 接口的模型服务均可配置，含自建服务。若模型服务位于 AWS 之外，查询及 Enhancer 发送的工具描述、批准示例也会交由该服务处理。即使传输加密，也应确认代理经营方、上游服务、日志留存、访问权限及删除安排。

是否用于训练、数据保留多久、存储与推理发生在哪里，应分别按模型服务的条款核实。不能仅由代理地址或模型名推断数据驻留。现有授权控制约束允许发送哪些数据，不能代替组织对第三方处理的批准。

若要求 embedding 输入只在 AWS 内处理，优先评估私网 EC2；如果要求全部模型数据都留在
AWS，还须处理仍调用远程服务的 chat/rerank。私有化也应关闭原始请求日志，并确认遥测、
模型下载和运维访问不会把业务数据发往外部。具体数据分类、留存和第三方处理要求由数据
所有者确认，本文不替代合规判断。

## 接入与迁移清单

[当前 Lambda IaC](../infra/functions.py)也未设置 VPC。EC2 私有化需要独立后续变更：

- **网络与 IaC：** 增加或引用明确的 VPC、私网子网、安全组、DNS/TLS、EC2 启动配置、
  固定镜像与模型制品。修改 [部署配置](../infra/config.py)和[栈组装](../infra/stack.py)，
  为 Search/Enhancer 接入 VPC 并补所需网络接口权限；Request/Indexer 无需为 embedding
  一并迁入。保留 S3、DynamoDB、Secrets Manager 的访问路径；仍需远程 chat 时提供受控出口。
  [Lambda VPC 文档](https://docs.aws.amazon.com/lambda/latest/dg/configuration-vpc.html)
  说明私网接入与权限要求；附加到公网子网本身不提供公网访问。
- **推理入口与认证：** 使用精确 hostname allowlist、HTTPS 和已批准的凭证。现有
  [HTTP 传输](../src/semantic_core/_http.py)禁止重定向并要求 HTTPS，普通 HTTP 推理端口
  不能直接接入。可信证书可沿用客户端；私有 CA 需要显式信任配置，不能关闭证书校验。
  双 AZ 生产增加内部 ALB 与模型就绪检查；[ALB 文档](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/application-load-balancers.html)
  要求至少两个不同 AZ 的子网。
- **分离模型目的地：** [ModelSettings](../src/semantic_service/models.py)目前让 embedding、
  chat、rerank 共用 base URL、allowlist 和 secret。直接把 `MODEL_BASE_URL` 改成仅提供
  embedding 的服务，会把 Enhancer chat 也指过去。后续应分离 embedding transport/凭证，
  并同步配置允许清单、运行时组装与精确 IAM；不能只替换 URL。
- **依赖与运维：** [vLLM profile](../examples/provider_profiles.py)只是使用 synthetic
  模型、三维向量和 MockTransport 的离线示例。真实推理服务需要独立锁定镜像、vLLM、
  框架、GPU runtime、模型/tokenizer revision 与许可证，验证目标硬件兼容性；不要把这些
  依赖加入现有 Lambda 包。扩展[监控](../infra/monitoring.py)记录就绪节点、排队/推理耗时、
  OOM/重启与服务错误，不记录输入正文或密钥。
- **模型与快照：** 不假定远程托管模型可原样导出到自建推理服务。选定可部署模型后，验证中英文
  检索质量、查询/文档前处理、batch、长度、响应格式和维度。完整 ModelProfile 必须与快照
  匹配；[Enhancer](../src/semantic_service/enhancer.py)按 profile 绑定缓存并生成新向量。
  即使维度相同，不同模型也不能复用旧向量。发布与回滚须成对选择模型配置及匹配快照；
  不同模型之间不能直接做 query embedding 故障切换，BM25 仍是安全兜底。

IaC、Lambda/VPC/IAM、secret、依赖和模型快照的变更均需按你的部署流程审批与验证。
上述建议不代替购买、部署或 Gateway 配置切换的审批。
