"""真实检索、快照和 OIDC 路径的计时必须区分调用、跳过、失败与缓存复用。"""

import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from test_coldstart_snapshot import bundle
from test_search_tuning import Embedding, context, fixture

from semantic_core._timing import collect
from semantic_core.providers import ErrorCode, ProviderError, RequestContext
from semantic_core.search import SearchEngine
from semantic_service.auth import OIDCConfig, OIDCVerifier
from semantic_service.storage import SnapshotReader, StorageError


def assert_stage(stages, name, *, errors=0):
    value = stages[name]
    assert value["count"] == 1
    assert value["errors"] == errors
    assert value["duration_ms"] >= 0
    assert 0 <= value["max_ms"] <= value["duration_ms"]


def test_successful_embedding_is_timed_and_keeps_rrf_diagnostics():
    snapshot, allowed = fixture()
    embedding = Embedding()
    diagnostics = []

    with collect() as captured:
        result = SearchEngine(snapshot, embedding).search(
            "cancel", allowed, context(), diagnostics=diagnostics.append
        )

    assert len(embedding.calls) == 1
    assert result.mode == "rrf"
    assert [hit.tool.name for hit in result.hits] == ["a", "c", "b"]
    assert len(diagnostics) == 1
    assert diagnostics[0].ranking_mode == "rrf"
    assert diagnostics[0].vector_availability == "available"
    assert diagnostics[0].degraded is False
    stages = captured.snapshot()
    for name in ("search.core", "search.bm25", "model.embedding", "search.vector"):
        assert_stage(stages, name)


def test_expired_budget_returns_bm25_without_an_embedding_timer_or_call():
    snapshot, allowed = fixture()
    embedding = Embedding()
    diagnostics = []
    expired = RequestContext("expired-timing-fixture", time.monotonic() - 1)

    with collect() as captured:
        result = SearchEngine(snapshot, embedding).search(
            "cancel", allowed, expired, diagnostics=diagnostics.append
        )

    assert embedding.calls == []
    assert result.mode == "bm25"
    assert [hit.tool.name for hit in result.hits] == ["a", "b", "c"]
    assert len(diagnostics) == 1
    assert diagnostics[0].ranking_mode == "bm25"
    assert diagnostics[0].vector_availability == "unavailable"
    assert diagnostics[0].degraded is True
    stages = captured.snapshot()
    assert "model.embedding" not in stages
    assert "search.vector" not in stages
    assert_stage(stages, "search.core")
    assert_stage(stages, "search.bm25")


def test_embedding_failure_is_timed_as_an_error_and_keeps_bm25_diagnostics():
    snapshot, allowed = fixture()
    embedding = Embedding(ProviderError(ErrorCode.TIMEOUT))
    diagnostics = []

    with collect() as captured:
        result = SearchEngine(snapshot, embedding).search(
            "cancel", allowed, context(), diagnostics=diagnostics.append
        )

    assert len(embedding.calls) == 1
    assert result.mode == "bm25"
    assert [hit.tool.name for hit in result.hits] == ["a", "b", "c"]
    assert len(diagnostics) == 1
    assert diagnostics[0].ranking_mode == "bm25"
    assert diagnostics[0].vector_availability == "failed"
    assert diagnostics[0].degraded is True
    stages = captured.snapshot()
    assert_stage(stages, "model.embedding", errors=1)
    assert_stage(stages, "search.core")
    assert "search.vector" not in stages


def test_snapshot_load_stages_appear_on_cold_refresh_and_disappear_on_reuse(tmp_path):
    store, probe, control, original, _ = bundle(tmp_path)
    reader = SnapshotReader(store, clock=lambda: 100)

    with collect() as cold:
        loaded = reader.current(control)
    calls_after_load = list(probe.calls)
    with collect() as warm:
        reused = reader.current(control)

    assert loaded.tools == original.tools
    assert reused is loaded
    assert probe.calls == calls_after_load
    cold_stages = cold.snapshot()
    for name in (
        "snapshot.manifest",
        "snapshot.objects",
        "snapshot.load",
        "snapshot.refresh",
        "aws.safety",
    ):
        assert_stage(cold_stages, name)
    warm_stages = warm.snapshot()
    assert_stage(warm_stages, "snapshot.refresh")
    assert not {
        "snapshot.manifest",
        "snapshot.objects",
        "snapshot.load",
        "aws.safety",
    }.intersection(warm_stages)


def test_failed_snapshot_download_records_failure_before_load_or_safety(tmp_path):
    store, probe, control, _, _ = bundle(tmp_path)
    probe.failure_name = "vectors.npy"
    reader = SnapshotReader(store, clock=lambda: 100)

    with collect() as captured, pytest.raises(StorageError, match="object unavailable"):
        reader.current(control)

    stages = captured.snapshot()
    assert_stage(stages, "snapshot.manifest")
    assert_stage(stages, "snapshot.objects", errors=1)
    assert_stage(stages, "snapshot.refresh", errors=1)
    assert "snapshot.load" not in stages
    assert "aws.safety" not in stages
    assert probe.active == 0


def test_oidc_discovery_and_jwks_timers_follow_actual_fetches_and_cache_reuse():
    now = int(time.time())
    issuer = "https://idp.example.invalid"
    config = OIDCConfig(
        issuer,
        issuer + "/jwks",
        "offline-audience",
        frozenset({"idp.example.invalid"}),
        discovery_uri=issuer + "/discovery",
    )
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid="offline-key", alg="RS256", use="sig")
    calls = []

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def transport(request):
        calls.append(str(request.url))
        if str(request.url) == config.discovery_uri:
            return httpx.Response(200, json={"issuer": issuer, "jwks_uri": config.jwks_uri})
        assert str(request.url) == config.jwks_uri
        return httpx.Response(200, json={"keys": [jwk]})

    token = jwt.encode(
        {
            "iss": issuer,
            "sub": "offline-principal",
            "aud": config.audience,
            "iat": now,
            "exp": now + 300,
            "token_use": "access",
        },
        private,
        algorithm="RS256",
        headers={"kid": "offline-key"},
    )
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        verifier = OIDCVerifier(config, client, clock=lambda: now)
        with collect() as cold:
            principal = verifier.verify(token)
        with collect() as warm:
            cached = verifier.verify(token)

    assert principal.subject == "offline-principal"
    assert cached == principal
    assert calls == [config.discovery_uri, config.jwks_uri]
    for name in ("auth.oidc", "auth.discovery", "auth.jwks"):
        assert_stage(cold.snapshot(), name)
    assert_stage(warm.snapshot(), "auth.oidc")
    assert "auth.discovery" not in warm.snapshot()
    assert "auth.jwks" not in warm.snapshot()
