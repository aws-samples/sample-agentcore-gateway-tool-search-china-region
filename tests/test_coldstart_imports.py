"""在独立进程验证 REQUEST 不加载检索依赖，避免测试集合提前导入造成假阳性。"""

from __future__ import annotations

import os
import subprocess  # nosec B404
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_CHILD_GUARD = """
import json
import os
import socket
import sys
import tempfile

network_attempts = tempfile.NamedTemporaryFile(mode="a+b")

def blocked(*args, **kwargs):
    # Persist before raising so optional prefetch cannot hide a swallowed error.
    os.write(network_attempts.fileno(), b"x")
    raise AssertionError("本地导入回归测试禁止网络访问")

socket.getaddrinfo = blocked
socket.socket.connect = blocked
socket.socket.connect_ex = blocked

FORBIDDEN = (
    "numpy",
    "jieba",
    "semantic_core.bm25",
    "semantic_core.providers",
    "semantic_core.search",
    "semantic_core.snapshot",
    "semantic_core.vectors",
    "semantic_service.catalog",
    "semantic_service.enhancer",
    "semantic_service.indexer",
    "semantic_service.models",
    "semantic_service.sdk",
    "semantic_service.search",
)

def check_imports(stage):
    loaded = [
        prefix for prefix in FORBIDDEN
        if any(name == prefix or name.startswith(prefix + ".") for name in sys.modules)
    ]
    print(json.dumps({"阶段": stage, "不应加载的模块": loaded}, ensure_ascii=False))
    assert not loaded, "REQUEST 导入了无关检索模块：" + ", ".join(loaded)
"""


def run_child(body: str) -> None:
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
    }
    result = subprocess.run(  # nosec B603
        [
            sys.executable,
            "-c",
            _CHILD_GUARD
            + "\ntry:\n"
            + textwrap.indent(textwrap.dedent(body), "    ")
            + "\nfinally:\n"
            + "    assert os.fstat(network_attempts.fileno()).st_size == 0, "
            + '"REQUEST attempted network access during import"\n',
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "entrypoint", ("semantic_service.runtime", "semantic_service.request_runtime")
)
def test_lambda_runtime_import_does_not_load_retrieval_dependencies(entrypoint: str) -> None:
    run_child(
        f"""
        import importlib
        entry = importlib.import_module({entrypoint!r})
        from semantic_service import runtime

        assert runtime.__file__.startswith(str(__import__("pathlib").Path.cwd() / "src"))
        assert callable(entry.request_handler)
        assert all(callable(getattr(runtime, name)) for name in (
            "request_handler", "search_handler", "indexer_handler", "enhancer_handler"
        ))
        check_imports("共享 Lambda 入口导入")
        """
    )


def run_factory_and_authorized_call(
    entrypoint: str,
    initialization_type: str,
    *,
    stub_http: bool = True,
    prefetch_probe: bool = False,
) -> None:
    run_child(
        f"ENTRYPOINT = {entrypoint!r}\nINITIALIZATION_TYPE = {initialization_type!r}\n"
        f"STUB_HTTP = {stub_http!r}\nPREFETCH_PROBE = {prefetch_probe!r}\n"
        + textwrap.dedent(
            """
        import base64
        import importlib
        import os
        import time
        from contextlib import ExitStack, redirect_stdout
        from types import SimpleNamespace
        from unittest.mock import patch
        import httpx
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        from semantic_service import _request_prefetch, runtime
        from semantic_service.auth import AuthorizationView, Principal

        descriptor = {
            "name": "discovery___search_tools",
            "description": "Discover authorized tools",
            "inputSchema": {"type": "object"},
        }
        os.environ.update({
            "SEMSEARCH_ACCOUNT": "000000000000",
            "SEMSEARCH_REGION": "cn-north-1",
            "SEMSEARCH_GATEWAY_ID": "gateway-fixture",
            "SEMSEARCH_SEARCH_TARGET_ID": "search-target",
            "SEMSEARCH_SEARCH_TOOL_NAME": descriptor["name"],
            "SEMSEARCH_SEARCH_INCARNATION": "fixture-incarnation",
            "SEMSEARCH_SEARCH_DESCRIPTOR_JSON": json.dumps(descriptor),
            "SEMSEARCH_BUCKET": "fixture-bucket",
            "SEMSEARCH_TABLE": "fixture-state",
            "SEMSEARCH_PROTECTED_TABLE": "fixture-protected",
            "SEMSEARCH_MACHINE_PRINCIPALS_JSON": "[]",
            "OIDC_ISSUER": "https://idp.example.invalid",
            "OIDC_JWKS_URI": "https://idp.example.invalid/jwks",
            "OIDC_DISCOVERY_URI": "https://idp.example.invalid/discovery",
            "OIDC_AUDIENCE": "fixture-audience",
            "OIDC_ALLOWED_HOSTS": "idp.example.invalid",
        })
        secret_calls = []
        clients = []
        now = int(time.time())

        class FakeAWS:
            def get_secret_value(self, **kwargs):
                secret_calls.append(kwargs)
                return {
                    "VersionId": "fixture-signing-version",
                    "SecretString": json.dumps({
                        "key_b64": base64.b64encode(b"fixture-signing-material-32-bytes").decode(),
                        "activated_at": now - 10,
                    }),
                }

        def aws(service):
            clients.append(service)
            return FakeAWS()

        runtime._aws = aws
        key = jwt.algorithms.RSAAlgorithm.to_jwk(
            rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key(),
            as_dict=True,
        )
        key.update(kid="fixture-key", alg="RS256", use="sig")
        def metadata_response(request):
            if PREFETCH_PROBE:
                try:
                    socket.getaddrinfo("idp.example.invalid", 443)
                except AssertionError:
                    pass
            if request.url.path == "/discovery":
                return httpx.Response(200, json={
                    "issuer": "https://idp.example.invalid",
                    "jwks_uri": "https://idp.example.invalid/jwks",
                })
            assert request.url.path == "/jwks"
            return httpx.Response(200, json={"keys": [key]})

        client = httpx.Client(transport=httpx.MockTransport(metadata_response), trust_env=False)
        if STUB_HTTP:
            runtime._http = lambda: client
        os.environ.update({
            "AWS_LAMBDA_FUNCTION_NAME": "fixture-request",
            "_HANDLER": "semantic_service.request_runtime.request_handler",
            "AWS_LAMBDA_INITIALIZATION_TYPE": INITIALIZATION_TYPE,
        })
        with ExitStack() as stack:
            init_output = stack.enter_context(
                tempfile.TemporaryFile(mode="a+", encoding="utf-8")
            )
            metadata_transports = []
            def prefetch_response(transport, request):
                metadata_transports.append((os.getpid(), id(transport)))
                return metadata_response(request)
            if STUB_HTTP:
                stack.enter_context(patch.object(
                    httpx.HTTPTransport, "handle_request", prefetch_response
                ))
            original_prefetch = _request_prefetch.prefetch
            def prefetch(verifier, **kwargs):
                return original_prefetch(verifier, **{**kwargs, "log_fd": init_output.fileno()})
            stack.enter_context(patch.object(_request_prefetch, "prefetch", prefetch))
            with redirect_stdout(init_output):
                entry = importlib.import_module(ENTRYPOINT)
            init_output.flush()
            init_output.seek(0)
            init_text = init_output.read()
        print(init_text, end="")
        if (
            ENTRYPOINT.endswith("request_runtime")
            and INITIALIZATION_TYPE == "provisioned-concurrency"
        ):
            assert runtime._request.cache_info().currsize == 1
            init_rows = [json.loads(line) for line in init_text.splitlines()]
            assert init_rows[-1]["oidc_prefetch"] == {"status": "ready"}
            assert init_rows[-1]["stage_timings"]["auth.discovery"]["count"] == 1
            assert init_rows[-1]["stage_timings"]["auth.jwks"]["count"] == 1
            assert len(metadata_transports) == 2
            assert {pid for pid, _ in metadata_transports} == {os.getpid()}
            assert len({transport for _, transport in metadata_transports}) == 1
            assert id(client._transport) not in {transport for _, transport in metadata_transports}
        else:
            assert init_text == ""
            assert metadata_transports == []
        interceptor = runtime._request()
        check_imports("REQUEST 工厂完成")
        search = interceptor.config.search_tool
        principal = Principal(
            "https://idp.example.invalid", "fixture-user", frozenset(), frozenset(),
            now, now + 300,
        )
        authorization_reads = []
        limiter_calls = []

        def authorize(actual_principal, gateway):
            authorization_reads.append((actual_principal, gateway))
            return AuthorizationView(
                control=SimpleNamespace(policy_epoch=1, tombstoned=False),
                allowed_ids=frozenset({search.tool_id}),
                tools_by_name={search.name: search.identity},
                schema_digests={search.tool_id: search.schema_digest},
                query_egress_allowed=False,
            )

        interceptor.verifier.verify = lambda token: principal
        interceptor.authorizer.read = authorize
        interceptor.limiter.admit = lambda *args, **kwargs: limiter_calls.append((args, kwargs))
        event = {
            "interceptorInputVersion": "1.0",
            "mcp": {"gatewayRequest": {
                "path": "/mcp", "httpMethod": "POST",
                "headers": {"Authorization": "Bearer synthetic-token"},
                "body": {
                    "jsonrpc": "2.0", "id": "fixture-request", "method": "tools/call",
                    "params": {"name": search.name, "arguments": {"query": "查找订单"}},
                },
            }},
        }
        output = entry.request_handler(event, SimpleNamespace())
        body = output["mcp"]["transformedGatewayRequest"]["body"]
        assert body["id"] == "fixture-request"
        assert body["params"]["arguments"]["query"] == "查找订单"
        assert isinstance(body["params"]["arguments"]["_auth_context"], str)
        assert len(authorization_reads) == len(limiter_calls) == len(secret_calls) == 1
        assert set(clients) == {"s3", "dynamodb", "secretsmanager"}
        check_imports("REQUEST 授权、限流与签名完成")
        client.close()
        """
        )
    )


@pytest.mark.parametrize(
    ("entrypoint", "initialization_type"),
    [
        ("semantic_service.runtime", "on-demand"),
        ("semantic_service.request_runtime", "on-demand"),
        ("semantic_service.request_runtime", "provisioned-concurrency"),
    ],
)
def test_request_factory_and_authorized_call_do_not_load_retrieval_dependencies(
    entrypoint: str, initialization_type: str
) -> None:
    run_factory_and_authorized_call(entrypoint, initialization_type)


def test_import_network_guard_detects_swallowed_attempt() -> None:
    with pytest.raises(AssertionError, match="REQUEST attempted network access during import"):
        run_child(
            "try:\n"
            "    socket.getaddrinfo('idp.example.invalid', 443)\n"
            "except AssertionError:\n"
            "    pass\n"
        )


def test_import_network_guard_detects_missing_pc_http_stub() -> None:
    with pytest.raises(AssertionError, match="REQUEST attempted network access during import"):
        run_factory_and_authorized_call(
            "semantic_service.request_runtime", "provisioned-concurrency", stub_http=False
        )


def test_import_network_guard_detects_swallowed_prefetch_attempt() -> None:
    with pytest.raises(AssertionError, match="REQUEST attempted network access during import"):
        run_factory_and_authorized_call(
            "semantic_service.request_runtime", "provisioned-concurrency", prefetch_probe=True
        )
