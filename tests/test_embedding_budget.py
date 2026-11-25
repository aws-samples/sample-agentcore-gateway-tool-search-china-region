"""配置模型预算贯通检索与 HTTP，收尾预留不替代最终授权。"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from service.test_identity import (
    BUSINESS,
    GATEWAY,
    SEARCH,
    Authorizer,
    codec,
    context,
    event,
    view,
)

from semantic_core.providers import ModelProfile, RerankItem, RerankResult
from semantic_core.snapshot import IndexSnapshot
from semantic_core.vectors import VectorIndex
from semantic_service import config, runtime
from semantic_service.auth import AuthenticationError, InputError, ServiceError
from semantic_service.deployment_config import parse_configuration
from semantic_service.models import ModelSettings, RotatingModelProviders
from semantic_service.search import SearchHandler
from semantic_service.secrets import SecretsCache

TOTAL_FIELD = "SEMSEARCH_SEARCH_TIMEOUT_SECONDS"
EMBEDDING_FIELD = "MODEL_EMBEDDING_TIMEOUT_SECONDS"


def model_values(seconds="0.45"):
    values = {
        "MODEL_ALLOWED_HOSTS": "budget-model.invalid",
        "MODEL_CHAT_MODEL": "example-chat-model",
        "MODEL_RERANK_MODEL": "example-rerank-model",
        "MODEL_EMBEDDING_MODEL": "budget-embedding",
        "MODEL_EMBEDDING_REVISION": "budget-v1",
        "MODEL_EMBEDDING_DIMENSIONS": "2",
    }
    if seconds is not None:
        values[EMBEDDING_FIELD] = seconds
    return values


@contextmanager
def budget_service(
    monkeypatch,
    *,
    embedding_seconds="0.45",
    request_seconds=1.5,
    pre_seconds=0,
    secret_seconds=0,
    response_seconds=(0,),
    statuses=(200,),
    authorizer=None,
    reranker=None,
):
    clock = [100.0]
    monkeypatch.setattr("semantic_service.search.time.monotonic", lambda: clock[0])
    values = model_values(embedding_seconds)
    settings = ModelSettings.from_env(values)
    snapshot = IndexSnapshot.build(
        GATEWAY,
        (BUSINESS,),
        vectors=VectorIndex([[1, 0]], [0], 1, 2),
        model_profile=settings.embedding.profile,
    )
    requests = []
    secret_calls = []

    class Secrets:
        def get_secret_value(self, **kwargs):
            secret_calls.append(kwargs)
            clock[0] += secret_seconds
            return {
                "VersionId": f"fixture-{len(secret_calls)}",
                "SecretString": json.dumps(
                    {"base_url": "https://budget-model.invalid/v1", "api_key": "synthetic"}
                ),
            }

    class Reader:
        def current(self, control):
            clock[0] += pre_seconds
            return snapshot

    def respond(request):
        index = len(requests)
        requests.append(request)
        clock[0] += response_seconds[min(index, len(response_seconds) - 1)]
        status = statuses[min(index, len(statuses) - 1)]
        return httpx.Response(
            status,
            json={"data": [{"index": 0, "embedding": [1, 0]}]},
        )

    with httpx.Client(transport=httpx.MockTransport(respond), trust_env=False) as client:
        models = RotatingModelProviders(
            SecretsCache(Secrets(), clock=lambda: clock[0]), settings, client
        )
        authorization = authorizer or Authorizer()
        options = {} if request_seconds is None else {"request_seconds": request_seconds}
        handler = SearchHandler(
            SEARCH,
            codec(),
            authorization,
            Reader(),
            models.embedding,
            reranker,
            config=replace(config.parse_search_config(values), rerank_enabled=reranker is not None),
            **options,
        )
        yield SimpleNamespace(
            handler=handler,
            requests=requests,
            clock=clock,
            secret_calls=secret_calls,
            authorizer=authorization,
        )


def read_timeout(request):
    timeouts = request.extensions["timeout"]
    assert set(timeouts) == {"connect", "read", "write", "pool"}
    assert len(set(timeouts.values())) == 1
    return timeouts["read"]


@pytest.mark.parametrize("seconds", ["0.45", "0.8", "1.0"])
def test_one_embedding_setting_controls_core_and_http_limits(seconds):
    values = model_values(seconds)
    assert config.parse_search_config(values).embedding_timeout == float(seconds)
    assert ModelSettings.from_env(values).embedding_timeout_seconds == float(seconds)


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf", "", "true", None, True, 0.8])
def test_invalid_embedding_budget_is_rejected_before_model_setup(value):
    with pytest.raises(ValueError, match=EMBEDDING_FIELD):
        config.parse_search_config({EMBEDDING_FIELD: value})


@pytest.mark.parametrize("seconds", [None, "0.301", "1.5", "2.5", "3.0", "6"])
def test_total_budget_defaults_and_boundaries(seconds):
    values = {} if seconds is None else {TOTAL_FIELD: seconds}
    expected = 3.0 if seconds is None else float(seconds)
    assert config.parse_search_request_seconds(values) == expected


@pytest.mark.parametrize(
    "value", ["0", "-1", "0.3", "6.001", "nan", "inf", "-inf", "", "true", None, True, 2.5]
)
def test_invalid_total_budget_is_rejected_by_shared_search_parser(value):
    with pytest.raises(ValueError, match=TOTAL_FIELD):
        config.parse_search_config({TOTAL_FIELD: value})


def test_pinned_configuration_accepts_both_budgets_without_changing_document_version():
    values = model_values("0.8") | {TOTAL_FIELD: "2.5"}
    data = json.dumps(
        {"version": 1, "gateway": GATEWAY.as_dict(), "operation": "search", "settings": values}
    ).encode()
    parsed = parse_configuration(data, GATEWAY, "search")
    assert config.parse_search_config(parsed).embedding_timeout == 0.8
    assert config.parse_search_request_seconds(parsed) == 2.5
    assert ModelSettings.from_env(parsed).embedding_timeout_seconds == 0.8


@pytest.mark.parametrize("models_enabled", [True, False])
@pytest.mark.parametrize(
    ("field", "value"), [(TOTAL_FIELD, "0.3"), (TOTAL_FIELD, "inf"), (EMBEDDING_FIELD, "nan")]
)
def test_cdk_rejects_invalid_budgets_even_when_models_are_disabled(models_enabled, field, value):
    from infra.config import DeploymentConfig

    deployment = DeploymentConfig.from_file(Path("infra/config.example.json"))
    category = "model_settings" if field == EMBEDDING_FIELD else "service_settings"
    with pytest.raises(ValueError, match=field):
        replace(
            deployment,
            models_enabled=models_enabled,
            **{category: getattr(deployment, category) | {field: value}},
        )


def test_explicit_previous_450ms_budget_keeps_final_authorization(monkeypatch):
    with budget_service(monkeypatch) as service:
        observed = []
        assert service.handler(event(), context(), diagnostics=observed.append) == {
            "tools": [BUSINESS.descriptor]
        }
        assert read_timeout(service.requests[0]) == pytest.approx(0.45)
        assert observed[0].degraded is False
        assert service.authorizer.calls == 5


@pytest.mark.parametrize(("seconds", "degraded"), [("0.45", True), ("0.8", False), ("1.0", False)])
def test_cold_front_work_and_model_response_share_the_configured_total(
    monkeypatch, seconds, degraded
):
    with budget_service(
        monkeypatch,
        embedding_seconds=seconds,
        request_seconds=2.5,
        pre_seconds=1.3,
        response_seconds=(0.7,),
    ) as service:
        observed = []
        assert service.handler(event(), context(), diagnostics=observed.append) == {
            "tools": [BUSINESS.descriptor]
        }
        assert observed[0].degraded is degraded
        assert read_timeout(service.requests[0]) == pytest.approx(float(seconds))
        assert service.authorizer.calls == 5


def test_application_budget_is_not_reduced_by_lambda_finalization_reserve(monkeypatch):
    with budget_service(monkeypatch, embedding_seconds="0.8", pre_seconds=0.9) as service:
        service.handler(event(), context())
        assert read_timeout(service.requests[0]) == pytest.approx(0.6)


def test_exhausted_provider_budget_skips_models_but_not_final_authorization(monkeypatch):
    authorization = Authorizer([view(), view(), view(), view(), view(allowed=[SEARCH.tool_id])])
    with budget_service(monkeypatch, pre_seconds=1.51, authorizer=authorization) as service:
        observed = []
        assert service.handler(event(), context(), diagnostics=observed.append) == {"tools": []}
        assert service.requests == []
        assert service.secret_calls == []
        assert observed[0].vector_availability == "unavailable"
        assert authorization.calls == 5


@pytest.mark.parametrize(("remaining_ms", "expected"), [(700, 0.4), (200, None), (0, None)])
def test_lambda_remaining_time_caps_provider_work(monkeypatch, remaining_ms, expected):
    with budget_service(monkeypatch, embedding_seconds="0.8", request_seconds=2.5) as service:
        ctx = context()
        ctx.get_remaining_time_in_millis = lambda: remaining_ms
        assert service.handler(event(), ctx) == {"tools": [BUSINESS.descriptor]}
        if expected is None:
            assert service.requests == []
        else:
            assert read_timeout(service.requests[0]) == pytest.approx(expected)
        assert service.authorizer.calls == 5


@pytest.mark.parametrize(
    "remaining_ms",
    [float("nan"), float("inf"), float("-inf"), True, "private-value-marker", -1, None],
)
def test_invalid_lambda_remaining_time_uses_application_budget(monkeypatch, remaining_ms):
    with budget_service(monkeypatch, pre_seconds=1.0) as service:
        ctx = context()
        ctx.get_remaining_time_in_millis = lambda: remaining_ms
        observed = []
        assert service.handler(event(), ctx, diagnostics=observed.append) == {
            "tools": [BUSINESS.descriptor]
        }
        assert read_timeout(service.requests[0]) == pytest.approx(0.45)
        assert observed[0].lambda_deadline_ignored is True
        assert "private-value-marker" not in json.dumps(asdict(observed[0]))


def test_cached_handler_recomputes_lambda_deadline_for_each_invocation(monkeypatch):
    with budget_service(monkeypatch, embedding_seconds="0.8", request_seconds=2.5) as service:
        remaining = [700]
        ctx = context()
        ctx.get_remaining_time_in_millis = lambda: remaining[0]
        service.handler(event(), ctx)
        remaining[0] = 1700
        service.handler(event(), ctx)
        assert [read_timeout(request) for request in service.requests] == pytest.approx([0.4, 0.8])


def test_secret_fetch_is_charged_to_the_embedding_stage(monkeypatch):
    with budget_service(monkeypatch, embedding_seconds="0.8", secret_seconds=0.12) as service:
        service.handler(event(), context())
        assert read_timeout(service.requests[0]) == pytest.approx(0.68)


def test_auth_retry_cannot_restart_the_embedding_budget(monkeypatch):
    with budget_service(
        monkeypatch,
        embedding_seconds="0.8",
        statuses=(401, 200),
        response_seconds=(0.35, 0.1),
    ) as service:
        observed = []
        service.handler(event(), context(), diagnostics=observed.append)
        assert [read_timeout(request) for request in service.requests] == pytest.approx([0.8, 0.45])
        assert observed[0].degraded is False
        assert len(service.secret_calls) == 2


def test_rerank_shares_provider_deadline_and_cannot_use_finalization_reserve(monkeypatch):
    remaining = []
    profile = ModelProfile("budget-rerank", "v1")

    class Reranker:
        def describe(self):
            return profile

        def rerank(self, query, documents, top_n, context):
            remaining.append(context.remaining())
            return RerankResult(
                tuple(RerankItem(document.id, 1.0) for document in documents), profile
            )

    with budget_service(
        monkeypatch,
        embedding_seconds="0.8",
        pre_seconds=0.2,
        response_seconds=(0.7,),
        reranker=Reranker(),
    ) as service:
        observed = []
        ctx = context()
        ctx.get_remaining_time_in_millis = lambda: 1500
        service.handler(event(), ctx, diagnostics=observed.append)
        assert remaining == pytest.approx([0.3])
        assert observed[0].degraded is False
        assert service.authorizer.calls == 5


@pytest.mark.parametrize(
    "seconds", [0, -1, 0.1, 0.3, 6.001, float("nan"), float("inf"), True, "1.5"]
)
def test_direct_request_seconds_uses_the_same_bounds_as_configuration(monkeypatch, seconds):
    with pytest.raises(ValueError, match="search deadline"):
        with budget_service(monkeypatch, request_seconds=seconds):
            pytest.fail("invalid direct budget was accepted")


@pytest.mark.parametrize("seconds", [0.301, 1.5, 6.0])
def test_direct_request_seconds_accepts_valid_boundaries(monkeypatch, seconds):
    with budget_service(monkeypatch, request_seconds=seconds) as service:
        observed = []
        assert service.handler(event(), context(), diagnostics=observed.append) == {
            "tools": [BUSINESS.descriptor]
        }
        assert read_timeout(service.requests[0]) == pytest.approx(min(seconds, 0.45))
        assert observed[0].lambda_deadline_ignored is False


@pytest.mark.parametrize(("seconds", "column"), [("0.45", 0), ("0.8", 1), ("1.0", 2)])
@pytest.mark.parametrize(
    ("pre_seconds", "expected"),
    [
        (0, (0.45, 0.8, 1.0)),
        (0.05, (0.45, 0.8, 1.0)),
        (0.3, (0.45, 0.8, 1.0)),
        (0.6, (0.45, 0.8, 0.9)),
        (0.75, (0.45, 0.75, 0.75)),
        (0.9, (0.45, 0.6, 0.6)),
        (1.0, (0.45, 0.5, 0.5)),
        (1.2, (0.3, 0.3, 0.3)),
        (1.4, (0.1, 0.1, 0.1)),
    ],
)
def test_review_front_work_matrix_retains_application_budget(
    monkeypatch, seconds, column, pre_seconds, expected
):
    with budget_service(monkeypatch, embedding_seconds=seconds, pre_seconds=pre_seconds) as service:
        observed = []
        service.handler(event(), context(), diagnostics=observed.append)
        assert read_timeout(service.requests[0]) == pytest.approx(expected[column])
        assert observed[0].lambda_deadline_ignored is False


@pytest.mark.parametrize("remaining_ms", [1500, 10000])
def test_only_the_lambda_branch_reserves_authorization_time(monkeypatch, remaining_ms):
    with budget_service(monkeypatch, embedding_seconds="1.0", pre_seconds=0.9) as service:
        ctx = context()
        ctx.get_remaining_time_in_millis = lambda: remaining_ms
        observed = []
        service.handler(event(), ctx, diagnostics=observed.append)
        assert read_timeout(service.requests[0]) == pytest.approx(
            0.3 if remaining_ms == 1500 else 0.6
        )
        assert observed[0].lambda_deadline_ignored is False


def broken_lambda_context(kind):
    original = context()

    def failure():
        raise RuntimeError("private-lambda-error-marker")

    if kind == "not_callable":
        original.get_remaining_time_in_millis = "private-lambda-value-marker"
    elif kind == "method":
        original.get_remaining_time_in_millis = failure
    else:

        class BrokenContext:
            client_context = original.client_context

            @property
            def get_remaining_time_in_millis(self):
                if kind == "attribute_error":
                    raise AttributeError("private-lambda-error-marker")
                failure()

        return BrokenContext()
    return original


@pytest.mark.parametrize("kind", ["not_callable", "method", "property", "attribute_error"])
def test_lambda_attribute_or_method_failure_is_bounded_diagnostic_only(monkeypatch, kind):
    with budget_service(monkeypatch, pre_seconds=1.0) as service:
        observed = []
        assert service.handler(
            event(), broken_lambda_context(kind), diagnostics=observed.append
        ) == {"tools": [BUSINESS.descriptor]}
        assert read_timeout(service.requests[0]) == pytest.approx(0.45)
        assert observed[0].lambda_deadline_ignored is True
        assert "private-lambda-" not in json.dumps(asdict(observed[0]))


def search_log(capsys):
    captured = capsys.readouterr()
    assert "private-lambda-" not in captured.out + captured.err
    rows = [json.loads(line) for line in captured.out.splitlines() if line.startswith("{")]
    return next(row for row in rows if row.get("operation") == "search")


@pytest.mark.parametrize("failure", ["input", "authentication"])
def test_bad_lambda_context_does_not_override_input_or_authentication_error(
    monkeypatch, capsys, failure
):
    with budget_service(monkeypatch) as service:
        monkeypatch.setattr(runtime, "_search", lambda: service.handler)
        invalid = {"query": ""} if failure == "input" else event() | {"_auth_context": "invalid"}
        error = InputError if failure == "input" else AuthenticationError
        with pytest.raises(error):
            runtime.search_handler(invalid, broken_lambda_context("property"))
        record = search_log(capsys)
        assert record["lambda_deadline_ignored"] is True
        assert record["embedding_timing"] is None
        assert service.requests == []


def test_lambda_diagnostic_is_request_local_even_after_success_and_rejection(monkeypatch, capsys):
    with budget_service(monkeypatch) as service:
        monkeypatch.setattr(runtime, "_search", lambda: service.handler)
        runtime.search_handler(event(), broken_lambda_context("method"))
        first = search_log(capsys)
        assert first["lambda_deadline_ignored"] is True
        assert first["embedding_timing"] is not None
        with pytest.raises(InputError):
            runtime.search_handler({"query": ""}, context())
        second = search_log(capsys)
        assert second["lambda_deadline_ignored"] is False
        assert second["embedding_timing"] is None


def test_lambda_diagnostic_defaults_false_when_runtime_factory_fails(monkeypatch, capsys):
    def broken():
        raise ValueError("private-lambda-error-marker")

    monkeypatch.setattr(runtime, "_search", broken)
    with pytest.raises(ServiceError):
        runtime.search_handler(event(), context())
    record = search_log(capsys)
    assert record["lambda_deadline_ignored"] is False
    assert record["embedding_timing"] is None
