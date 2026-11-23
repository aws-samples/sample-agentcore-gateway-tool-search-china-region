"""离线验证启动工厂不加载传输助手，同时保留应用使用的低层 AWS 协议。"""

from __future__ import annotations

import ast
import base64
import inspect
import io
import json
import os
import subprocess  # nosec B404
import sys
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from zlib import crc32

import boto3
import botocore.client
import botocore.session
import pytest
from botocore.awsrequest import AWSResponse
from botocore.config import Config
from botocore.loaders import Loader
from botocore.session import Session

from semantic_core import _timing
from semantic_core._timing import collect
from semantic_core.catalog import GatewayIdentity
from semantic_service import runtime

GATEWAY = GatewayIdentity("000000000000", "cn-north-1", "offline-runtime")
ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("s3", "dynamodb", "secretsmanager", "bedrock-agentcore-control")


def offline_environment():
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"SYSTEMROOT", "WINDIR", "TMP", "TEMP", "TMPDIR"}
    }
    environment.update(  # nosec B106
        PATH=os.defpath,
        AWS_ACCESS_KEY_ID="synthetic-access",
        AWS_SECRET_ACCESS_KEY="synthetic-secret",
        AWS_SESSION_TOKEN="synthetic-token",
        AWS_EC2_METADATA_DISABLED="true",
        AWS_CONFIG_FILE=os.devnull,
        AWS_SHARED_CREDENTIALS_FILE=os.devnull,
        BOTO_CONFIG=os.devnull,
        PYTHONDONTWRITEBYTECODE="1",
    )
    return environment


@pytest.fixture
def factory(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("AWS_") or key == "BOTO_CONFIG":
            monkeypatch.delenv(key)
    for key, value in offline_environment().items():
        if key.startswith("AWS_") or key == "BOTO_CONFIG":
            monkeypatch.setenv(key, value)
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)
    monkeypatch.setattr(runtime, "_settings", lambda: SimpleNamespace(gateway=GATEWAY))
    runtime._aws.cache_clear()
    session_factory = getattr(runtime, "_aws_session", None)
    if session_factory:
        session_factory.cache_clear()
    clients = []

    def create(service):
        client = runtime._aws(service)
        if client not in clients:
            clients.append(client)
        return client

    yield create
    for client in clients:
        client.close()
    runtime._aws.cache_clear()
    if session_factory:
        session_factory.cache_clear()


def test_runtime_import_does_not_load_unused_boto3_transfer_stack():
    code = """
import json, socket, sys
def forbidden(*args, **kwargs):
    raise AssertionError("network forbidden")
socket.getaddrinfo = forbidden
socket.socket.connect = forbidden
import semantic_service.runtime
print(json.dumps([name for name in ("boto3", "s3transfer") if name in sys.modules]))
"""
    environment = offline_environment()
    environment["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(  # nosec B603
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


def test_session_and_clients_are_reused_with_named_inclusive_stages(factory, monkeypatch):
    sessions = []

    def new_session():
        session = Session()
        sessions.append(session)
        return session

    monkeypatch.setattr(runtime, "Session", new_session)
    with collect() as timing:
        clients = {service: factory(service) for service in SERVICES}
        assert all(factory(service) is client for service, client in clients.items())
    assert len(sessions) == 1
    stages = timing.snapshot()
    parent = stages["runtime.aws_client"]
    assert parent["count"] == 4
    shared = stages["runtime.aws_session"]
    assert shared["count"] == 1 and shared["errors"] == 0
    children = [stages[f"runtime.aws_client.{service.replace('-', '_')}"] for service in SERVICES]
    assert all(stage["count"] == 1 and stage["errors"] == 0 for stage in children)
    assert parent["duration_ms"] + 0.01 >= (
        shared["duration_ms"] + sum(stage["duration_ms"] for stage in children)
    )
    for client in clients.values():
        assert client.meta.region_name == GATEWAY.region
        assert client.meta.config.connect_timeout == 1
        assert client.meta.config.read_timeout == 3
        assert client.meta.config.retries == {"total_max_attempts": 1, "mode": "standard"}


@pytest.mark.parametrize("first", SERVICES)
def test_shared_sdk_work_is_timed_before_any_service_client(first, factory, monkeypatch):
    active = []
    observed = []
    index_scans = []
    original_stage = _timing.stage
    original_resolver = botocore.session.EndpointResolver
    original_list = Loader.list_available_services
    original_listdir = os.listdir

    @contextmanager
    def record_stage(name):
        active.append(name)
        try:
            with original_stage(name):
                yield
        finally:
            assert active.pop() == name

    class ObservedSession(Session):
        def get_credentials(self):
            if self._credentials is None:
                assert self.get_config_variable("region") == GATEWAY.region
                observed.append(("credentials", tuple(active)))
            return super().get_credentials()

    def create_resolver(*args, **kwargs):
        observed.append(("endpoint_resolver", tuple(active)))
        return original_resolver(*args, **kwargs)

    def list_services(loader, *args, **kwargs):
        index_scans.append([args[0] if args else kwargs["type_name"], False])
        try:
            return original_list(loader, *args, **kwargs)
        finally:
            index_scans.pop()

    def listdir(path):
        if index_scans and not index_scans[-1][1]:
            observed.append((f"service_index:{index_scans[-1][0]}", tuple(active)))
            index_scans[-1][1] = True
        return original_listdir(path)

    monkeypatch.setattr(_timing, "stage", record_stage)
    monkeypatch.setattr(runtime, "stage", record_stage)
    monkeypatch.setattr(runtime, "Session", ObservedSession)
    monkeypatch.setattr(botocore.session, "EndpointResolver", create_resolver)
    monkeypatch.setattr(Loader, "list_available_services", list_services)
    monkeypatch.setattr(os, "listdir", listdir)
    with collect() as timing:
        for service in (first, *(service for service in SERVICES if service != first)):
            factory(service)
    assert {name for name, _ in observed} == {
        "credentials",
        "endpoint_resolver",
        "service_index:service-2",
        "service_index:endpoint-rule-set-1",
    }
    assert len(observed) == 4
    assert all("runtime.aws_session" in stages for _, stages in observed), observed
    assert all(
        not any(name.startswith("runtime.aws_client.") for name in stages) for _, stages in observed
    ), observed
    assert timing.snapshot()["runtime.aws_session"]["count"] == 1


def test_aws_registry_cache_and_stage_names_cover_every_runtime_callsite(factory):
    registered = runtime._AWS_SERVICES
    assert len(registered) == len(set(registered))
    assert runtime._aws.cache_parameters()["maxsize"] == len(registered)
    calls = {
        node.args[0].value
        for node in ast.walk(ast.parse(inspect.getsource(runtime)))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_aws"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    assert calls == set(registered)
    with collect() as timing:
        clients = {service: factory(service) for service in registered}
        for _ in range(2):
            assert all(factory(service) is client for service, client in clients.items())
    expected = {f"runtime.aws_client.{service.replace('-', '_')}" for service in registered}
    stages = timing.snapshot()
    assert {name for name in stages if name.startswith("runtime.aws_client.")} == expected
    assert stages["runtime.aws_client"]["count"] == len(registered)
    assert all(stages[name]["count"] == 1 for name in expected)


def test_unknown_aws_service_is_rejected_before_session_and_not_cached(factory, monkeypatch):
    def forbidden():
        pytest.fail("unknown services must not create or warm an AWS session")

    monkeypatch.setattr(runtime, "_aws_session", forbidden)
    with collect() as timing, pytest.raises(ValueError, match="unsupported AWS service"):
        factory("ec2")
    assert runtime._aws.cache_info().currsize == 0
    assert set(timing.snapshot()) == {"runtime.aws_client"}
    assert timing.snapshot()["runtime.aws_client"]["errors"] == 1


def test_offline_subprocess_environment_excludes_unrelated_tokens(monkeypatch):
    monkeypatch.setenv("UNRELATED_API_TOKEN", "synthetic-never-forward")
    assert "UNRELATED_API_TOKEN" not in offline_environment()


@pytest.mark.parametrize("provider", ["assume-role", "assume-role-with-web-identity"])
def test_shared_warmup_preserves_deferred_credentials_and_nested_sts_region(
    provider, factory, monkeypatch, tmp_path
):
    token_file = tmp_path / "synthetic-web-token"
    token_file.write_text("synthetic-token")
    profile = {
        "region": "us-east-1",
        "role_arn": "arn:aws-cn:iam::000000000000:role/offline",
        "role_session_name": "offline-session",
    }
    if provider == "assume-role":
        profile["credential_source"] = "Environment"
    else:
        profile["web_identity_token_file"] = str(token_file)
    clients = []
    nested_clients = []
    operations = []

    class ProfileSession(Session):
        def __init__(self):
            super().__init__(profile="offline")

        @property
        def full_config(self):
            return {"profiles": {"offline": profile}}

        def create_client(self, service, **kwargs):
            client = super().create_client(service, **kwargs)
            clients.append(client)
            if service == "sts":
                nested_clients.append((client.meta.region_name, client.meta.endpoint_url))
            return client

    def fake_sts_call(client, operation, parameters):
        assert client.meta.service_model.service_name == "sts"
        assert operation in {"AssumeRole", "AssumeRoleWithWebIdentity"}
        operations.append(operation)
        return {
            "Credentials": {
                "AccessKeyId": "synthetic-role-access",
                "SecretAccessKey": "synthetic-role-secret",
                "SessionToken": "synthetic-role-token",
                "Expiration": datetime.now(UTC) + timedelta(hours=1),
            }
        }

    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", fake_sts_call)
    monkeypatch.setattr(runtime, "Session", ProfileSession)
    try:
        reference = ProfileSession()
        reference.create_client("s3", region_name=GATEWAY.region)
        assert operations == []
        reference_credentials = reference.get_credentials()
        assert reference_credentials.method == provider
        expected = reference_credentials.get_frozen_credentials()
        expected_nested = nested_clients[:]
        assert expected_nested == [
            (GATEWAY.region, f"https://sts.{GATEWAY.region}.amazonaws.com.cn")
        ]
        nested_clients.clear()
        operations.clear()
        factory("s3")
        assert operations == []
        current_credentials = runtime._aws_session().get_credentials()
        assert current_credentials.method == provider
        assert current_credentials.get_frozen_credentials() == expected
        assert nested_clients == expected_nested
        assert len(operations) == 1
    finally:
        for client in clients:
            client.close()


def test_failed_client_creation_is_not_cached(factory, monkeypatch):
    session = runtime._aws_session()
    original = session.create_client
    calls = []

    def sometimes_fails(service, **kwargs):
        calls.append(service)
        if len(calls) == 1:
            raise RuntimeError("synthetic client setup failure")
        return original(service, **kwargs)

    monkeypatch.setattr(session, "create_client", sometimes_fails)
    with collect() as timing:
        with pytest.raises(RuntimeError, match="synthetic client setup"):
            factory("s3")
        client = factory("s3")
        assert factory("s3") is client
    assert calls == ["s3", "s3"]
    stages = timing.snapshot()
    assert stages["runtime.aws_client"]["errors"] == 1
    assert stages["runtime.aws_client.s3"]["errors"] == 1
    assert stages["runtime.aws_client.s3"]["count"] == 2


def test_failed_session_initialization_is_not_cached(factory, monkeypatch):
    attempts = 0

    def new_session():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("synthetic session setup failure")
        return Session()

    monkeypatch.setattr(runtime, "Session", new_session)
    with collect() as timing:
        with pytest.raises(RuntimeError, match="synthetic session setup"):
            factory("s3")
        client = factory("s3")
        assert factory("s3") is client
    assert attempts == 2
    stages = timing.snapshot()
    assert stages["runtime.aws_session"]["count"] == 2
    assert stages["runtime.aws_session"]["errors"] == 1
    assert stages["runtime.aws_client.s3"]["count"] == 1
    assert stages["runtime.aws_client.s3"]["errors"] == 0


def test_failed_shared_warmup_retries_with_a_new_session(factory, monkeypatch):
    sessions = []

    class SometimesFailsSession(Session):
        def __init__(self):
            super().__init__()
            sessions.append(self)

        def get_available_partitions(self):
            if len(sessions) == 1:
                raise RuntimeError("synthetic shared endpoint setup failure")
            return super().get_available_partitions()

    monkeypatch.setattr(runtime, "Session", SometimesFailsSession)
    with collect() as timing:
        with pytest.raises(RuntimeError, match="synthetic shared endpoint"):
            factory("s3")
        client = factory("s3")
        assert factory("s3") is client
    assert len(sessions) == 2
    stages = timing.snapshot()
    assert stages["runtime.aws_session"]["count"] == 2
    assert stages["runtime.aws_session"]["errors"] == 1
    assert stages["runtime.aws_client.s3"]["count"] == 1
    assert stages["runtime.aws_client.s3"]["errors"] == 0


def wire_call(client, operation, parameters, monkeypatch):
    observed = []

    def respond(request, **kwargs):
        headers = {
            key.lower(): value.decode() if isinstance(value, bytes) else value
            for key, value in request.headers.items()
        }
        assert "AWS4-HMAC-SHA256" in headers["authorization"]
        assert (
            f"/{GATEWAY.region}/{client.meta.service_model.signing_name}/aws4_request"
            in (headers["authorization"])
        )
        assert headers["x-amz-security-token"] == "synthetic-token"
        signed_headers = headers["authorization"].split("SignedHeaders=")[1].split(",")[0]
        body = request.body.read() if hasattr(request.body, "read") else request.body
        observed.append(
            {
                "method": request.method,
                "url": request.url,
                "body": body,
                "signed_headers": signed_headers,
                "headers": {
                    key: value
                    for key, value in headers.items()
                    if key
                    not in {"authorization", "x-amz-date", "user-agent", "amz-sdk-invocation-id"}
                },
            }
        )
        contents = (
            b"payload" if operation == "get_object" else b"" if operation == "put_object" else b"{}"
        )

        class Raw:
            def __init__(self):
                self.stream_bytes = io.BytesIO(contents)

            def read(self, size=-1):
                return self.stream_bytes.read(size)

            def stream(self, *args, **kwargs):
                yield contents

            def close(self):
                self.stream_bytes.close()

        return AWSResponse(
            request.url,
            200,
            {"content-length": str(len(contents)), "x-amz-version-id": "immutable-version"},
            Raw(),
        )

    def forbidden(*args, **kwargs):
        pytest.fail("real AWS transport must never run")

    monkeypatch.setattr(client._endpoint.http_session, "send", forbidden)
    client.meta.events.register("before-send", respond)
    response = getattr(client, operation)(**parameters)
    if operation == "get_object":
        assert response["VersionId"] == "immutable-version"
        assert response["Body"].read() == b"payload"
        response["Body"].close()
    assert len(observed) == 1
    return observed[0]


@pytest.mark.parametrize(
    "service,operation,parameters",
    [
        ("s3", "get_object", {"Bucket": "offline-bucket", "Key": "a/b", "VersionId": "v+1/="}),
        (
            "s3",
            "put_object",
            {
                "Bucket": "offline-bucket",
                "Key": "snapshot",
                "Body": b"payload",
                "ChecksumAlgorithm": "CRC32",
            },
        ),
        (
            "dynamodb",
            "get_item",
            {"TableName": "offline-state", "Key": {"pk": {"S": "value"}}, "ConsistentRead": True},
        ),
        (
            "dynamodb",
            "put_item",
            {"TableName": "offline-state", "Item": {"pk": {"S": "value"}, "revision": {"N": "1"}}},
        ),
        (
            "dynamodb",
            "transact_write_items",
            {
                "TransactItems": [
                    {"Put": {"TableName": "offline-state", "Item": {"pk": {"S": "value"}}}}
                ],
                "ClientRequestToken": "offline-idempotency",
            },
        ),
        (
            "dynamodb",
            "update_item",
            {
                "TableName": "offline-state",
                "Key": {"pk": {"S": "value"}},
                "UpdateExpression": "ADD calls :one",
                "ExpressionAttributeValues": {":one": {"N": "1"}},
            },
        ),
        (
            "secretsmanager",
            "get_secret_value",
            {"SecretId": "offline", "VersionStage": "AWSCURRENT"},
        ),
    ],
)
def test_used_low_level_requests_match_boto3(factory, monkeypatch, service, operation, parameters):
    client = factory(service)
    with closing(
        boto3.session.Session().client(
            service,
            region_name=GATEWAY.region,
            config=Config(
                connect_timeout=1,
                read_timeout=3,
                retries={"total_max_attempts": 1, "mode": "standard"},
            ),
        )
    ) as reference:
        expected = wire_call(reference, operation, parameters, monkeypatch)
    actual = wire_call(client, operation, parameters, monkeypatch)
    assert actual == expected
    if operation == "get_object":
        assert parse_qs(urlsplit(actual["url"]).query)["versionId"] == ["v+1/="]
    if operation == "put_object":
        checksum = base64.b64encode(crc32(b"payload").to_bytes(4, "big"))
        assert b"x-amz-checksum-crc32:" + checksum in actual["body"]
        assert "x-amz-trailer" in actual["signed_headers"]
