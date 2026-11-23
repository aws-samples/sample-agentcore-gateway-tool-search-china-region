# ECS Search 设计

启用可选 `ecs_search` 后，Search 使用常驻 ECS 处理主路，独立 Lambda 按规定条件回退；默认关闭 ECS 时仍直接使用 Search Lambda。公开工具与 REQUEST 拦截器契约不变。

## 入口选择

Gateway 的现有 Lambda target 改指向 facade 的固定 alias。保留真实 gateway ID、target ID、incarnation、工具名称、完整 descriptor 与 schema 摘要，注册表和安全目录必须一致。facade 只从 Gateway 可信 ClientContext 获取 MCP ID，拒绝 arguments 或新增客户端头指定身份/后端。ECS 与 Lambda 以同一真实身份重新验签、读取现行策略、检索并在返回前复核。部署先新增 facade 和独立 fallback，保留旧 Search 的版本、代码包、alias 和 Gateway 调用权限。验收后按有损操作安排维护窗口切换 target；后端先就绪只能缩小风险，不能承诺 Gateway 更新期间无错误。

| 方案 | 选择依据 |
| --- | --- |
| Lambda facade → ECS，失败 → Lambda | 采用；原 ID 和公开契约可保留；回退控制位于 ECS 外 |
| Gateway HTTPS MCP target → ECS | 北京支持，但原型实测下游 ID 改写，会破坏现有封套绑定 |
| Gateway OpenAPI target → ECS | 原型需要额外客户端 ID 头，涉及契约变化；直接入口也不能独立处理 ECS 全故障 |
| REQUEST 按热度分流 | 增加状态与路由复杂度，未实施 |

内部传输是新增的私有实现接口，不改变 MCP 工具 schema 或成功结果契约。正常、被接纳且未超时的检索复用同一核心；新增准入故障意味着全部错误行为并不与旧 Lambda 相同。成功的 BM25 降级仍是成功，不触发回退。连接失败、HTTP 协议断连、传输超时、明确的容量不足、后端 deadline 和 replay 存储不可用可回退一次；IdP DNS/连接/超时等可证明传输故障也可回退，日志为oauth_transport_failed；首次401后因facade自身预算不足而尚未再次向IdP发请求的刷新失败为oauth_refresh_unavailable，可回退；已完成第二次明确拒绝仍硬失败。Secrets Manager限流/429/5xx单列oauth_secret_unavailable并可回退，AccessDenied等明确权限拒绝不回退。明确400/401/403/invalid_client、TLS验证、授权、重放拒绝、畸形响应、控制读取和已知业务错误直接失败；IdP 5xx单列oauth_server_failed，保守不回退，未知原因保持oauth_failed。备用函数无递归路由。

## 网络与安全

已实现方案将 ECS task 放在两 AZ 私有子网，关闭 task 公网 IPv4；facade 和 task 的公网 IdP/AWS API/模型 HTTPS 出口经同 AZ NAT。任务 8443（Search）与 9443（健康检查）只允许 NLB 安全组；internal NLB 443 只允许 facade 安全组，仅转发至 8443。出站仍允许任意地址 HTTPS，私有子网不阻止被攻陷工作负载经 NAT 外传数据。签名 HMAC 的读取者也具备签发能力；应用验签不能代替工作负载和控制面完整性。

facade 校验 HTTPS 证书链和固定域名，禁用环境代理、重定向和隐式重试。NLB 到任务再次使用 TLS，容器使用临时自签证书；NLB 不验证目标证书，不能据此证明后端身份。OAuth 认证调用方，REQUEST 签名封套认证请求，真实 Search 再执行实时授权；它们不能阻止被注册为目标的恶意服务返回伪造响应。部署控制面、目标注册权限及批准镜像是信任条件，应用角色不得获得这些权限。来源 SG 约束网络路径，不等价于响应签名或 mTLS。

facade 只读取专用 OAuth 客户端 secret，并只能 Invoke 固定备用 alias；不能读取检索数据、模型或签名密钥。ECS 只获得现有 Search 所需的只读数据、签名和按开关开放的模型 secret 权限，以及独立重放表的 PutItem 权限（按后端前缀限定）。REQUEST 权限和包保持原样。任务 execution role 与应用 task role 分开；镜像只引用不可变 digest。

OAuth 使用真实服务 IdP 的 client_credentials，客户端凭据由现有 SecretsCache/OAuthTokenSource 有界缓存、轮换刷新；ECS 使用现有 OIDCVerifier 检查短期 JWT，并限制唯一服务主体和 scope，不接受用户令牌替代服务通道。部署时先配置新客户端可用，再切 AWSCURRENT，保留旧端短期重叠，观察刷新成功后撤销旧凭据。真实 IdP、客户端和 secret 均须另行批准，不能默认使用测试 IdP。

封套自身带随机 nonce。后端验签成功后，按规范化封套摘要条件写入独立 DynamoDB 重放表；ECS 和 fallback 各有自己的前缀，允许同一次读取请求在超时不确定时回退一次，禁止同一后端再执行。当前后端写入失败时不执行 Search；ECS 的存储不可用可尝试独立 fallback，但 fallback 必须成功完成自己的条件写。两端都无法写入时均拒绝执行。IAM 能限制动作和 LeadingKeys，不能强制请求携带 ConditionExpression；条件写正确性由应用实现及回归守护。被攻陷的任务可覆写自身前缀内记录，这是保留的威胁模型限制。TTL 只回收空间，过期判断必须在条件写中完成；保留期长于封套最大有效时间及允许时钟偏差。OAuth bearer 不声明防窃取后首次使用，仅阻止捕获请求的重复执行。

## 服务与预算

最少两个任务，每任务 2 vCPU / 4 GiB、4 个预热 Search 工作进程及 4 个认证进程。每个进程独立创建 SDK/HTTP 客户端，不跨 fork 共享连接；认证进程不预载检索快照。完整读取有大小和墙钟上限的正文后，先完成 OAuth 与封套认证，随后才申请 Search 工作许可。业务连接上限 128，请求行和头合计最多 16 KiB、64 行，头部与正文各有 1 秒墙钟上限；健康检查使用独立 TLS 9443 监听及 8 个连接额度；业务端口不提供健康路径。复用连接最多100个请求，60秒软寿命后不再接纳新请求；已接纳请求保留原6秒期限，最长约66秒后释放，避免寿命计时切断在途成功响应。认证与检索均运行于可终止的独立进程，超过绝对期限后终止、回收并重建相应 worker，不把“关闭 socket”当作工作取消。

认证池、Search 池共享 60 秒总启动期限；容器健康 start period 为 120 秒，服务 grace 为 210 秒以容纳 NLB 检查。Search worker 预载索引后才可接纳工作；单 worker 串行请求，刷新期间不会让其他 worker 等待同一快照锁，但各进程仍可能同时下载同一新快照。缓存内存、启动和刷新流量按 4 份计算，实际 RSS 与吞吐必须在 dev 验收。短时满槽产生受控容量错误，不能直接触发 readiness 失败；每个池至少一个可用或正在工作的健康 worker 即具备部分服务能力；所有槽忙不等于未就绪。独立 supervisor 检测闲置死亡并重建，单次回收或日志异常不能杀死监督线程；readiness同时检查supervisor存活，退避上限 5 秒且带抖动，单次初始化失败不永久污染 readiness。创建/回收进程不持全池锁；零容量期间返回未就绪但仍自动修复；若OS回收未确认则保守不就绪且该槽仍占容量，避免未知工作与无限补员并存。监督循环每池50ms一次，每task两池约40次唤醒/秒，开销待现场profile，不改成延迟更大的探测间隔。发送负载前已过期的请求直接拒绝，不终止未接负载的健康 worker。`/health` 与 `/ready` 均报告这一状态。启动前即安装SIGTERM处理，启动中止也回收已创建进程。SIGTERM 停止接入，最多排空 10 秒，随后回收工作进程，ECS 停止宽限 30 秒。应用容器非 root、drop ALL capabilities、只读根；root 初始化容器共享任务角色，仅执行固定临时卷 chmod/chown，去掉除 CHOWN 外的默认 capabilities，不设置 Fargate 不支持的 Add=CHOWN。应用依赖 SUCCESS 并有启动等待上限。

CDK 保持滚动健康下限 100%、上限 200%、熔断回滚、2–4 任务自动伸缩。CPU 60% 是补充信号；每任务工作槽利用率以活跃健康槽为分母，部分容量损失也能报告饱和，定期输出空闲零值，以服务维度平均值追踪 60%，覆盖低 CPU 的 I/O 等待和词法计算饱和。滚动更新可短时达到 8 个任务、16 vCPU。扩容存在指标发布和任务预热延迟，秒级突发主要依靠现有容量与回退，不能用伸缩策略证明首批低延迟。新建服务显式 DesiredCount=2；更新必须通过离线 overlay guard，从完整既有部署模板构造候选并对已存在服务移除 DesiredCount，避免回缩自动扩容结果。直接绕过 guard 使用原始 CDK 模板更新仍有回缩风险，见[运维步骤](ecs-search-operations.md)。

embedding 1.0 秒、provider 总预算 3.0 秒仍是配置上限；OAuth、验签、replay、控制读取等开销会压缩 Search 入场余额，不能称有效预算或降级率与 Lambda 相同。facade、ECS、进程执行及 fallback 统一消费 [route_budget.py](../src/semantic_service/route_budget.py)：总路由 14 秒，ECS 单次最多 4 秒，显式给冷 fallback 留 8 秒，fallback 单次最多 10 秒；函数 timeout 分别为 facade 15 秒、fallback 11 秒。真实 Lambda 余额另扣 150 ms 后限制路由；ECS 授额是 `min(4s, 路由余额−8s)`，不足最低 0.5 秒时跳过 ECS。服务端从该传输听窗内扣除共享的0.5秒结果交付预留，所以完整4秒窗口最多提供约3.5秒认证/执行时间；认证消耗也在其中。execute派发前再次要求至少0.5秒余量，不把近到期工作送入池。已完成结果可在执行截止后交付，但不额外延长facade听窗；Lambda回退不扣这项ECS预留。跨TLS与真实spawn的红绿测试证明执行截止后0.2秒交付不触发回退；任意网络断开、超过有限交付窗或平台退出仍是未知交付，不能据此承诺exactly-once。已有余额不足 8 秒时回退仅为 best effort，日志记录不足，不宣称具备完整冷预算。

8 秒是工程初值，不是冷 fallback 的实测 p99。应在目标环境验证新冷 fallback、suppressed init、最大配置，以及封套包装、SDK Invoke 和回传所需余量。router 构造移到独立 `facade_lambda.handler` 的模块 Init；fallback 导入公共 facade 类型不会触发该构造。Init 构造 SDK/HTTP 客户端但不读取应用 secret、不换取 token；OAuth 获取在有时限的 ECS 尝试内部。Init失败记录有界异常类型、不记录消息；独立Init日志不伪造route finish。route中的factory_ms_scope=invocation，eager模式factory_ms=0只表示此次Invoke没有构造，不代表Init没有成本。阶段计时沿用 `stage_timings` 的 count/duration_ms/max_ms/errors 及 inclusive 口径，不可将父子耗时相加；OAuth secret 与 token 获取单独计时。

普通 Lambda Init、首次创建网络、suppressed init 和 Gateway/REQUEST 时间不属于同一个应用路由计时。on-demand Init 初始限 10 秒，初始化重试可进入首次 Invoke；不能以“前移 Init”宣称冷请求一定减少总耗时。厂商行为依据：[Lambda 执行环境生命周期](https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtime-environment.html)。

已完成的真实 Search 结果不因完成后的墙钟或单调时钟观测被丢弃；单调硬截止只约束尚未完成的执行。工作进程以独立完成标志通知监督端，结果交付另有有限宽限；完成后无法交付记内部交付失败，不伪造deadline后重算。facade/平台/网络有独立期限，此规则不能保证结果一定抵达客户端。清理本机 worker 或 SDK 连接不能撤回已发送的远端模型请求、Lambda 调用或费用。fallback 不设置 PC，任何一次路由最多两次只读 Search，保留双方尝试与失败，不去重掩盖重复模型调用。

## 可观测性与验收

保留现有 Search 逐请求 embedding_timing；旧 Lambda 的 41 类指标继续只写原 namespace，ECS 与 fallback 各仅发布search-count、search-internal-errors、search-degraded、vector-failed到`原namespace/ecs`、`原namespace/fallback`，由对应告警消费；其余37类用Logs Insights查询。旧告警继续只观察旧Lambda的41类Search指标，base namespace中其他ECS/route/slot指标仍保留。此前已混入的历史点无法追溯拆分。新增 route、attempt、真实 backend、回退原因和阶段耗时，关联标识只用散列，不记录 query、token、封套或 secret。告警覆盖不健康目标、ECS CPU/内存/任务数/工作槽、NLB 新建与活动连接数、facade 与 ECS 延迟、错误/回退、TLS/OAuth/重放失败；无流量时不把缺失请求指标当作健康成功。

正式接流前必须回读真实 target、descriptor/schema 摘要、alias、配置版本、任务镜像 digest 与任务角色；错误身份、篡改 ID/query、重复封套、撤销权限、轮换、ECS 全停和恢复都要做 dev 验证。测量计划单列闲置首批、冷并发、突发与热请求，解释历史 6 秒长尾，并逐请求核对 embedding 实得额度。

## 私网入口实施与迁移（方案 A）

可选 ECS 使用内网入口。保留 Gateway Lambda target 和 REQUEST，仅把 facade 的 ECS 出口接入同一 VPC；公网 IdP 仍通过受控出口访问。方案 B 的进程回收、准入期限和独立健康端口继续保留，因为私网来源和下游故障仍可耗尽容量。

| 改动位置 | 实现与后续部署动作 |
| --- | --- |
| CDK 网络 | 在现有两 AZ 各增私有子网及独立路由表；每 AZ 一个 NAT gateway 放在公网子网并绑定一个 EIP。NLB 定义改 internal，选择私有子网；如果目标环境已有公网 NLB，先审查替换计划并保留可回滚的旧 Lambda 路径；NLB SG 入站 443 只允许 facade SG。任务 8443/9443 仍仅允许对应 NLB SG |
| facade 配置 | 给 facade 指定私有子网和专用 SG，出口至 internal NLB 443、经 NAT 至 IdP/Secrets/Lambda API 等 HTTPS 依赖；Lambda 处于公网子网本身不会获得公网地址或互联网出口 |
| IAM | 仅为 facade 加 Lambda 管理 Hyperplane ENI 所需动作；按官方建议用 lambda:SourceFunctionArn 的显式 Deny 禁止函数代码自行调用 ENI API，保留服务创建/回收能力；其余 secret/Invoke 仍精确到既有引用 |
| TLS 与 DNS | internal NLB 使用已批准证书及 SAN 匹配的独立内部主机名，VPC DNS 解析至新入口；不能直接把原公网主机名改成私有地址后让旧无 VPC facade 继续访问。证书/DNS 变更单独批准 |
| 逐步切换 | 先发布网络与 facade 的新版本/验证 alias，等待函数 Active、两 AZ targets 健康；验证新版本访问私网服务、公网 IdP、Secrets 和 fallback Invoke。最后移动 facade live alias 或原 Gateway target 的已批准引用；保留旧版本和入口用于回滚 |
| 清理 | 观察、回读、回滚演练完成后才申请删除旧公网 NLB/公网 DNS；不连带删除日志或 replay。新旧并行期间两套 NLB 的费用另计 |

新环境部署前须验证 ECR API/DKR/layer、Logs、Secrets、DynamoDB、S3、IdP/模型 HTTPS 出口。S3/DynamoDB gateway endpoints无固定端点费用。S3最小策略包含应用配置/索引对象读取及区域ECR layer桶GetObject；DynamoDB端点不重复添加LeadingKeys/Null，只保留原表/Action/精确task-role主体；细粒度读取及replay前缀PutItem限制继续由角色原策略承担。ECS显式依赖两条私有默认路由和两个端点；不增加interface endpoint。改变已有 S3 流量的子网路由可能中断连接，需按部署窗口验证。端点复制逻辑要求每条权限语句的全部Action属于单一服务；未来混合S3/DynamoDB语句必须先拆分，否则两端点都会省略它并拒绝访问。fallback当前无VPC配置，S3/DDB调用不经过只允许task role的端点；未来迁入该私网须另行审核端点主体及两服务路由，不能直接复用当前策略。维护与首次拉取验收见[部署验证步骤](ecs-search-operations.md#部署顺序与证据)。

成本需按目标区域、任务数、活跃小时和实际流量计算，包括 Fargate、NLB/NLCU、NAT、
公网 IPv4、监控、日志、模型、请求、传输以及扩容和滚动重叠。私网流量经过 NAT
可能增加费用；S3/DynamoDB gateway endpoints 没有固定端点费，不代表请求免费。
当前价格见 [VPC/NAT/IPv4](https://www.amazonaws.cn/en/vpc/pricing/) 和
[CloudWatch](https://www.amazonaws.cn/en/cloudwatch/pricing/)；监控数量以当前模板为准。
REQUEST 预置并发应另外计价，样例不提供费用上限承诺。

启用前验证 Gateway 全链路预算、冷回退、supervisor 故障、私有出口、指标来源、通知和容量；已知问题见[已知限制](known-limitations.md)。

部署预算在所有生产消费者导入时校验：时钟容差不超过1秒、MIN_ATTEMPT严格小于ECS授额、ECS交付预留与最低执行窗之和严格小于ECS上限、fallback函数期限短于facade函数期限且覆盖fallback执行预算。不通过校验的候选在导入/synth前失败。

DynamoDB 端点策略限定表、Action 和精确 task-role principal；角色策略继续限制键前缀。验证时同时检查角色的全部有效权限和端点策略，以正确及错误表、动作、后端前缀分别测试，不能只看一条 Allow。
