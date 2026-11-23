# 从 ECS 主路迁移到纯 Lambda

本手册用于保留 Gateway 服务、REQUEST 拦截器和现有数据，只把 Search 从
facade → ECS 主路切回直接调用 Search Lambda。ECS 代码与 `ecs_search` 开关继续保留，
默认关闭。迁移前重新核对当前配置、权限、回滚范围和[已知限制](known-limitations.md)。

**顺序必须是：先让 Search Lambda 可服务，再更新栈外 Gateway target，完成请求验证
与观察，最后退役 ECS。** Gateway target 不受本栈管理；直接提交关闭 `ecs_search`
的整栈 synth 会先删除它仍在调用的 facade，CloudFormation 回滚也不会自动修复 target。
本迁移不采用[整环境退役流程](operations-runbook.md#我要清理或退役环境)，不删除
Gateway，不执行永久 tombstone，不删除或回退授权数据。

## 输入与执行前置条件

在新的获批窗口重新取得有效会话，以本次目标区域显式指定每条 AWS 命令的 `--region`。
区域从你的部署清单取值。出现 `ExpiredToken` 或其他认证
失败立即停止。历史记录、资源数和本地 synth 不能代替当次现场读取。

先阅读[已知限制](known-limitations.md)中的启用与预热前置条件。迁移完成不代表
REQUEST 预热已适用于任意 IdP；依赖预热前仍须处理该项限制。迁移后如需重新启用
ECS，也须先修复该页列出的 ECS 阻断项，再按[ECS 部署手册](ecs-search-operations.md)
准备新的变更；固定名保留资源须先走[恢复检查](#保留资源与再次启用)，不能直接 CREATE。

本页变量均来自当次批准的部署清单或现场只读结果；不要从资源名猜值，也不要记录 secret、
JWT、完整认证头或私钥。迁移材料存入本次自选的证据目录，不能依赖某台机器已有的
`.artifacts` 文件。

| 输入 | 来源与用途 |
| --- | --- |
| `SEMSEARCH_PROFILE`、`SEMSEARCH_REGION`、`SEMSEARCH_ACCOUNT_ID`、`SEMSEARCH_EXPECTED_STACK_ARN` | 操作身份与完整 Stack ARN；预期值独立来自交接记录，按[采集规程](ecs-search-operations.md#离线部署预检与完整模板保护)导出，不能从待验信封反抄 |
| `SEMSEARCH_GATEWAY_ID`、`SEMSEARCH_SEARCH_TARGET_ID` | 读取同一个现有 Gateway 和 search target，保存完整 target 配置 |
| `SEMSEARCH_REQUEST_ALIAS_ARN`、`SEMSEARCH_SEARCH_ALIAS_ARN`、`SEMSEARCH_FACADE_ALIAS_ARN` | 现场 alias 与版本；目标为 Search alias，不能用私有 fallback alias 代替 |
| `SEMSEARCH_SEARCH_ZIP`、`SEMSEARCH_SEARCH_CODE_SHA256` | 本次受审提交构建的 Search ZIP 与实际代码摘要；与 manifest、依赖锁和 handler 一起核对 |
| `SEMSEARCH_FIXED_CONFIG_REFERENCE` | 固定 S3 bucket/key/VersionId/hash 及模型、签名引用，只记录非秘密元数据 |
| `SEMSEARCH_ECS_HOSTNAME`、`SEMSEARCH_DNS_ZONE_ID`、`SEMSEARCH_DNS_RECORD_BEFORE` | 专用 DNS 主机名、zone 和完整原记录集，包括原先不存在的状态 |
| `SEMSEARCH_SERVICE_OAUTH_CLIENT_ID`、`SEMSEARCH_SERVICE_OAUTH_SECRET_REFERENCE` | 专用客户端与 secret 引用及各自负责人；不记录 secret 值 |
| `SEMSEARCH_GATEWAY_ROLE_ARN`、`SEMSEARCH_GATEWAY_POLICIES_BEFORE` | Gateway trust、全部 inline/managed policy 与业务调用权限的完整基线 |
| `SEMSEARCH_MIGRATION_EVIDENCE_DIR` | 本窗口新建的证据目录；保存 fresh 底稿、资产清单、候选、逐项差异、正反向结果 |
| `SEMSEARCH_FULL_LIVE_TEMPLATE`、`SEMSEARCH_FRESH_STACK_METADATA` | 采集规程产出的完整 `live-template.json` 与 `live-evidence.json`；绑定同一 Stack ARN、完整分页和模板摘要 |
| `SEMSEARCH_REVIEWED_CANDIDATE` | 从本次受审代码及启用配置生成、逐项审核的再次启用完整模板文件；用于识别需要导入的固定名节点 |
| `SEMSEARCH_RETAINED_RESOURCE_INVENTORY` | 退役前归档生成并与当前实际资源独立核对的库存 JSON 文件；结构见[恢复检查](#保留资源与再次启用)，不是退役后栈资源列表 |
| `SEMSEARCH_RECOVERY_PLAN_OUTPUT` | 本次调用专用且此前不存在的输出文件路径，位于证据目录；不能复用旧成功输出 |
| `SEMSEARCH_ECS_ON_TEMPLATE`、`SEMSEARCH_ECS_OFF_TEMPLATE` | 同一受审提交、工具链、账号/区域/栈名、公共配置及资产的开/关 ECS 完整 synth JSON；仅改变 `ecs_search` 及其必需资产参数，用于告警差集盘点，不作为直接部署候选 |
| `SEMSEARCH_ECS_ALARM_INVENTORY` | 本次此前不存在的本地输出文件路径，保存步骤 5 生成的完整 ECS 专属告警清单 |

现场读取至少覆盖稳定的栈状态、Original、实际参数和 Outputs、完整资源分页、Gateway 与
target、六个 Lambda 的 Code/alias/Version/PC、ECS 服务和实际伸缩容量、网络、IAM、
DNS、证书及 OAuth 客户端生命周期状态。确认所有拟退役资源独占于该后端，没有共享消费者。
读取或变更客户端管理面须由其负责人完成，不借迁移读取凭证明文。

### 切换前权限证据的有效性

以下是正向与反向切 target 共用的执行规程，不是 SDK 或 IAM 的缓存 TTL。
同角色 Invoke 结论只对同一批准窗口、Gateway 执行角色与调用身份、精确 alias 及其
版本/配置有效；任何栈更新、策略/trust/权限边界、身份、alias/配置或 target 变更都使
旧结论失效，包括测试权限的授予和撤回，即使最终恢复原值也须重跑。
每次切 target 前立即重新回读实际有效策略及 alias，并以实际 Gateway 执行角色
成功调用所需精确 alias，记录时间、身份和配置摘要；通过后直接执行该次切换，
中间如再发生上述变更就停止并重做。操作者身份、模拟器、过期会话或不可用的测试主体
不能替代该证据。取不到有效的同角色 Invoke 正控时记 UNKNOWN，保持当前 target、
不执行切换，待获批验证通过后再继续。其他步骤引用“同角色 Invoke 通过”时均适用本规则。

## 候选与删留边界

分开审核 Search 发布、ECS 专属告警撤除、ECS 退役三份 CloudFormation 候选，
后两份在切流验收后依次执行。每份都以当次 fresh Original 为底稿，列出全部新增、
修改、删除及保留项和依赖，不把关闭开关的原始 synth 当作可直接执行的候选。
**当前退役候选需要人工编辑完整 JSON 并独立复审，没有一键生成或自动放行工具。**
制作时复制当次 Original：Search 候选只合入受审 synth 的 Search Code、新 Version
和 alias 引用；告警候选只移除批准的 ECS 专属告警；退役候选按开关两侧差异逐逻辑 ID
删除 ECS 节点，更新 `GatewayInvokePolicyD60C93F0` 并移除失效引用和 Outputs，
其余节点逐属性冻结。`GatewayInvokePolicy` 是[栈定义](../infra/stack.py)中的 construct ID，
本版模板的逻辑 ID 带上述 CDK 路径哈希后缀；编辑前还须以 fresh Original 中的
`PolicyName = {prefix}-gateway-invoke` 及绑定角色确认同一节点，不能新建一个同名 construct ID 节点。
删除带 `Retain` 的模板节点须另列为“物理资源保留、退出栈管理”，不能算作数据删除。
分别保存完整 JSON、增改删清单、依赖检查及反向候选；材料不齐就不进入写入阶段。

[现有 overlay](ecs-search-operations.md#离线部署预检与完整模板保护)只能删除获准的
诊断 MetricFilter，不能删除 ECS Service、告警或 Outputs，不能用于上述退役候选。
若用它辅助 Search 发布，所有现存 ECS Service 也须显式选入并移除 `DesiredCount`，
另审容量保持语义；它不自动验证新 Version 的 `CodeSha256`。不能绕过守卫、
修改历史常数或把人工候选声称为该工具已放行。

Search 使用当前受审源码构建的新 shared ZIP，但第一阶段只更新 Search Function Code、
新增绑定实际 `CodeSha256` 的 Search Version，并推进 Search alias。REQUEST、Indexer、
Enhancer 的 Code、环境、IAM、alias、Version 和计划任务原样保留；不因共用构建包就推进
它们的版本。固定配置、模型和签名引用、工具协议与权限边界也保留，实质变更单独审核。
入口契约见[Search runtime](../src/semantic_service/runtime.py)与
[私有 fallback handler](../src/semantic_service/fallback.py)。

| 资源或配置 | 处置原则 |
| --- | --- |
| REQUEST Function、角色、alias、全部旧 Version、日志与四项 request-init 监控 | 逐属性保留 Code、当前 PC 配置、监控及 alias 的 `DependsOn`；执行时核对并保留已批准版本 |
| Search、Indexer、Enhancer 及基础监控 | 只批准上述 Search 更新；保留旧 Search Version 供回滚，Indexer/Enhancer 的代码、角色、调度与异步设置不动 |
| 索引桶、state 表（栈管理） | 零删除、零替换；索引桶保留版本化，state 保留 PITR、删除保护与 `expires_at` TTL，各自保留 `DeletionPolicy`/`UpdateReplacePolicy`；不回写旧 `CONTROL` |
| 配置桶（参数导入） | 通过 `ConfigurationBucketName` 引用，栈不创建或修改，没有该桶的模板资源或保留策略。private、versioned、TLS-only 及配置版本不变由外部负责人核对，见[部署前置条件](deploy.md#前置条件)；迁移不删除或覆盖配置对象 |
| protected 表 | 零删除、零替换，保留 PITR、删除保护与两项保留策略；按设计无 TTL，不得新增 TTL。永久 tombstone 若过期删除，可能让已删除 Gateway 重新生效；不回写旧 `PROTECTED`，见[存储实现](../infra/storage.py) |
| `{prefix}-replay`、`{prefix}-ecs-search-logs`、`{prefix}-facade-logs`、`{prefix}-fallback-logs` | 关闭开关后不在模板中，凭 `Retain` 保留物理资源并成为栈外资源；记录归属、表保护/TTL/PITR 与日志保留期，不删除内容。再次启用先按下方恢复检查处理固定名冲突 |
| facade/fallback Function、alias、Version、角色 | target 已切走、观察完成并排空后才退役；删除整个 Function 会删除其全部版本与 alias，Version 节点的 Retain 不能保住它们。退出模板的六个 Retain 节点中，这两个 Version 不计入四个保留的栈外数据/日志资源 |
| ECS Service、伸缩目标/策略、task definition、cluster | 先停止接流和排空，再按依赖退役；保留实际容量与镜像/配置记录供重建 |
| NLB、listener、target group、独占安全组、端点、NAT/EIP、子网/路由、VPC | 外部 DNS 引用已处理、ENI 等依赖已释放且确认无共享使用者后按依赖清理 |
| ECS/facade/fallback 专属指标、告警与失效 Outputs | 步骤 5 先单独撤除批准的 ECS 专属告警并回读，再停服退役；保留纯 Lambda、REQUEST 和后台任务的基础信号及通知动作 |
| Gateway Invoke 权限 | `{prefix}-gateway-invoke` 由栈管理：ECS 开启时含 Search/facade/REQUEST，关闭时由步骤 6 的栈更新移除 facade，保留 Search/REQUEST；不得手改制造漂移。导入角色上的其他策略、trust 与业务工具权限单独核对 |
| DNS、service OAuth 客户端、SNS、ECR、ACM、Secrets、hosted zone | 栈外逐项处理，见步骤 5；不删除共享 zone、证书、topic、镜像或 secret |

### 保留资源与再次启用

再次启用前先核对四个固定名资源的真实归属和属性；`Retain` 不会让新栈更新自动接管它们，
直接 CREATE 会遇到 `already exists`。退役前从完整模板、物理 ID 和参数捕获生成库存底稿，
再由负责人核对当前实际表/日志组及漂移；退役后的 `list_stack_resources` 已看不到这四项，
不能据此重建库存或判定资源不存在。库存须为以下结构：

| 层级/字段 | 必需内容 |
| --- | --- |
| 顶层对象的 `resources` | 资源对象数组；缺项或空数组不证明没有固定名冲突 |
| 每行 `Type` | 资源类型，如 `AWS::DynamoDB::Table` 或 `AWS::Logs::LogGroup` |
| 每行 `PhysicalId` | 该资源的实际表名或日志组名，与候选解析后的固定名匹配 |
| 每行 `Properties` | 完整规范化的 CloudFormation 属性对象；从退役前捕获解析伪参数/引用后生成，再与实际资源独立核对，不是 describe 响应原样拷贝 |

当前没有自动采集这些栈外资源库存的命令；需按上述结构准备文件。
[恢复实现](../infra/deployment_preflight.py)要求匹配资源唯一，`Properties` 与再次启用候选
解析后的全部属性结构相等；JSON 键顺序和缩进无关，不是比较原文件字节。

用于本次 recovery 的现存稳定栈捕获须重新取得，不能把退役前库存底稿冒充 fresh 信封。
[实现](../infra/live_evidence.py)要求捕获时间不在未来且距检查不超过 **900 秒**，
并核对完整分页、账号/区域/预期 Stack ARN 和规范化模板 SHA-256。
按[采集时序](ecs-search-operations.md#离线部署预检与完整模板保护)控制整个提交前流程，
不能每步重置 900 秒；过期就重采。先确认输出路径此前不存在，再运行本地命令：

```sh
.venv/bin/python scripts/preflight_ecs_deployment.py recovery \
  --live-template "$SEMSEARCH_FULL_LIVE_TEMPLATE" \
  --live-evidence "$SEMSEARCH_FRESH_STACK_METADATA" \
  --expected-stack-id "$SEMSEARCH_EXPECTED_STACK_ARN" \
  --candidate-template "$SEMSEARCH_REVIEWED_CANDIDATE" \
  --inventory "$SEMSEARCH_RETAINED_RESOURCE_INVENTORY" \
  --output "$SEMSEARCH_RECOVERY_PLAN_OUTPUT"
```

`recovery` 正常产出计划和参数/文件/证据校验硬失败都会退出 **2**。
必须同时确认这是本次调用新生成的可解析输出、stdout 含
`PLAN_ONLY: unresolved deployment prerequisites`、stderr 无错误，并核对 `ready=false`
及下列明确状态；缺输出、旧输出、解析失败或错误均停止，不能仅凭退出码继续。

| `status` | 可接受的计划形态与后续 |
| --- | --- |
| `IMPORT_REQUIRED` | 非空 `resources_to_import` 和完整 `import_template`；独立审核 IMPORT-only 变更，导入完成后重采 Original，再另做启用更新 |
| `NO_OBSERVED_COLLISIONS` | 空导入列表、`import_template=null`；只表示库存未观察到匹配冲突，仍须核对库存完整性与实际归属，不代表自动放行 |

输出还恒含 `destructive_alternative.requires_explicit_approval=true` 的删除备选，
其中关闭删除保护、删除资源和数据都须另批；它不是已获授权的恢复步骤。
本手册默认保留数据并走必要的受审导入，不把导入和创建其他资源混做。
栈已删除、状态不稳定或属性不符时停止，按[固定名恢复流程](ecs-search-operations.md#首次部署失败后的清理与重试)
处理，不伪造空底稿或删除数据来避开冲突。

## 分阶段执行与回滚

### 1. 核对现场并批准具体候选

完成上述读取、受保护资源对比、Search 资产核验和外部资源清单。优先在隔离环境用真实
测试主体验证 Search 与 Gateway IAM 的正负控，再把精确策略用于共享环境；模拟器或本地
fixture 不能替代这项验证。隔离验证未完成时，保留未通过结论，由变更负责人明确批准
受控验证范围，不能默认把首次权限试验放到共享入口。

真实变更集、每阶段停止条件、观察长度、回滚操作者及外部资源负责人均在执行前确认。
本手册不包含已经生成或获批的变更集。任何未解释差异、共享消费者或不完整权限结论
都停止进入写入阶段。

**反向步骤：** 本阶段没有线上写入，取消候选并保留当前服务；若单独创建了隔离测试资源，
按其批准清单撤回测试主体权限并清理测试资源，不能触及共享数据。

### 2. 先让 Search Lambda 可服务

执行 Search 专项候选，保留 ECS 实际容量和当前 facade target。核对新版本实际摘要、
handler、固定配置引用及 alias，再用获批测试身份验证返回格式、授权过滤、签名上下文和
伪造上下文拒绝。[栈实现](../infra/stack.py)在 ECS 开关两侧都授权同一个 Search alias，
推进 Version 不改变 alias ARN，也不改变该策略。必须把实际 `{prefix}-gateway-invoke`
及角色全部有效策略与模板逐项比对；若有手工漂移或实际拒绝，保持 facade target，
另批明确的漂移修复动作，不能把无变化的候选或一次 alias 更新当作补权。
修复后回读实际策略，并经获批验证路径取得**实际 Gateway 执行角色调用精确 Search alias**
的成功证据，连同必要负控通过后才进入步骤 3。操作者身份调用或模拟器不能替代该正控；
角色不可用于获批验证、权限或漂移仍未查明时停止，不能先切 target 试权。

**反向步骤：** 保持 target 指向 facade；Search 验证失败则用保留的旧 Search Version
恢复 alias 和本次确实修改的配置，回读旧摘要及实际权限。漂移修复的反向动作单独审核，
不能恢复一个已知缺少 Search 权限的错误状态，也不手改栈管理策略撤权。
REQUEST、Indexer、Enhancer 和共享授权数据不随 Search 回滚。

### 3. 更新栈外同一个 Gateway target

按[权限证据规则](#切换前权限证据的有效性)在本次切换前重新完成实际策略回读与同角色 Invoke，
再重新读取 target，以当前值构造只修改
`targetConfiguration.mcp.lambda.lambdaArn` 的请求：

```text
SEMSEARCH_FACADE_ALIAS_ARN -> SEMSEARCH_SEARCH_ALIAS_ARN
```

保留 target ID/name/description、内联 toolSchema、`GATEWAY_IAM_ROLE` credential
provider、registry/incarnation 及全部业务 target；REQUEST interceptor 仍绑定原
REQUEST alias。不删除重建 target，不改工具名或 `_auth_context` 协议。
该动作在有损维护窗口执行，暂停批量流量并记录切换时间。回读 READY、实际 ARN、
schema、credential provider 和 REQUEST 绑定，再经 Gateway 验证发现工具、正常 Search
和权限拒绝；确认 Search 收到新流量，facade/ECS 不再承接新请求。

**反向步骤：** ECS 仍健康时，先确认 facade alias、DNS/TLS、OAuth 和原 Invoke 权限
有效，确认原 ECS 告警及 `AlarmActions` 完整存在并通过新的同角色 Invoke 后，
再把同一个 target 的 ARN 恢复为本窗口记录的 facade alias。回读完整 target
并重新执行正负控。仅 CloudFormation 回滚不能恢复此栈外绑定；切流失败时不删除 ECS。

### 4. 观察并关闭快速切回窗口

在批准的观察窗口覆盖正常请求、完整索引轮询、闲置后首批和并发请求，保存全部错误、
超时及请求分母。继续核对 REQUEST PC READY=1、四项 init 监控、后台调度和业务工具。
热 p95 1.5s、冷 p95 4s 来自设计规划，SLO 未经确认；它们不是本窗口临时设定的通过阈值。
纯 Lambda 迁移结果必须重新测量，不能沿用历史 ECS 样本。

通过前保留 ECS 的实际容量、网络、DNS、OAuth 客户端及 facade Invoke 权限。负责人
确认观察通过并批准退出快速切回窗口后，才进入外部引用和基础设施退役。

**反向步骤：** 出现授权、契约、错误率或未解释长尾异常时，停止扩大负载，按步骤 3
恢复 facade 绑定并验证；必要时再按步骤 2 回退 Search。不能先缩容到零或删 NLB/NAT
再决定是否回滚。

### 5. 撤除专属告警并退役外部引用

观察通过后，先用单独候选撤除已批准的 ECS 专属告警。获批清单须从**完整的开关
synth 告警差集**逐项审核，覆盖所有可能在排空期间触发的告警，不得只照抄
`{prefix}-ecs-running-tasks`、`{prefix}-ecs-healthy-targets`、
`{prefix}-ecs-unhealthy-targets` 三项，也不能按名称是否含 `ecs` 筛选。
本次配置通知主题的双 China 区域离线 synth 均新增 **30 个告警，30/30 带 `AlarmActions`**，其中仅 18 个
名称含 `ecs`；集合含 facade/fallback、TLS、worker 和后端错误等信号，定义见
[ECS 告警](../infra/ecs_search.py)与[监控定义](../infra/monitoring.py)。

按[本地 synth 参数](../scripts/synth.py)生成输入表中的同配置开关两份模板后，
从工作树根目录运行以下本地盘点。输出保留每个 ECS 专属告警的完整模板节点，
包括 `AlarmName`、阈值/周期、`TreatMissingData` 和原 `AlarmActions`：

```sh
.venv/bin/python - "$SEMSEARCH_ECS_ON_TEMPLATE" "$SEMSEARCH_ECS_OFF_TEMPLATE" "$SEMSEARCH_ECS_ALARM_INVENTORY" <<'PY'
import json
import sys
from pathlib import Path

def alarms(path):
    resources = json.loads(Path(path).read_text())["Resources"]
    return {
        logical_id: resource
        for logical_id, resource in resources.items()
        if resource["Type"] == "AWS::CloudWatch::Alarm"
    }

enabled, disabled = alarms(sys.argv[1]), alarms(sys.argv[2])
exclusive = {key: enabled[key] for key in sorted(enabled.keys() - disabled.keys())}
with_actions = [
    key for key, resource in exclusive.items()
    if resource.get("Properties", {}).get("AlarmActions")
]
inventory = {
    "Resources": exclusive,
    "alarm_count": len(exclusive),
    "alarm_actions_count": len(with_actions),
    "alarm_actions_logical_ids": with_actions,
}
with Path(sys.argv[3]).open("x", encoding="utf-8") as output:
    json.dump(inventory, output, ensure_ascii=False, indent=2)
    output.write("\n")
PY
```

此方法只盘点，不校验输入配置是否一致、不生成撤除候选，也不自动授权。下个窗口须
重新生成并核对完整集合，30 不是固定放行常数；零项或未解释的差异须先查明。
命令退出 0、产物完整后，从 fresh Original 按 `Properties.AlarmName` 映射实际逻辑 ID，
逐项核对原动作和触发条件，补齐现场额外的 ECS 专属告警；任何保留项须证明不会因排空触发并记录理由，
否则纳入获批的先行撤除清单或停止退役。不能把裸名称或查无结果当成已撤除。
确认撤除完成后才停止/排空服务或退役外部依赖。
不依赖同一栈更新的删除顺序，不关闭 REQUEST、Search 或后台任务的基础告警。
保存完整原告警定义，明确包含原 `AlarmActions`（以及原有其他动作/启用状态）；
撤除期间由操作者持续核对指标和事件。取消退役且服务未变时恢复并回读原告警；
服务已拆除时按“重建健康 → 恢复原告警及原 `AlarmActions` 并回读 → 新同角色 Invoke
正控 → 切回 target”的顺序执行，不保留流量已切回而告警仍未恢复的空窗。

再确认 facade/ECS 没有在途请求和共享消费者。逐项保存执行前的完整元数据，并按
各资源负责人批准的范围操作。导入的 Gateway 角色与 trust 在栈外，
`{prefix}-gateway-invoke` 策略本身由栈管理；本步骤不手动修改它：

| 外部对象 | 正向处置及通过依据 | 反向步骤 |
| --- | --- | --- |
| DNS A alias | 在删除 NLB 前，删除确认独占的 ECS A alias；若本次启用曾覆盖原记录则恢复原记录。回读确认不再指向待删 NLB，不删除 hosted zone 或共享记录 | NLB 仍存在时恢复先前完整记录并验证解析/TLS；若 NLB 已重建，用新 `SearchNlbDnsName` 与 `SearchNlbCanonicalHostedZoneId` 更新记录，不能复用旧目标 |
| 专用 service OAuth 客户端 | 确认无其他消费者后停用客户端、撤销专用凭据并核对 IdP 状态。已签发 token 可能继续有效，按实际 token 生命周期和验证端策略等待/验证拒绝，不能把撤销客户端等同于立即撤销所有 JWT | 由 IdP 负责人重新启用或创建专用客户端，发布新的 secret 版本并验证 issuer/audience/subject/scope、JWT 寿命及 token 获取/验证；已撤销凭据不重新启用 |
| Gateway 角色的其他栈外策略 | 仅按批准清单处理其他 inline/managed 策略中的多余 facade/wildcard 授权，保留 Search/REQUEST、trust 及业务权限。此时栈管理策略仍允许 facade，不能以 facade 调用仍被允许判失败；步骤 6 完成后按步骤 7 执行 facade Invoke 拒绝验证 | 在切回前恢复确有必要的栈外精确授权并回读；栈管理策略若有漂移则按步骤 2 单独修复。按权限证据规则取得新的同角色 Invoke 成功结果后才切 target，不用 `Resource: "*"` 代替恢复 |
| SNS、ECR、ACM、secret 引用和 hosted zone | 默认保留以便审计及未来受审重建；secret 内容不读取，删除或证书撤销另行批准 | 若另批清理，恢复方案须在该动作前落实；不能依赖已经删除的镜像、证书或 secret |

**反向步骤：** 本阶段开始后不再承诺立即切回。先按表恢复必要 DNS、专用客户端与精确
Invoke 权限并验证 ECS 路径健康，随后恢复原 ECS 告警及原 `AlarmActions` 并回读，
最后重做同角色 Invoke 正控，通过后才按步骤 3 切回 target；恢复期间保持 Search 接流。
不要把 Gateway target 切到依赖、实际权限或告警尚未恢复的 facade。

### 6. 按依赖退役 ECS 基础设施

再取 fresh Original 与依赖状态，确认切流已验证、步骤 5 的告警已先撤除，再执行退役候选。
先停止并排空后端消费者，退役 ECS Service/伸缩和 facade/fallback；再按实际依赖退役
task definition、listener/target group/NLB、cluster、专属 IAM 角色与策略。确认服务、
Lambda VPC ENI、NLB 和其他消费者已释放后，才能清理独占端点、NAT/EIP、安全组、
子网/路由、Internet Gateway 及 VPC。具体删除顺序由候选依赖图和真实变更集核验，
不能按资源名称批量删除。

关闭开关的候选同时把栈管理的 `GatewayInvokePolicyD60C93F0` 更新为 Search/REQUEST，
由该栈更新撤掉 facade 引用；回读实际策略，不能仅看模板推断权限已生效。
移除批准的剩余 ECS 专属监控与失效 Outputs。replay 表与三个 ECS 日志组的模板节点
退出栈管理，物理资源按 `Retain` 保留；分别登记栈外归属、属性、保护设置和保管人。
索引桶、state/protected 表继续受栈管理，导入配置桶继续由外部负责人维护；
核心旧 Version 按候选保留并逐项核销。任何物理数据删除、
替换或未批准的 REQUEST 差异都停止执行。`Retain` 不证明仍在栈内或仍可调用。
**从 Retain 节点移除提交起，本步骤按不可直接反向重放的退役管理。**
节点实际退出栈管理后，重新提交旧模板会尝试 CREATE，不能撤销这次归属变化；
恢复必须先核对是否需要另行导入或重建。

**反向步骤：** 所有恢复分支都先保持 Search 接流，按[保留资源恢复检查](#保留资源与再次启用)
核对实际栈归属与固定名冲突。ECS Service 熔断只恢复服务部署，须另查栈事件；
若节点仍在栈内且受审回滚可行，按其实际状态恢复，不能从服务健康推断整个栈已回滚。
只要四个固定名 Retain 资源中的任一已退出栈管理，就先完成必要的受审导入并重采 Original，
不得重提旧候选让它 CREATE。facade、网络或客户端已退役时，用保存的镜像、ZIP、
配置和新 Original 生成恢复候选，依次恢复网络/角色/端点、服务、facade/fallback
及栈管理 Invoke 授权，再恢复 DNS/OAuth 和必要栈外权限。
验证后端就绪、TLS、身份和请求后，先恢复原告警及原 `AlarmActions` 并回读，
再按权限证据规则重做同角色 Invoke，通过后才切回 target。禁止覆盖历史
225 资源模板；受保护数据与最新授权状态始终保留。

### 7. 完成回读与交付

步骤 6 的撤权更新完成后，除回读全部有效策略，还须以实际 Gateway 执行角色
对原精确 facade alias **执行一次 Invoke 请求并取得明确的 IAM 权限拒绝**，
同时验证 Search/REQUEST/业务正控。其他 inline/managed 策略或既有 wildcard 可能
仍放行 facade；任何放行均停止签收并按批准范围处理，不能只看栈管理策略已移除引用。
`ResourceNotFoundException`、过期凭证、错误主体或不可用的测试身份不是权限拒绝证明；
即使 facade 已删除，也不能把查无资源标作负控通过，无法取得有效拒绝时记 UNKNOWN、
停止迁移签收并安排获批验证。保存拒绝调用的身份、精确资源、时间与脱敏错误证据。
以 UNKNOWN 收尾时保持已验证的 Search 接流，登记待办和负责人、暂停签收并安排获批后续验证，
不因该负控未知而盲目切回 facade，也不把 UNKNOWN 改记为通过。

确认 target 直连 Search；Search 版本/摘要、REQUEST 最新批准版本与 PC READY=1、
四项 init 监控及其依赖、业务调用权限和 Indexer/Enhancer 计划均与候选相符。
逐项核销 ECS 退役清单、外部 DNS/客户端处置、栈管理与栈外 IAM 的实际状态；
分开核销仍受管理的资源和四个保留的栈外资源，确认无未处理漂移，保存最终
Original、资源分页、target 和策略快照、正负控、延迟样本及未知项。

**反向步骤：** 未通过最终核对就不宣布迁移完成；Search 自身问题按步骤 2 的兼容版本
回滚处理。需要恢复 ECS 时按步骤 6 重建和验证，不能只把 target 改回旧 ARN。
真实故障注入、凭证轮换、冷回退、自动伸缩和 SNS 送达等既有未验收项继续见
[已知限制](known-limitations.md)，不因本手册提交或迁移成功而自动关闭。
