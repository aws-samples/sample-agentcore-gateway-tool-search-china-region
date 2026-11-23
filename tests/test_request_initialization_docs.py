"""The documented failure query must include failed provisioned initialization."""

import re
import subprocess  # nosec B404
from pathlib import Path

import pytest


def test_runbook_failure_query_includes_request_init():
    text = Path("docs/operations-runbook.md").read_text()
    matches = re.findall(r"```text\n(.*?)```", text, re.DOTALL)
    assert any(
        re.search(r'filter operation in \[[^\]]*"request_init"', query)
        and re.search(r'filter status in \[[^\]]*"failed"', query)
        for query in matches
    ), "failed REQUEST Init is excluded from the documented failure query"


def test_service_timing_covers_prepared_factory_and_init_failure():
    text = Path("docs/service.md").read_text()
    assert "request_entry_import_ms" in text
    assert "request_init" in text and "handler.factory" in text
    assert "REQUEST initialization failed" in text


def test_service_platform_timing_distinguishes_provisioned_init():
    text = Path("docs/service.md").read_text()
    paragraph = text.split("平台 **Lambda Init Duration**", 1)[1].split("\n\n", 1)[0]
    assert "INIT_REPORT" in paragraph
    assert "按需" in paragraph and "预置并发" in paragraph


def test_runbook_can_query_ready_init_with_failed_prefetch():
    text = Path("docs/operations-runbook.md").read_text()
    queries = re.findall(r"```text\n(.*?)```", text, re.DOTALL)
    query = next(query for query in queries if 'record.oidc_prefetch.status = "failed"' in query)
    assert 'record.operation = "request_init"' in query
    assert 'record.status = "ready"' in query
    for field in (
        "record.oidc_prefetch.failure_kind",
        "record.oidc_prefetch.auth_failure.category",
        "record.oidc_prefetch.metadata_recovered",
        "@logStream",
    ):
        assert field in query


def test_runbook_distinguishes_init_attempts_from_missing_request_phase():
    text = Path("docs/operations-runbook.md").read_text()
    queries = re.findall(r"```text\n(.*?)```", text, re.DOTALL)
    assert any(
        'record.operation = "oidc_fetch"' in query
        and 'record.phase = "init"' in query
        and "@logStream" in query
        for query in queries
    )
    assert any(
        'record.operation = "oidc_fetch"' in query and "not ispresent(record.phase)" in query
        for query in queries
    )
    assert not any('record.phase != "init"' in query for query in queries)


def test_authoritative_jwks_schema_covers_prefetch_phase_and_null_request_id():
    text = Path("docs/service.md").read_text()
    section = text.split("#### JWKS 逐次读取事件", 1)[1].split("#### 冷路径认证恢复", 1)[0]
    assert "| `phase` |" in section
    assert "请求期省略" in section
    assert "Init 固定为 `null`" in section
    assert "`verify` 或 `_prefetch`" in section
    assert "operation=request_init" in section


@pytest.mark.parametrize(
    "document",
    [
        "docs/service.md",
        "docs/security-and-data-flow.md",
    ],
)
def test_authoritative_diagnostics_disclose_init_failure_and_partial_recovery(document):
    text = Path(document).read_text()
    for field in ("oidc_prefetch", "failure_kind", "metadata_recovered", "phase"):
        assert field in text
    for kind in ("dependency", "structural", "deadline"):
        assert kind in text


def test_request_deployment_documents_monitoring_before_pc():
    text = Path("docs/deploy.md").read_text()
    section = text.split("## REQUEST 预置并发验证", 1)[1].split("## 已有同表数据迁移", 1)[0]
    for resource in (
        "request-init-auth-recovered",
        "request-init-oidc-prefetch-failed",
        "AWS::Logs::MetricFilter",
        "AWS::CloudWatch::Alarm",
    ):
        assert resource in section
    assert "Sum > 2" in section
    assert "先创建监控，再分配 PC" in section
    assert "意外变化时停止部署" in section
    assert "[部署步骤](docs/deploy.md)" in Path("README.md").read_text()


def test_request_deployment_measures_expired_cache_and_stale_connection_risk():
    text = Path("docs/deploy.md").read_text()
    for phrase in (
        "Init 后超过 300 秒",
        "keepalive_expiry",
        "network/read",
        "network/protocol",
        "monotonic",
        "缓存年龄",
    ):
        assert phrase in text
    assert "5 秒" in text


@pytest.mark.parametrize("document", ["docs/service.md", "docs/deploy.md"])
def test_prefetch_total_budget_is_separate_from_platform_init_limit(document):
    text = Path(document).read_text()
    assert "3.0 秒" in text
    assert "discovery" in text and "JWKS" in text and "补偿" in text
    assert "factory" in text
    assert "预置并发 Init" in text and "10 秒" in text
    assert "deadline" in text


def test_prefetch_separate_client_does_not_prewarm_handler_connections():
    service = Path("docs/service.md").read_text()
    assert "父进程" in service
    assert "独立 HTTP 客户端" in service
    assert "独立 verifier" in service
    assert "关闭" in service
    assert "不预热请求客户端的 HTTP 连接池" in service
    assert "使用同一个 verifier 和 HTTP client" not in service
    assert "posix_spawn" not in service


def test_known_limitations_does_not_promise_prefetch_benefit_after_idle_ttl():
    text = Path("docs/known-limitations.md").read_text()
    paragraph = next(
        paragraph for paragraph in text.split("\n\n") if "Init 后超过 300 秒" in paragraph
    )
    assert "零" in paragraph
    assert "连接池" in paragraph
    oidc_row = next(row for row in text.splitlines() if row.startswith("| OIDC / JWKS"))
    assert "预置并发 Init 预取" in oidc_row


def test_documented_finch_loop_preserves_the_first_failure():
    blocks = re.findall(r"```sh\n(.*?)```", Path("docs/deploy.md").read_text(), re.DOTALL)
    snippet = next(block for block in blocks if "for SEMSEARCH_INIT_MODE" in block)
    stubs = """
    mktemp() { printf '/private/tmp/documented-command-fixture\\n'; }
    unzip() { return 0; }
    cp() { return 0; }
    finch() {
      case "$*" in
        *AWS_LAMBDA_INITIALIZATION_TYPE=on-demand*) return 7;;
        *) return 0;;
      esac
    }
    """
    # Execute the committed shell example with local stubs, never external input.
    # nosemgrep: dangerous-subprocess-use-audit
    result = subprocess.run(  # nosec B603
        ["/bin/sh", "-c", stubs + snippet],
        env={},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 7, "a later successful Finch mode hid the first failure"


@pytest.mark.parametrize(
    "document",
    ["docs/service.md", "docs/security-and-data-flow.md"],
)
def test_prefetch_docs_bind_parent_execution_and_optional_baseexception_boundary(document):
    text = Path(document).read_text()
    for field in (
        "父进程",
        "独立 HTTP 客户端",
        "独立 verifier",
        "日志 FD",
        "SystemExit",
        "KeyboardInterrupt",
        "factory",
    ):
        assert field in text
    for obsolete in ("posix_spawn", "私有 IPC", "控制帧"):
        assert obsolete not in text


@pytest.mark.parametrize("document", ["docs/service.md", "docs/known-limitations.md"])
def test_prefetch_docs_state_signal_ownership_and_soft_deadline_limits(document):
    text = Path(document).read_text()
    for phrase in (
        "2.75 秒",
        "0.25 秒",
        "SIGALRM",
        "非主线程",
        "屏蔽",
        "pending",
        "自定义 handler",
        "getaddrinfo",
        "硬实时",
        "父进程",
        "structural",
    ):
        assert phrase in text
    assert "3.0 秒" in text
    assert "Init 摘要" in text


@pytest.mark.parametrize(
    "document",
    ["docs/service.md", "docs/security-and-data-flow.md"],
)
def test_prefetch_docs_expose_only_bounded_partial_key_validation_counts(document):
    text = Path(document).read_text()
    for phrase in ("公钥材料", "归一化", "首请求", "TTL", "jwks_validation"):
        assert phrase in text
    for field in ("accepted", "rejected", "rejection_reasons"):
        assert field in text
    for reason in (
        "usage",
        "key_ops",
        "private_material",
        "algorithm",
        "missing_material",
        "encoding",
        "material",
    ):
        assert f"`{reason}`" in text
    assert "rejected > 0" in text
    assert "request-init-oidc-prefetch-failed" in text
    assert "不接纳部分缓存" in text


def test_service_distinguishes_prefetch_idle_limits_from_request_and_total_budget():
    text = Path("docs/service.md").read_text()
    section = text.split("### 冷启动分段计时", 1)[1].split("每条 runtime 请求日志", 1)[0]
    for phrase in ("0.8 秒", "0.35 秒", "25–75ms", "2 秒", "0.5 秒", "idle"):
        assert phrase in section
    assert "共享一次补偿" in section
    assert "逐操作" in section and "总上限" in section


def test_docs_preserve_parent_process_benchmark():
    benchmark = Path("docs/developer-guide.md").read_text().split("## 本地初始化基准", 1)[1]
    benchmark = benchmark.split("## 专项验证", 1)[0]
    for phrase in ("同一进程", "独立 HTTP 客户端", "线上收益仍须部署后"):
        assert phrase in benchmark
    service = Path("docs/service.md").read_text()
    assert "3.0 秒" in service and "软截止" in service
