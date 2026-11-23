# 安全扫描与豁免维护

本地检查不需要 AWS 凭据或云端 API。执行以下固定版本扫描，发布前还需运行组织要求的完整安全策略。
本地规则回归通过不等于完整平台扫描通过，也不证明 Git 历史可公开。

## 规则和作用范围

| 检查 | 配置 | 约束 |
| --- | --- | --- |
| Semgrep 本地回归 | [规则锁文件](../.security/semgrep-rules.lock.json) | 13 条本地规则子集，不是平台完整策略；来源为上游 semgrep-rules 提交 `16b9e6d45f10649f6a531ec4838ed9924372c3bc`，规则正文不随仓库分发，按上游独立条款本地下载 |
| Bandit 运行时 | [bandit-runtime.yaml](../.security/bandit-runtime.yaml) | `src/semantic_core`、`src/semantic_service`、`infra`；B110/B311 均启用，诊断清理与两处调度抖动逐行豁免 |
| Bandit 开发工具 | [bandit-development.yaml](../.security/bandit-development.yaml) | `scripts`、`reports`、`examples`、`src/semantic_eval`、`setup.py`；固定本地命令、非密码学采样；高风险调用另经 Semgrep 审计 |
| Bandit 测试 | [bandit-tests.yaml](../.security/bandit-tests.yaml) | 只限 `tests`；合成口令、断言、临时目录和本地子进程；`exec` 和未验证 TLS 仍逐处审查 |
| Checkov | `--framework dockerfile` | 扫描容器 Dockerfile 的全部内置规则，无规则豁免 |
| Gitleaks | [.gitleaks.toml](../.gitleaks.toml) | 保留默认规则；仅 generic-api-key 的精确路径与字段形态同时满足时豁免，覆盖 Git 历史 |

本地 Semgrep 回归的目录排除以仓库根锚定；`.gitignore` 中的 build、dist 同步锚定，
嵌套产品目录不因名为 tests、build 或 dist 而排除。
根 tests 目录由 Bandit 测试策略遍历，其中 B101、B105、B106、B108、B311、B404、
B603、B607 按测试用途跳过，不等于所有规则全覆盖。
不得将这些测试豁免复制到产品代码。
`nosemgrep` 必须注明规则 ID，`nosec` 必须注明 B 编号，理由放在相邻注释或本表。
新增任何豁免都应确认输入来源及认证/网络边界，不能仅以“零告警”为依据。

## 精确豁免与 Git 历史

保留现有 `.gitleaks.toml`、`.gitleaksignore` 和扫描策略。历史 fingerprint 只对应特定
提交、文件、行与规则，不是整个文件或未来提交的豁免。
新增例外必须审查输入来源，以指定规则 ID 的行内注释说明理由；不要扩大路径排除来隐藏发现。

Gitleaks 使用 `--log-opts=HEAD` 限定当前分支历史，避免扫描共享仓库的其他分支。
不带 `--config` 也可能自动加载根配置；不能据此声称验证过另一套策略。
不要归档原始秘密输出，只保留位置、规则、提交及脱敏信息。
秘密扫描零发现不代表历史中没有私人部署记录、客户资料或内部标识。

## 本地复跑

先按 README 创建 Python 3.12 应用 `.venv`。扫描器安装进另一个环境，不改变运行时锁文件。
[requirements.txt](../.security/requirements.txt) 是顶层版本输入；
[requirements.lock](../.security/requirements.lock) 固定完整依赖及分发物 hash：

```sh
uv --no-config venv --python .venv/bin/python --no-python-downloads \
  --cache-dir .uv-cache .artifacts/scanner-venv
uv --no-config pip install --python .artifacts/scanner-venv/bin/python \
  --cache-dir .uv-cache --keyring-provider disabled --default-index https://pypi.org/simple \
  --no-deps --require-hashes --only-binary :all: -r .security/requirements.lock
```

另外从 Gitleaks 官方发行包安装 **v8.30.1** 到 `.artifacts/scanner-bin/gitleaks`。
扫描子进程的 HOME、缓存和工具状态显式限定到当前工作树，避免默认加载开发者配置；
这不是文件系统沙箱，不能约束恶意工具访问其它绝对路径：

```sh
mkdir -p .artifacts/security-home
security_state="$PWD/.artifacts/security-home"
security_scan() {
  env -i PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" \
    HOME="$security_state" \
    XDG_CONFIG_HOME="$security_state/config" XDG_CACHE_HOME="$security_state/cache" \
    SEMGREP_SETTINGS_FILE="$security_state/semgrep.yml" \
    SEMGREP_SEND_METRICS=off SEMGREP_ENABLE_VERSION_CHECK=0 \
    AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null \
    AWS_EC2_METADATA_DISABLED=true CHECKOV_SKIP_DOWNLOAD=true "$@"
}

security_scan .artifacts/scanner-venv/bin/python .security/prepare_semgrep.py && \
security_scan .artifacts/scanner-venv/bin/semgrep scan --config .artifacts/semgrep-rules/selected.yaml \
  --no-rewrite-rule-ids --metrics off --disable-version-check --error .
security_scan .artifacts/scanner-venv/bin/bandit -q -r src/semantic_core src/semantic_service infra \
  -c .security/bandit-runtime.yaml
security_scan .artifacts/scanner-venv/bin/bandit -q -r scripts reports examples src/semantic_eval setup.py \
  -c .security/bandit-development.yaml
security_scan .artifacts/scanner-venv/bin/bandit -q -r tests -c .security/bandit-tests.yaml
security_scan .artifacts/scanner-venv/bin/checkov -f containers/search/Dockerfile \
  --framework dockerfile --skip-download
security_scan .artifacts/scanner-bin/gitleaks git . --config .gitleaks.toml --redact=100 --log-opts=HEAD
```

规则准备脚本校验固定提交、原始文件 SHA-256 及所选规则定义 SHA-256，只选取锁文件中的
13 条规则。上游多规则文件中的其他规则不会自动加入。下载不读取 AWS 凭据、不使用环境
代理或跟随重定向；失败会移除旧生成配置。已有完整缓存时可加 `--offline` 复核。
若缓存损坏导致 checksum 错误，按错误信息定位 `.artifacts/semgrep-rules/<rule-id>.source`，
仅删除该本地缓存文件，再运行准备命令重新下载。不要修改锁文件中的 hash 来接受损坏内容。
下载正文受 [Semgrep Rules License](https://semgrep.dev/legal/rules-license) 约束，不要提交或分发
`.artifacts/semgrep-rules/`。完整性与离线选择测试：

```sh
security_scan .artifacts/scanner-venv/bin/python -m unittest discover -s .security -p 'test_*.py'
```

函数只为扫描子进程设置环境，不改变调用者的 HOME，也不继承任何 AWS profile 或凭据。

组织平台扫描**保留平台原有完整规则集**，不能用这 13 条本地回归规则替换。
平台完整规则的 ID 即使带前缀，也能识别现有短 ID 的行内 `nosemgrep`。
本地目录排除也不应扩大平台已有的扫描排除范围；Bandit 分组只说明本地例外，平台继续执行其完整策略。
本地扫描不代替发布批准。
Gitleaks 路径规则兼容 `git .`、`dir .` 与 `dir <绝对路径>`；目录边界使用 `(?:^|/)`，
完整文件名、行形态和 generic-api-key 规则限制仍须同时满足。
不要归档原始秘密扫描输出；归档文件只保留位置、规则、提交和脱敏形态。

## 上游依据

- [Python 3.12 HTTPSConnection](https://docs.python.org/3.12/library/http.client.html#http.client.HTTPSConnection)：默认校验证书；固定回环探针的显式例外见源码。
- [Gitleaks 配置](https://github.com/gitleaks/gitleaks#configuration)：默认规则扩展及 AND 条件 allowlist。
- [Semgrep 忽略说明](https://semgrep.dev/docs/ignoring-files-folders-code)：精确行和规则 ID 的豁免。
- [Bandit 配置](https://bandit.readthedocs.io/en/latest/config.html)：分组配置与 `nosec`。
