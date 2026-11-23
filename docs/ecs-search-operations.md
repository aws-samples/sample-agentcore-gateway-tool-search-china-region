# ECS Search 部署与运维

`ecs_search` 是默认关闭的部署选项；启用后 Search 主路由 ECS 执行，按规定条件由独立 Lambda 回退，入口保持同一 Gateway Lambda target。[设计文档](ecs-search-design.md)说明边界，[已知限制](known-limitations.md)列出启用前必须核对的边界。

## 部署输入与固定容量

在已有公开配置中显式提供 `ecs_search` 的镜像 digest、与证书 SAN 匹配的 HTTPS 主机名、同账号/区的既有 ACM certificate ARN、两个不同 AZ、专用 OAuth secret 的完整引用、service subject 与 scope。保留已有真实 Gateway、target、incarnation、工具完整名称、descriptor、schema摘要及注册表，不沿用原型测试身份。环境配置只能引用凭证，不能携带 secret 明文或私钥。

启用 ECS 时必须显式传入原 `asset` 和独立 CDK `ecs_asset`，相同解析路径会在合成前拒绝；仅供新增facade/fallback函数使用；`asset`和可选`request_asset`继续指向部署前已核验的原包。不能为了方便把两个上下文参数都指向新包后，声称原四函数资源未变。新增fallback使用独立函数/alias，旧Search保持可调用，最后才切Gateway target；容器镜像与新ECS Lambda包必须来自同一提交和依赖锁。部署变更集仍须比较实际Code引用和版本，参数分开本身不证明原资源不变。

默认最小/期望为 **2 个任务**，每任务 **2 vCPU / 4 GiB**、4 个预热 Search 工作进程和 4 个不加载快照的认证进程；自动伸缩最大 4，工作槽利用率与 CPU target tracking 都以 60% 为目标。认证池及Search池共享60秒总启动期限，容器 start period 120 秒、服务健康 grace 210 秒；首次构造仍等待各池全部 worker 预热，超时则 task 启动失败；运行期间至少一个健康 worker 即保留部分容量，零容量时 supervisor 仍独立重建，单次失败按带抖动的最多 5 秒退避重试。各 Search 工作进程预载授权配置和索引后才接纳请求；awslogs 使用 4 MiB non-blocking buffer，日志出口堵塞不阻塞检索，丢记录必须计入测量完整性失败。过载时保留有界并发和受控错误，不能用无限线程/队列代替扩容。滚动更新健康下限100%、上限200%，启用部署熔断与回滚。期望数最大4时，滚动更新可短时运行最多8个任务，费用应计入这一峰值。ECS停止宽限与NLB排空窗口为30秒，应用自身排空默认10秒；停止后不得继续接收新Search。记录构造/输出失败均不得杀死监督或指标线程；不能仅以没有错误日志判健康。每连接100请求/60秒软寿命，已接纳请求最多继续原6秒，客户端继续复用连接并按Connection: close正常重连。不能用连接频繁重建掩盖闲置后的首批测量。

已实现方案把两AZ的task放在私有子网、禁用公网IPv4，复用NAT；新增S3/DynamoDB gateway endpoint仅关联两条私有路由表，服务显式等待默认路由及端点。ECR API/DKR、Logs、Secrets、IdP/模型仍经NAT，S3镜像层/应用读取与DDB走端点。出站允许任意地址 HTTPS，不是网络目的域名允许清单；NAT 不阻止持有 HMAC/数据读取权限的受损工作负载外传或签发请求。task 没有公网来源入站规则，8443/9443 仅允许 NLB 安全组。internal NLB 443 只允许私有子网 facade SG，健康探针使用独立 9443；NLB 使用既有 ACM 证书。facade URL必须使用匹配证书的主机名，不能把NLB生成域名直接当作该证书的匹配名。DNS记录纳入单独获批的部署清单，完成前不切target。


## 本地构建与合成

先运行 README 的完整构建与测试。容器使用同一份 hash 锁定的 x86_64 Lambda ZIP，
没有另装服务框架或重建依赖锁。[Dockerfile](../containers/search/Dockerfile) 固定
Lambda Python 3.12（Amazon Linux）镜像系列；当前固定该 tag
在 2026-11-10 的官方 Linux amd64 manifest digest，
不再接受 `PYTHON_IMAGE` 构建参数；更换基础镜像须单独获得批准并完成运行验证。
仅把 Dockerfile 和 ZIP 复制到独立构建上下文，
不要把整个工作树、凭证目录或本地配置提交给 builder。

```sh
finch build --network none --platform linux/amd64 \
  --tag search-production-local:validation .artifacts/container-context
```

`--network none` 限制镜像构建步骤，基础镜像元数据解析仍可能访问公开 registry。
本地验证使用空 Docker 配置。当前 Dockerfile 固定镜像为
`public.ecr.aws/lambda/python:3.12@sha256:8366d766893e53d1d7ba4f9d2fd9adff47f68093a762c0bba3c54035e6ffb569`。
本地构建与 smoke 不替代 ECS 部署或现场验收。

Dockerfile 与 ECS 使用同一个 `semantic_service.ecs_health` 探针，间隔 30 秒、
超时 5 秒、重试 3 次、启动宽限 120 秒。探针仅向固定 `127.0.0.1` 发出无凭证
readiness 请求，因进程临时自签证书而显式跳过证书验证；此例外不适用于外部 HTTPS。
Search 与 9443 健康端口均固定监听全部任务接口；动态任务 ENI 没有可预先固定的地址。
入站依赖私有子网和只允许 NLB 的安全组。两个监听边界保持一致，避免回环业务监听
无法接收 NLB 流量、健康探针却仍成功；不提供单独缩窄业务地址的环境开关。

启用 ECS 时，明确区分旧四函数资产和新 facade/fallback 资产。以下变量均指向已审核的本地
非秘密输入；保留原配置版本及原 ZIP，不能重新构建旧包后假定 hash 相同：

```sh
.venv/bin/python scripts/synth.py --region cn-north-1 \
  --config "$SEMSEARCH_DEPLOY_CONFIG" \
  --asset "$SEMSEARCH_EXISTING_SHARED_ZIP" \
  --request-asset "$SEMSEARCH_EXISTING_REQUEST_ZIP" \
  --ecs-asset .artifacts/lambda.zip \
  --output .artifacts/cdk-ecs-cn-north-1
```

仅在原配置 `request_package=request` 时使用 `--request-asset`；原来使用 shared 则省略。
`ecs_search` 配置必须提供显式且不同路径的 `--asset` 与 `--ecs-asset`，合成不会访问 AWS。新旧资产隔离并不自动证明
无漂移：用实际旧 assembly 核对旧 Search、REQUEST、Indexer、Enhancer、alias、PC 和固定
配置均无差异，新增 Gateway Invoke 权限是预期变化。仅首次改用facade或实际引用需变更时，获批后单独更新原 Gateway target
的 Lambda 引用为 `FacadeAliasArn`，其他 target 字段逐项保持原值；若已接入 facade 且引用未变，候选无需切换 target。

## 部署顺序与证据

下列流程用于获批后启用可选 ECS 或更新既有 ECS。每次操作都重新核对当前资源、依赖、权限与回滚范围。

本页所有同角色 Invoke 正控及正反向切流，统一遵守
[切换前权限证据的有效性](migrate-to-lambda.md#切换前权限证据的有效性)。
同一窗口内发生栈、策略、trust、身份、alias/配置、target 或相关权限变更后，
旧结论立即失效；步骤 7 增加或撤回测试权限也不例外。每次切 target 前立即重新
核对实际权限、版本和配置，并以该 Gateway 执行角色成功 Invoke 即将引用的精确 alias；
不能用操作者身份、模拟器或变更前的正控代替。

1. **安排窗口。** 记录不可变提交、ZIP hash、镜像 digest、依赖锁、配置 VersionId、
   目标账号/区、栈/Gateway、费用、停止条件与回滚操作者。在共享环境部署时先协调其他变更；本地全量验证通过、启用前置问题已解决且窗口获批后再写入。
   **反向：** 尚未写入时取消窗口并保持当前服务；不导出凭证到证据目录。
2. **只读准入并优先在隔离环境验证 IAM。** 从 fresh Original 和全部有效策略核对
   S3 ECR layer 桶、DynamoDB 表/动作/主体范围及角色键前缀条件。回读 target/注册表、
   REQUEST 版本/PC、旧 Search alias、固定配置、IAM、网络、IdP、证书、告警及操作者
   可见性、两 AZ 与 Fargate 按需峰值 16 vCPU 配额。部署前记录当前实际链路的热/冷
   延迟；尚无 facade 时其改前冷启动为不适用。优先在独立环境用获批测试主体及精确资源
   完成 IAM 正负控，再向共享栈发布同一受审策略。隔离验证缺失时记 UNKNOWN，并由
   变更负责人明确批准共享环境的受控验证，不能默认先扩大共享权限再发现负控失败。
   **反向：** 准入失败保持原 target；清理隔离测试权限与资源须遵守其批准清单。
3. **准备外部引用和 DNS 变更计划。** 由 IdP 负责人注册专用 service OAuth 客户端，
   核对 issuer/audience/subject/scope、JWT 寿命与轮换机制，仅提供 secret 引用。
   由证书/DNS 负责人核对证书、hosted zone 和记录名，保存完整原记录及原先不存在的
   状态。此时只批准 DNS 计划；新 NLB 的 alias 值尚未产生，不要求记录已存在。
   **反向：** 放弃启用且确认无共享消费者时停用/撤销本次专用客户端和凭据，保留
   secret 引用的审计信息；共享证书、zone 和凭据不删除。
4. **部署后端，取得 NLB Outputs。** 发布受审镜像和变更集，保持旧 target。
   首次服务保持两个 task 至各池预热、NLB 目标健康；核对 facade VPC Active、
   NAT/IdP/Secrets/fallback Invoke 出口、非 root/只读根/临时卷、8443/9443 仅 NLB
   来源、NLB 仅 facade SG、健康连接额度、独立角色与 replay 保护。读取部署后
   `SearchNlbDnsName`、`SearchNlbCanonicalHostedZoneId`、`SearchEcsEndpoint`、
   `SearchEcsImageDigest` 和 `SearchEcsTaskRoleArn`。`SearchEcsImageDigest` 只是配置
   字面量，不能证明运行镜像；另回读服务实际 task definition 与 running task
   的容器镜像 digest，并与受审镜像比对。健康端点须验证预载状态，不得固定返回 200；
   端口已监听但未就绪时返回 503，初始预载完成前端口尚未监听也属于未就绪。
   **反向：** 保持原 target，按本窗口底稿回滚栈；若后续已写 DNS，先按步骤 5
   恢复记录，再拆 NLB。不能从 ECS 熔断成功推导整个栈已回滚。
5. **写入 DNS 后验证 HTTPS。** 使用步骤 4 的实际 NLB DNS 名和 canonical zone ID
   写入已批准的专用 A alias，回读完整记录，再用 fresh 栈、证书、NLB/listener 和
   DNS 证据完成 `activate` 预检及主机名/TLS 验证。predeploy 的退出 3 不代表可接流。
   **反向：** 恢复原完整 DNS 记录；原先不存在则删除本次专用 alias。在任何会删除
   NLB 的回滚前完成这一步，避免悬挂记录；不删除 hosted zone。
6. **验证现场完整权限。** 回读 facade、task、fallback、Gateway 的全部有效策略。
   facade 只读专用 OAuth secret、Invoke 精确 fallback alias、写日志及供 Lambda
   服务管理 VPC ENI；函数代码受 SourceFunctionArn 条件 Deny，不自行操作 ENI。
   ECS/fallback 只读 Search 必需数据及按开关开放的 secret，写各自 replay 前缀。
   Gateway 角色为导入对象，但 `{prefix}-gateway-invoke` 是栈管理的
   `AWS::IAM::Policy`：ECS 开启时授权 Search、facade、REQUEST，关闭时授权
   Search、REQUEST，见[栈定义](../infra/stack.py)。不能手改该策略撤掉 Search。
   回读实际策略并核对漂移；若缺少模板声明的权限，单独批准修复并重新回读，
   内容相同的 CloudFormation 候选不保证修复手工漂移。以该 Gateway 执行角色
   实测所需精确 alias 的 Invoke 通过后才切 target；角色不可用则记 UNKNOWN 并停止。
   其他业务权限与 trust 保留；数据/前缀正负控也须使用真实角色，不能只检查新增 Allow。
   **反向：** 任一正控拒绝或负控放行即停止扩大负载，保持旧 target，恢复本窗口
   受审策略；不全放开端点、不削弱角色条件、不删 replay。
7. **完成准入正负控及故障验证。** 在隔离副本验证真实 ID、Unicode MCP ID、
   错误 target、篡改 query/ID、错误 service token/issuer/audience/subject/scope、
   过期 token、重放、跨后端一次回退、撤销、DDB 不可用及 ECS 不健康/恢复。
   覆盖真正冷 fallback 并记录全部失败，不能静默增加保温或 PC。模拟器无上下文、
   测试主体无权限、角色无法假设或凭证过期都记 UNKNOWN。
   **反向：** 撤回故障注入和测试权限并验证隔离环境恢复；缺项或失败时不切共享 target。
8. **仅在引用变化时切同一个 target。** 在有损维护窗口将 Lambda ARN 改为 facade
   固定 alias，保留 ID/name、descriptor/schema、credential provider 与注册表身份；
   REQUEST 不改工具名、不添加 route 参数；步骤 7 完成后、切换前立即重做步骤 6
   的实际权限回读与同角色 Invoke 核验，
   再切换并回读全部身份，验证 tools/list、Search 与授权拒绝。**反向：** 先确认
   旧 Search 版本/配置可用，按步骤 6 核实实际策略、处理漂移且同角色 Invoke 通过，
   再切回旧 ARN；target 的栈外变更须单独恢复。
9. **执行部署后功能验证并观察。** 即使未切 target 也发送唯一 MCP ID 的 canary，
   用其 UTF-8 SHA-256 关联唯一 `search_route` end，要求 `backend="ecs"`、
   `fallback_started=false`、`primary_not_started="none"`、`outcome="ok"` 且客户端
   契约/授权正确，实际 ECS Search 一次、fallback 零次。t=0/20/40/60/80/100/120 秒
   至少七条全部通过，日志五分钟内完整；不能只看前三项而忽略 `outcome=error`。
   保留部署后 canary 的全部失败分母，
   再完成批准的观察窗口。**反向：** 任一失败停止扩大负载；引用曾改变则按步骤 8
   回退，纯栈更新则按受审栈候选回退，不无故重复切 Gateway。
10. **收紧获批的其他栈外 Gateway 策略。** 观察通过且负责人批准关闭快速回滚窗口后，
    仅处理该角色上其他 inline/managed policy 中获批移除的授权与 wildcard，
    保留业务权限与 trust；
    `{prefix}-gateway-invoke` 由栈管理，不在手工收紧范围内。当前代码在 ECS 开启时
    仍授权 Search，撤掉这项栈管理授权需要另行修改并审核代码；不能把 Search 拒绝
    作为本步骤的通过条件。对实际收紧项核对全部有效策略并做正负控；仍被其他策略
    允许的撤销目标不得标记收紧完成。
    **反向：** 只恢复本步骤实际修改且获批恢复的栈外策略；切回 Search 前仍须按
    步骤 6 回读实际策略、修复已批准的漂移并实测同角色 Invoke，通过后才按步骤 8
    切 target，不能用通配授权代替精确权限。
11. **交付外部资源与回滚记录。** 记录最终 target、有效 IAM、DNS、OAuth 客户端
    生命周期负责人、新旧 alias、task definition、镜像与配置，核对 REQUEST/PC 和
    共享资源无未批准变化；保留告警、未知项、性能与费用结果。
    **反向：** 放弃新后端时先按步骤 6、10 核对和恢复所需实际权限并通过 Invoke，
    再按步骤 8 恢复旧链路并排空；随后按步骤 5 恢复 DNS、步骤 3 停用专用客户端，
    最后按批准清单清理后端。不把一次 smoke 成功当成生产 SLO 或所有回滚路径已验收。

Gateway target 切换按**有损操作**安排：提前告知调用方短暂错误风险，暂停批量/业务负载，保留旧可用引用、权限和明确回滚操作者。控制面 READY 不是数据面无损证明；切换后先执行带唯一关联 ID 的 canary，观察至少 1–2 分钟及多次连续成功，再逐步恢复负载。该观察窗不是传播时间保证，出现任何未解释错误即停止扩大负载并按批准流程回滚。

冷启动研究与业务切换分开：首次 canary 可作为冷样本，但必须从发送前开始完整记录；不能先静默预热再把后续调用标为首冷。每次获批切换为每次成功和失败保存 UTC/单调时间、HTTP 状态、原始解码 MCP 错误对象、服务错误代码/请求 ID（有则保留，无则 null）、trace、原始响应字节 SHA-256、切换时间偏移以及 REQUEST/facade 对应日志；不记录 token、envelope 或 secret。通用错误文本不能用来推断 Gateway 内部根因。进一步服务端诊断/追踪如需新增权限或采集，须独立批准。

## 离线部署预检与完整模板保护

[preflight_ecs_deployment.py](../scripts/preflight_ecs_deployment.py) 只读取本地非秘密 JSON，不读取凭证、不调用 AWS。使用[元数据采集脚本](../scripts/collect_ecs_live_evidence.py)显式采集当前 CloudFormation 信息。该脚本仅调用 DescribeStacks、GetTemplate 和 ListStackResources，不读取 secret 值。ACM、NLB 和 DNS 信息需要另外准备。

独立确认预期 Stack ARN 后按以下方式采集。输出目录必须是新目录；不要提交生成的部署元数据。采集与审核流程必须在 900 秒有效期内完成，过期重新采集。

```sh
export SEMSEARCH_EXPECTED_STACK_ARN="<your-full-stack-arn>"
export SEMSEARCH_NEW_CAPTURE_DIR=".artifacts/current-deployment"
.venv/bin/python scripts/collect_ecs_live_evidence.py --help
```

按 `--help` 中的必填参数传入账号、区域、Stack ARN 和输出目录。将输出的 `live-template.json` 与 `live-evidence.json` 分别用于 `SEMSEARCH_FULL_LIVE_TEMPLATE` 和 `SEMSEARCH_FRESH_STACK_METADATA`。

更新流程先从重新 synth 的资源中逐项提取获批 replacement 节点，保留旧四函数各自真实 Code、alias、PC、配置和未获批节点。每个现存 ECS Service 都必须显式列入 replacement，guard 会移除其 `DesiredCount`，让 CloudFormation 更新不重置当前伸缩容量；新服务仍要求显式至少 2。guard校验≤15分钟、同账号/区/StackId的完整GetTemplate/DescribeStacks/ListStackResources分页及canonical JSON SHA-256；拒绝截断/不完整/跨栈输入。保留未选节点、原顶层字段、DeletionPolicy/UpdateReplacePolicy以及既有资源的物理名；仅可通过单独output allow list新增尚不存在的Outputs，原输出即使同值也禁止覆盖，拒绝越过allow list或变更已有资源类型，拒绝部分已知的task公网地址配置；仍须逐资源确认`AssignPublicIp`为字面量`"DISABLED"`和子网真实归属，不能把intrinsic绕过当已受保护。仅有 allow list 不代表获得发布批准，变更集仍需独立审核。原始 CDK 模板保留首次创建所需 DesiredCount=2，**不得绕过 overlay 直接用于现存栈更新**。

```sh
.venv/bin/python scripts/preflight_ecs_deployment.py overlay \
  --live-template "$SEMSEARCH_FULL_LIVE_TEMPLATE" \
  --live-evidence "$SEMSEARCH_FRESH_STACK_METADATA" \
  --expected-stack-id "$SEMSEARCH_EXPECTED_STACK_ARN" \
  --replacements "$SEMSEARCH_APPROVED_REPLACEMENTS" \
  --allow-resource "$SEMSEARCH_EXISTING_SERVICE_LOGICAL_ID" \
  --output .artifacts/reviewed-update.json
```

为每个获批替换资源重复`--allow-resource`；对获批删除的每个废弃后端诊断 MetricFilter 分别显式传`--remove-metric-filter`且也列入allow list，只允许精简清单中的过滤器删除。不能用通配或把新 synth 全资源默认批准。输出只是候选文件，不上传、不创建变更集。离线JSON无法认证被人为伪造的AWS响应；授权操作者从真实API采集、独立change set审核仍是信任条件。raw CDK可绕过本地guard，所以发布规程明确禁止直用；该工具不提供云端强制策略。未来执行前核对资源计数/删除集合、真实容量、IAM、版本/镜像和变更集；不能以资源总数不下降代替删除集合审查；删除未被告警/伸缩消费的重复 MetricFilter 仍需逐项批准，不得借此删除数据资源。

开启 ECS 相对关闭开关新增八个 Outputs：`FacadeAliasArn`、`FallbackAliasArn`、
`ReplayTableName`、`SearchEcsEndpoint`、`SearchEcsImageDigest`、`SearchEcsTaskRoleArn`、
`SearchNlbCanonicalHostedZoneId`、`SearchNlbDnsName`；对应
[栈输出](../infra/stack.py)与 [ECS 输出](../infra/ecs_search.py)。
`ReplayTableName` 在 ECS 分支显式创建；`FacadeAliasArn`、`FallbackAliasArn` 由
`functions.aliases.items()` 循环中的 `f"{operation.title()}AliasArn"` 生成，
源码中没有这两个完整 Output 名的定义字面量，核对时须查看循环或实际 synth。
从同一受审 synth 逐项提取当次 live 尚不存在的获批项，保存为 JSON 对象并逐个
`--allow-output`。八项均不存在时，在上方完整 `overlay` 命令追加
`--output-additions "$SEMSEARCH_APPROVED_OUTPUT_ADDITIONS"` 及以下八个参数：

```text
--allow-output FacadeAliasArn
--allow-output FallbackAliasArn
--allow-output ReplayTableName
--allow-output SearchEcsEndpoint
--allow-output SearchEcsImageDigest
--allow-output SearchEcsTaskRoleArn
--allow-output SearchNlbCanonicalHostedZoneId
--allow-output SearchNlbDnsName
```

已有项不放入 additions。原 Outputs 全部保持，禁止覆盖/删除或任意自动导入新模板的
所有 Outputs。栈更新成功后重新获取完整 fresh 模板及查询结果，再跑 activate；
旧 capture 或本地候选均不能冒充已部署输出。下次新建/切引用前必须通过 activate。

入口预检在 NLB/listener 存在后、切 target 前执行；新建前先校验证书与 DNS 计划，创建后再提供完整证据。predeploy阶段不要求NLB已存在，只检查证书和hosted zone，返回PREDEPLOY_CHECKED、passed=true、ready=false、退出3，不能据此接流。activate阶段必须另传fresh完整栈证据与预期StackId，证明NLB确为该栈资源；输入envelope包含`captured_at/account/region`，以及 `certificate/hosted_zone/record_sets/load_balancer/listeners` 五个 `{request,response}` 对象，绑定每次查询的精确资源参数。证据不得超过 15 分钟；必须完整分页。预检核对证书 ARN、ISSUED、SAN/单层 wildcard、有效期，internal/active NLB 的 TLS 443 listener 证书，hosted zone/VPC 和精确 simple A alias 的 NLB 域名/zone ID。

```sh
.venv/bin/python scripts/preflight_ecs_deployment.py endpoint \
  --phase activate \
  --live-evidence "$SEMSEARCH_FRESH_STACK_METADATA" \
  --expected-stack-id "$SEMSEARCH_EXPECTED_STACK_ARN" \
  --config "$SEMSEARCH_DEPLOY_CONFIG" --region cn-north-1 \
  --evidence "$SEMSEARCH_FRESH_ENDPOINT_METADATA" \
  --hosted-zone-id "$SEMSEARCH_ENDPOINT_ZONE_ID" \
  --load-balancer-arn "$SEMSEARCH_INTERNAL_NLB_ARN" \
  --output .artifacts/endpoint-preflight.json
```

新建前先用`endpoint --phase predeploy`与证书/zone证据生成计划，缺少NLB不能伪造activate成功。退出码3只表示predeploy前置条件通过，只有activate的READY/ready=true才退出0并表示完整入口元数据通过，均不代表获准发布；缺失 DNS 返回 2 并输出 CREATE-only 计划，冲突拒绝，不自动 UPSERT。DNS 写入必须另有明确授权，完成后重采集验证。证书不匹配属于安全失败，不触发 Lambda 回退；必须在接流前阻断，而不是等待回退掩盖错误。

## 指标与告警

指标以实际部署模板和日志字段为准；下面是需在准入时逐项回读的运维覆盖。普通请求日志不能带query、完整MCP ID、token、envelope、secret、原始provider错误正文或高基数用户标签。请求关联使用受控hash与attempt ID，CloudWatch维度只用部署/服务/后端等固定值。

| 信号 | 运维含义与处置 |
| --- | --- |
| NLB `HealthyHostCount` / `UnHealthyHostCount` | 小于2个健康目标应调查；全部不健康是主路不可用。NLB在全部不健康时可能fail open，不把其路由行为当作安全或应用健康保证 |
| ECS `RunningTaskCount`、部署事件 | 目标最少2；确认所用namespace和是否启用对应Container Insights采集。缺数据视为需要调查，不能当作running=2 |
| ECS CPU/内存与在途请求 | 工作槽周期均值目标 60% 覆盖 I/O 饱和，CPU 60% 补充；高内存应核对 4 个进程的独立缓存、索引刷新峰值和 RSS。扩容上限4后仍过载需部署负责人容量决策 |
| NLB `NewFlowCount` / `ActiveFlowCount` | 初始阈值为每分钟新建连接 Sum>1200、活动连接 Max>400，各连续 2 点；私网也保留异常流量检测，后续以实测校准 |
| `SearchSlotUtilization`、容量拒绝和连接丢弃 | 每任务 10 秒样本含空闲 0，服务维度均值；槽位 >85% 连续 2 分钟告警。拒绝与超时回收日志分别统计，不用释放信号量掩盖旧工作残留 |
| facade/ECS 延迟 | facade Duration p95>4000ms、ECS backend p95>3000ms 连续 2 分钟；仅为初始调查阈值，不是 SLO |
| facade error、`route-fallback` | 回退可以成功但说明主路故障；同时看尝试次数、原因、ECS耗时和Lambda冷启动，不只看最终成功率 |
| `ecs-auth-failed`、TLS验证失败 | 鉴权或证书故障；不能以回退绕过。核对IdP元数据、service subject/scope、时间、secret版本及DNS |
| `ecs-replay-failed` | 区分重放被拒绝与DDB依赖不可用；两者均不运行Search，不删除replay记录“修复”错误 |
| `ecs-backend-errors`及Search错误类别 | 控制读取、索引未就绪、最终授权、内部错误分别处理；泛化503不能自动认定可回退 |
| 向量可用率、degraded、embedding错误类别/时序 | HTTP成功仍可能退化为BM25；逐请求核对1000ms配置、阶段余额与HTTP实得额度，避免预算截断被成功率掩盖 |
| deadline、fallback失败及无结果关联 | 代码常量 facade 总预算14s、ECS最多4s、显式冷fallback预留8s、fallback单次最多10s；函数timeout15s/11s，仍受真实Lambda剩余额度约束；8s需新冷样本验证，不是最坏时延保证 |

初始阈值用于发现故障，不是承诺的SLO。目标健康/运行任务数用1分钟窗口连续2点不足告警；CPU>85%或内存>85%连续2个1分钟点告警；认证/重放/后端错误以 1-of-1 捕获孤立事件；回退或 backend error 出现即检查主路，避免长时间把全部流量隐性迁到Lambda。低流量时计数阈值与比例阈值须配合；无请求的日志指标缺失不等于健康。启用 ECS 必须提供非空 SNS topic 引用，每条告警附描述与通知动作；配置校验不能证明真实送达。部署前验证实际通知订阅与送达；人工观察不能替代无人值守告警。client/target TLS 错误分别为 >0 单个1分钟点告警，覆盖私网低流量下的孤立故障。回退率使用实际启动的 fallback attempt 数 / facade finish 数；旧namespace的41类Search指标和旧告警继续只包含旧Lambda来源；ECS/fallback各四类指标写入`原namespace/ecs`与`原namespace/fallback`，对应错误率/降级/向量失败告警。base namespace其他ECS、route和slot指标仍保留，不要把整个namespace说成只含旧Lambda。比对 MetricFilter 的 LogGroup、FilterPattern、namespace/name/dimensions 后才能解释指标，不能把不同后端计数相除当作回退率。历史已混流样本无法逆向拆分，迁移时记录时间边界，避免跨边界比较。

部署后验证过滤器匹配受控日志并生成指标，核对 CloudWatch 告警与 SNS 订阅的实际送达，并验证中止阈值。仅 CDK 包含 Alarm 资源不能证明告警已生效。不得复制旧 Lambda 专用过滤器后，直接假定 ECS 日志具有相同顶层字段。

### 按需查询其余诊断

不为 ECS/fallback 复制其余 37 类 Search 指标。选择对应后端日志组和部署后时间范围运行以下 Logs Insights 查询；`@log` 保留来源，不能把两个后端合并为一个故障分母。查询费用按扫描 GB，缩小时间和日志组范围。旧 Lambda 的 41 类指标和旧告警不删改。

```text
fields @timestamp, @log, ranking_mode, configured_fusion, vector_availability, rerank_state
| filter operation = "search"
| stats count(*) as requests by @log, ranking_mode, configured_fusion,
    vector_availability, rerank_state, bin(5m)
```

```text
fields @timestamp, @log, aws_request_id, embedding_error.kind, embedding_timing
| filter operation = "search" and ispresent(embedding_error.kind)
| stats count(*) as failures by @log, embedding_error.kind, bin(5m)
```

```text
fields @timestamp, @log, aws_request_id, status, empty_result, below_threshold,
    degraded, lambda_deadline_ignored, embedding_timing
| filter operation = "search"
| filter empty_result = true or below_threshold = true or degraded = true
    or lambda_deadline_ignored = true or not ispresent(ranking_mode)
| sort @timestamp asc
| limit 10000
```

最后一条用于逐请求诊断，10,000 行截断不适合统计全部分母；完整测量须导出原始分页事件交给本地分析器。新后端错误率告警以 5 分钟 Search 内部错误/调用数 >1% 为初值；降级和向量失败各为 1 分钟 >0，低流量也可触发。`search-count` 是相应 Search 日志数，OAuth 前置拒绝没有 Search 日志，因此还要看 route/OAuth/backend 拒绝，不用这四个指标覆盖全部准入错误。

## OAuth轮换与重放

生产OAuth使用独立service客户端，服务token需通过固定issuer、audience、service subject、scope、签名和有效期校验。现有OIDCVerifier要求JWT寿命不超过300秒，时钟偏差容忍上限30秒；注册服务客户端时必须满足该限制，不能把只在token响应中缩短`expires_in`当作JWT寿命已缩短。用户的访问token不能替代facade通道身份。凭据通过SecretReference引用，进入进程内缓存；日志只记录受控类别和无敏感的版本关联。

轮换由授权操作者先配置IdP接受新凭据，随后写入新secret版本并将其提升为AWSCURRENT。旧凭据重叠窗口至少覆盖缓存及token最长有效期、时钟余量；验证新凭据换token成功、ECS验证新token成功及过期旧token拒绝，再撤销旧凭据。不得只改Secrets Manager而未更新IdP，也不得同时撤销尚在使用的JWKS签名键。401/403后的有界刷新不能重新开始整个Search预算；真正完成的第二次IdP明确拒绝硬失败。首次401后若本地刷新预算不足、第二次请求未发出，当前策略归oauth_refresh_unavailable并可回退；fallback仍验证原用户授权、签名封套和replay，不扩大用户权限。首次尝试不必然使用旧缓存；应按实际请求时序验证上述刷新与回退边界。

停用 ECS 时先切换并验证 Search、排空 facade/ECS，再由 IdP 负责人停用专用客户端、
撤销其凭据并核对新 token 获取被拒绝。已有 JWT 可能继续有效，须按实际生命周期和
验证端策略等待/验证拒绝，不把客户端停用当作即时 token 撤销。secret 引用默认保留，
删除另批。恢复 ECS 时使用新的有效凭据版本，重新验证 service 身份和 token 寿命，
不能重新启用已撤销凭据；完整顺序见[迁移外部引用处置](migrate-to-lambda.md#5-撤除专属告警并退役外部引用)。

若secret当前版本不可读、IdP不可达或签名键未知，保留脱敏失败证据，核对版本发布顺序和网络；不要临时放宽issuer/audience/scope或将secret放入环境变量。缓存、JWKS刷新和撤销的实际传播时延须通过获批dev负控验证，不能仅凭默认TTL推断所有进程已经切换。

重放表按已验证封套的规范化摘要加后端前缀条件Put，ECS与fallback分别最多执行一次。该设计允许一次不确定ECS执行后的只读回退，不提供跨后端exactly-once保证，也不防止被窃bearer与合法封套的首次抢先使用。TTL只是异步空间回收；应用必须比较有效期并保留覆盖封套生命周期的记录。DDB 不可用时当前后端 fail closed；ECS 存储不可用仅可触发独立 fallback，后者仍必须成功条件写。IAM 只限制前缀/动作，不能强制 ConditionExpression；不删除表、单条记录或缩短TTL来恢复流量。

## 后端TLS与网络边界

facade→NLB的HTTPS由客户端校验证书链与主机名，禁止跳过验证和自动跟随重定向。NLB→task也使用TLS，task启动时生成进程私有的短期自签证书；**NLB不验证target证书**，所以这段提供加密，不是mTLS或经CA验证的后端身份认证。task 的 NLB 来源 SG 约束网络路径，OAuth/用户封套验证请求；它们不能认证恶意后端的响应。保持目标注册、任务更新和镜像发布权限独立，是当前信任前提。

自签证书只在task临时目录中存在，不写镜像、日志或证据；重启生成新证书。就绪/健康检查不能读取模型secret或发模型请求。若组织要求CA验证或双向TLS，则需新的代理/证书生命周期方案和费用评审；当前任务不默默添加依赖、申请证书或扩大密钥读取权限。

NLB 入站已改 internal 且限 facade SG；本地候选 task 无公网 IP，但仍通过 NAT 访问公网，不能称整套部署“完全私网”。保留连接/头/正文墙钟时限、认证与 Search 分池、独立健康端口；监测新连接、拒绝和槽位。部署后应确认 task 确无公网地址、私有子网 route 与 NAT 正确，非 NLB 的 VPC 来源不能访问 task 8443/9443，非 facade 来源不能访问 NLB 443；必须用获批负控验证。

官方依据：[NLB target TLS行为及健康路由](https://docs.aws.amazon.com/elasticloadbalancing/latest/network/load-balancer-target-groups.html)。

## 首次部署失败后的清理与重试

NLB不开启deletion protection，CloudFormation可正常删除或替换它；8个日志组（含 VPC Flow Logs）、
3个DynamoDB表与索引S3桶是栈管理的Retain资源，3表均有deletion protection，
不能只核对replay。配置桶通过参数导入，栈不创建或修改它；其版本化、private/TLS-only
须按[外部前置条件](deploy.md#前置条件)核对，属性与生命周期由外部负责人管理。
Lambda保留版本也应清点，但不按固定物理名资源擅自接管。首次失败先读取 stack events、
物理 ID 和服务失败原因，保持旧 Gateway target，不反复执行同一 create。

1. 原栈仍存在且可更新时，优先修正获批配置并在原栈内重试，保留资源所有权；不要把原栈删除当作默认修复。
2. 若已回滚/删除而保留固定名资源，清点 `{prefix}-facade-logs`、`{prefix}-fallback-logs`、`{prefix}-ecs-search-logs`、`{prefix}-replay`及既有日志、状态/受保护表、由prefix/account/region组成固定名的S3桶等实际归属。Retain 不代表下次 create 自动采用同名资源；直接重试会报 `already exists`。
3. 对确为目标部署的保留资源，准备单独 CloudFormation import 模板及导入映射，核对类型是否支持导入、属性/DeletionPolicy 与实际一致，再由 部署负责人 批准执行导入后更新。导入与创建其他资源不能随意混成一个失败重试动作。若资源归属不明，停止并报告，不覆盖或接管。
4. 若导入不可行，向 部署负责人 提交具体资源/数据保留方案与唯一名称的新部署计划；更改 prefix 会影响角色、函数和配置，不能擅自把所有旧资源一起替换。若另行明确批准删除精确资源及其数据，先单独关闭获批精确表的DynamoDB deletion protection，确认关闭后才删除，再确认资源确实不存在后重试创建；Retain 不会自动解除数据删除保护。删除保留日志或 replay 是单独的数据删除动作，必须另行取得精确范围的授权。
5. `ROLLBACK_FAILED`/`DELETE_FAILED` 时针对具体事件解除阻塞后恢复原栈流程；不要盲目 skip 或删资源。已运行的 NAT、EIP、任务和失败栈资源可能继续计费，记录实际清单并申请后续清理。

恢复计划可由离线 `recovery` 子命令生成。沿用
[迁移输入表](migrate-to-lambda.md#输入与执行前置条件)中的变量；
`SEMSEARCH_REVIEWED_CANDIDATE` 是启用 ECS 的受审候选，
`SEMSEARCH_RETAINED_RESOURCE_INVENTORY` 的 schema、退役前归档来源与独立核对要求见
[保留资源恢复检查](migrate-to-lambda.md#保留资源与再次启用)。
`SEMSEARCH_RECOVERY_PLAN_OUTPUT` 必须是本次此前不存在的新文件路径，不能把旧计划当成本次成功产物。

```sh
.venv/bin/python scripts/preflight_ecs_deployment.py recovery \
  --live-template "$SEMSEARCH_FULL_LIVE_TEMPLATE" \
  --live-evidence "$SEMSEARCH_FRESH_STACK_METADATA" \
  --expected-stack-id "$SEMSEARCH_EXPECTED_STACK_ARN" \
  --candidate-template "$SEMSEARCH_REVIEWED_CANDIDATE" \
  --inventory "$SEMSEARCH_RETAINED_RESOURCE_INVENTORY" \
  --output "$SEMSEARCH_RECOVERY_PLAN_OUTPUT"
```

当前recovery工具只处理可取得fresh完整证据的现存稳定栈，捕获必须在900秒内且通过
独立预期StackId和完整模板hash绑定；超时重采，不能重盖时间。原栈已删除或仍处于
不允许更新的失败状态时，不能伪造空live模板通过它。由部署负责人另行安排恢复栈状态或新栈
导入方案，并保留精确资源归属/属性清单；本工具不会自动清理或重建栈。

固定名资源库存须核实归属并规范化为实际CloudFormation Properties；固定S3桶名仅解析已知AWS::AccountId/AWS::Region/AWS::StackName等伪参数，未知表达式必须拒绝，不能当没有固定名而跳过。缺记录不证明不存在。工具仅输出保持完整 live 模板的 IMPORT-only 计划和需另批的删除备选，不创建或删除资源。
`recovery` 成功生成计划与输入校验失败都会退出2；须确认本次新生成、完整可解析的输出，
并核对`status`为`IMPORT_REQUIRED`或`NO_OBSERVED_COLLISIONS`且`ready=false`，
不能只看退出码。缺输出、解析失败或stderr报错均停止；
`NO_OBSERVED_COLLISIONS`也不证明未入库存的资源不存在。
导入成功后重采完整模板再做更新，不能把 import 与正常 update 混做一个动作。服务更新使用前述 overlay 保留自动伸缩结果，并在授权部署前后实测 desired/running；模板层检查不能替代真实 CloudFormation 扩容后更新的验证。

## 常见故障与回滚

ECS全不健康时先核对task事件、镜像拉取、配置/索引预载、临时卷权限和健康检查路径。即使Lambda回退成功也保留主路故障告警。OAuth明确拒绝/TLS校验/重放拒绝/权限错误是拒绝条件；不能通过关闭验证、临时放宽IAM或将它们改成retryable来恢复服务。

出现稳定的闲置后长尾，先按单次请求关联客户端TCP/TLS、REQUEST Init、facade Init/排队、ECS服务时间、模型连接与embedding实得额度。健康检查通过不等于模型连接已热，也不证明用户请求不会触发JWKS或secret刷新。CPU平均值低不能排除瞬时排队、网络等待和控制读取。

target 引用未改变的 ECS 更新，首要回滚路径是 **CloudFormation 更新回滚**。
ECS 部署熔断只恢复服务到最近成功部署，并不证明整个栈已回滚；分别回读 task definition、
子网/public-IP、desired/running、注册目标及栈事件/状态。按本次实际变更逐节点核对
过滤器、namespace、端点、告警、版本/alias 与 Outputs，不能从服务健康推导栈已恢复。
`UPDATE_ROLLBACK_FAILED` 交 部署负责人 核对具体资源，不自动 skip 或删除资源。

回滚或关闭 ECS 后留下的固定名 replay 表及日志组，重新创建前按
[固定名资源恢复](#首次部署失败后的清理与重试)清点归属并使用离线 `recovery`
子命令准备导入计划；Retain 不会让后续 create 自动接管同名资源。

CloudFormation 不管理这里的外部 DNS、service OAuth 客户端与 Gateway target。
若回滚会删除 NLB，先恢复原 DNS 记录或删除本次新建的专用 A alias；NLB 重建后使用
新的实际 Outputs 重建记录并验证 TLS。确认后端已排空且无共享消费者后，才停用专用
OAuth 客户端；恢复时发布新有效凭据并验证，不能重开已撤销凭据。Gateway 的
`{prefix}-gateway-invoke` 由栈管理，其他栈外策略的恢复与它分别核对。切回旧 Search
前须回读实际策略；存在漂移时单独批准修复，再以同一 Gateway 执行角色实测精确 alias
Invoke；不能假设无变化的栈候选补回了权限，通过后才恢复 target。这些结果与栈回滚
分别留证；详细步骤见上方[部署顺序](#部署顺序与证据)。

任何恢复 ECS 接流的反向操作，先恢复后端及其 DNS/OAuth 依赖并验证服务健康，
再恢复撤除前的实际告警定义及原 `AlarmActions`，回读核对告警已恢复。
随后按[权限证据规则](migrate-to-lambda.md#切换前权限证据的有效性)完成新的同角色
Invoke 正控，才切 target 回 facade；不能先切流后补告警。

MetricFilter 删除和重建影响后续数据点，不搬迁已有指标历史。回滚时核对每个过滤器的名称、日志组、模式、命名空间及告警输入和动作；按实际变更集合逐项检查，不能仅比较资源数量。

**DynamoDB 权限验证。** 端点限制表、动作和 task role，键前缀由角色策略限制。以正确表和前缀验证读取/重放写入，以错误表、动作及另一后端前缀验证拒绝。任何正控失败或负控放行均停止扩大负载，按已审核底稿回滚并保留诊断。

**NAT现场观测。** 每AZ记录AWS/NATGateway的ErrorPortAllocation、PacketsDropCount（Sum/1分钟）和处理字节，结合task启动/IdP/Secrets/ECR/Logs故障时间线。用两方向流入字节估计处理GB并与账单口径核对：BytesInFromSource+BytesInFromDestination，不把四个in/out指标相加重复计费。任何错误/丢包非零暂停扩大负载并调查；按实际GB重算按量成本。当前 CDK 未配置 NAT 专属告警，持续无人值守监控仍有缺口；后续用实测阈值和SNS送达验证补齐，现有应用/目标告警不能证明NAT健康。

仅引用真的变更时，路由回滚才采用下一段；本次正常栈更新失败不重复切Gateway。

快速回滚由 部署负责人 确认的操作者将**同一个 Gateway target** 的 Lambda 引用恢复为部署前
记录的旧 Search alias/版本，并恢复对应固定配置引用；保留原身份和 schema，不删除
重放数据或修改 REQUEST。先回读原 alias 仍引用可用旧 Search 版本，验证服务契约和
Gateway 实际有效权限；缺权或漂移须单独批准修复，以同一执行角色实测精确 alias
Invoke 通过后才切 target。若部署过程复用了旧 alias 名称，必须通过保留的具体版本
恢复，不能把名称相同当作版本未变。模板声明同时授权 Search/facade 不代替实际策略
核验；路由回滚计划必须同时覆盖这两项引用。

回滚后验证tools/list、正常Search、授权拒绝与向量诊断，确认新请求不再经过facade；ECS保留至排空、证据收集与独立清理批准完成。关闭告警、缩容到0、删表/日志/镜像或删除任何数据都是后续获批操作，不包含在“回滚路由”动作中。永久退役时按[纯 Lambda 迁移手册](migrate-to-lambda.md)逐项处理 DNS、专用 OAuth 客户端和 Gateway IAM，再清理独占基础设施；不能让外部 alias 悬指已删 NLB。
