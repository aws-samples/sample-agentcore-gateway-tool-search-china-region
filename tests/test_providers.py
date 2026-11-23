"""Offline provider contract tests; no credentials or network are involved."""

import time
from typing import cast

import numpy as np
import pytest

from semantic_core.providers import (
    EmbeddingProvider,
    EmbeddingResult,
    ErrorCode,
    LLMProvider,
    MockEmbeddingProvider,
    MockLLMProvider,
    MockRerankProvider,
    ModelProfile,
    ProviderError,
    RequestContext,
    RerankDocument,
    RerankProvider,
    normalize_vectors,
    validate_embedding_result,
)


def context() -> RequestContext:
    return RequestContext("offline-contract", time.monotonic() + 10)


@pytest.mark.parametrize(
    "vectors",
    [
        [[0.0, 0.0]],
        [[np.nan, 1.0]],
        [[np.inf, 1.0]],
        [[-np.inf, 1.0]],
        [[1e100, 1.0]],
        [[True, False]],
        [[True, 1.0]],
        [[1e-100, 1e-100]],
        [[1 + 1j, 2]],
        [["1", "2"]],
        [[1.0], [1.0, 2.0]],
        [1.0, 2.0],
        [],
    ],
)
def test_invalid_vectors_rejected(vectors: object) -> None:
    with pytest.raises(ProviderError) as error:
        normalize_vectors(vectors, dimension=2)
    assert error.value.code is ErrorCode.INVALID_RESPONSE


def test_both_wrappers_delegate_once_to_the_shared_numerical_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semantic_core import _normalization
    from semantic_core.vectors import normalize_vectors as normalize_index

    core = _normalization.normalize_rows
    calls: list[tuple[bool, bool, int | None]] = []

    def recording_core(
        vectors: object,
        dimension: int,
        *,
        allow_empty: bool,
        float32_input: bool,
        expected_rows: int | None = None,
    ) -> np.ndarray:
        calls.append((allow_empty, float32_input, expected_rows))
        return core(
            vectors,
            dimension,
            allow_empty=allow_empty,
            float32_input=float32_input,
            expected_rows=expected_rows,
        )

    monkeypatch.setattr(_normalization, "normalize_rows", recording_core)
    provider = normalize_vectors([[3.0, 4.0]], dimension=2, rows=1)
    index = normalize_index([[3.0, 4.0]], 2)
    assert calls == [(False, True, 1), (True, False, None)]
    np.testing.assert_array_equal(provider, index)
    assert not provider.flags.writeable
    assert index.flags.writeable


@pytest.mark.parametrize(
    "vectors",
    [[], [[3e300, 4e300]], [[3e-300, 4e-300]], np.array([[3.0, 4.0]], dtype=object)],
)
def test_provider_and_index_intentionally_have_different_input_boundaries(vectors: object) -> None:
    from numpy.typing import ArrayLike

    from semantic_core.vectors import normalize_vectors as normalize_index

    indexed = normalize_index(cast(ArrayLike, vectors), 2)
    assert indexed.dtype == np.float32
    with pytest.raises(ProviderError) as error:
        normalize_vectors(vectors, dimension=2)
    assert error.value.code is ErrorCode.INVALID_RESPONSE


def test_shared_core_failure_preserves_each_wrapper_error_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semantic_core import _normalization
    from semantic_core.vectors import normalize_vectors as normalize_index

    def invalid_core(*args: object, **kwargs: object) -> np.ndarray:
        raise ValueError("invalid numeric rows")

    monkeypatch.setattr(_normalization, "normalize_rows", invalid_core)
    with pytest.raises(ValueError, match="numeric rows"):
        normalize_index([[3.0, 4.0]], 2)
    with pytest.raises(ProviderError) as error:
        normalize_vectors([[3.0, 4.0]], dimension=2)
    assert error.value.code is ErrorCode.INVALID_RESPONSE


def test_normalization_is_explicit_stable_and_read_only() -> None:
    result = normalize_vectors([[3.0, 4.0], [1e30, 1e30]], dimension=2)
    assert result.dtype == np.float32
    np.testing.assert_allclose(result, [[0.6, 0.8], [2**-0.5, 2**-0.5]], rtol=1e-6)
    assert not result.flags.writeable


def test_wrong_shape_and_profile_are_rejected() -> None:
    profile = ModelProfile("offline", "fixture-v1", 2)
    with pytest.raises(ProviderError):
        normalize_vectors([[1.0, 2.0, 3.0]], dimension=2)
    result = EmbeddingResult(np.ones((1, 2), dtype=np.float32), profile)
    for expected, rows in [
        (ModelProfile("other", "fixture-v1", 2), 1),
        (ModelProfile("offline", "fixture-v2", 2), 1),
        (ModelProfile("offline", "fixture-v1", 2, config_version="v2"), 1),
        (profile, 2),
    ]:
        with pytest.raises(ProviderError) as error:
            validate_embedding_result(result, expected, rows)
        assert error.value.code is ErrorCode.INVALID_RESPONSE


def test_mock_embedding_is_deterministic_for_both_purposes() -> None:
    provider = MockEmbeddingProvider(dimension=8)
    assert isinstance(provider, EmbeddingProvider)
    first = provider.embed(["取消订单", "search orders"], "document", context())
    second = provider.embed(["取消订单", "search orders"], "query", context())
    np.testing.assert_array_equal(first.vectors, second.vectors)
    assert first.vectors.shape == (2, 8)
    assert first.profile == provider.describe()
    assert not np.array_equal(first.vectors[0], first.vectors[1])


def test_mock_reranker_keeps_candidate_ids_and_stable_ties() -> None:
    provider = MockRerankProvider()
    assert isinstance(provider, RerankProvider)
    documents = (
        RerankDocument("b", "cancel order"),
        RerankDocument("a", "cancel order"),
        RerankDocument("c", "weather"),
    )
    result = provider.rerank("cancel", documents, 3, context())
    assert [item.id for item in result.items] == ["b", "a", "c"]
    assert all(np.isfinite(item.score) for item in result.items)


def test_mock_llm_is_deterministic_and_bounded() -> None:
    provider = MockLLMProvider()
    assert isinstance(provider, LLMProvider)
    first = provider.generate_queries("取消未发货订单", ("zh", "en"), 6, context())
    second = provider.generate_queries("取消未发货订单", ("zh", "en"), 6, context())
    assert first == second
    assert len(first.queries) == len(set(first.queries)) == 6
    assert max(map(len, first.queries)) <= 256
    assert first.prompt_version


def test_mock_deadline_and_input_errors_are_classified() -> None:
    provider = MockEmbeddingProvider(dimension=2)
    with pytest.raises(ProviderError) as error:
        provider.embed(["test"], "query", RequestContext("expired", 0))
    assert error.value.code is ErrorCode.TIMEOUT
    with pytest.raises(ProviderError) as error:
        provider.embed(["test"], "other", context())  # type: ignore[arg-type]
    assert error.value.code is ErrorCode.INVALID_REQUEST


def test_provider_error_does_not_include_request_content() -> None:
    error = ProviderError(ErrorCode.UNAVAILABLE)
    assert str(error) == "provider unavailable"
    assert "Bearer" not in repr(error)


@pytest.mark.parametrize("rows", ["1", 1.0, [], None])
def test_malformed_expected_rows_keep_provider_error_boundary(rows: object) -> None:
    if rows is None:
        # None is the documented optional value, not an invalid row count.
        assert normalize_vectors([[3.0, 4.0]], dimension=2, rows=None).shape == (1, 2)
        return
    with pytest.raises(ProviderError) as error:
        normalize_vectors([[3.0, 4.0]], dimension=2, rows=cast(int, rows))
    assert error.value.code is ErrorCode.INVALID_RESPONSE
