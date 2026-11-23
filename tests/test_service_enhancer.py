"""Offline enhancement is resumable, isolated from base publishing, and reviewed."""

import time

import numpy as np
import pytest
from test_service_storage import GW, DynamoStub, S3Stub, SnapshotStore, initialize, publish, tool

from semantic_core.providers import (
    ErrorCode,
    GeneratedQueries,
    MockEmbeddingProvider,
    MockLLMProvider,
    ProviderError,
    RequestContext,
)
from semantic_service.enhancer import CatalogEnhancer, EnhancementRepository
from semantic_service.storage import PublishConflict, StorageError


@pytest.fixture
def store():
    return SnapshotStore(S3Stub(), DynamoStub(), "bucket-test", "table-test", "table-protected", GW)


def context(seconds=240):
    return RequestContext("enhancer-test", time.monotonic() + seconds)


def test_cache_and_progress_access_only_the_state_table(store):
    repository = EnhancementRepository(store)
    repository.cache("a" * 64, {"fixture": "cached"})
    repository.save_progress("b" * 64, {"fixture": "progress"})
    assert repository.cached("a" * 64) == {"fixture": "cached"}
    assert repository.progress("b" * 64) == {"fixture": "progress"}
    assert {table for table, _ in store.dynamodb.items} == {store.table}
    assert all(params["TableName"] == store.table for _, params in store.dynamodb.calls)


class CountingEmbedding(MockEmbeddingProvider):
    def __init__(self):
        super().__init__(4)
        self.calls = 0

    def embed(self, texts, purpose, context):
        self.calls += 1
        return super().embed(texts, purpose, context)


class ReviewedLLM(MockLLMProvider):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def generate_queries(self, description, languages, count, context):
        self.calls += 1
        return GeneratedQueries(
            ("cancel the order", "find the order"), self.describe(), "mock-queries-v1"
        )


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (("cancel the order",), ("find the order",)),
        (("cancel the order",), ()),
        (("cancel the order", "find the order"), ("find the order", "cancel the order")),
    ],
)
@pytest.mark.parametrize("new_revision", [False, True])
def test_same_review_version_reselection_misses_vector_cache(store, before, after, new_revision):
    publish(store, initialize(store))
    provider, llm = CountingEmbedding(), ReviewedLLM()
    selection = before
    enhancer = CatalogEnhancer(
        store,
        EnhancementRepository(store),
        provider,
        llm,
        prompt_version="mock-queries-v1",
        review=lambda tool, generated: selection,
        review_version="approved-v1",
    )
    assert enhancer.run(context()) == "published"
    assert provider.calls == llm.calls == 1
    if new_revision:
        publish(store, store.read_control())
    selection = after
    assert enhancer.run(context()) == "published"
    assert provider.calls == 2
    assert llm.calls == 1
    enhanced = store.load_snapshot(store.read_control())
    assert enhanced.tools[0].examples == after
    expected = (
        MockEmbeddingProvider(4).embed([tool().summary, *after], "document", context()).vectors
    )
    np.testing.assert_allclose(enhanced.vectors.vectors, expected, atol=1e-7)
    assert enhancer.run(context()) == "unchanged"
    assert provider.calls == 2
    selection = before
    assert enhancer.run(context()) == "published"
    assert store.load_snapshot(store.read_control()).tools[0].examples == before
    assert provider.calls == 2 and llm.calls == 1


def test_same_review_version_reselection_rechecks_resumed_prefix(store):
    active = publish(store, initialize(store), (tool(), tool("orders___find")))
    provider, llm = CountingEmbedding(), ReviewedLLM()
    selection = ("cancel the order",)
    enhancer = CatalogEnhancer(
        store,
        EnhancementRepository(store),
        provider,
        llm,
        prompt_version="mock-queries-v1",
        review=lambda tool, generated: selection,
        review_version="approved-v1",
        max_items=1,
    )
    assert enhancer.run(context()) == "paused"
    assert store.read_control() == active
    selection = ("find the order",)
    # Finish the second descriptor, then replace the first descriptor's stale vectors.
    assert enhancer.run(context()) == "paused"
    assert store.read_control() == active
    assert enhancer.run(context()) == "published"
    assert provider.calls == 3 and llm.calls == 2
    enhanced = store.load_snapshot(store.read_control())
    assert all(record.examples == selection for record in enhanced.tools)
    expected = (
        MockEmbeddingProvider(4)
        .embed(
            [text for record in enhanced.tools for text in (record.summary, *selection)],
            "document",
            context(),
        )
        .vectors
    )
    np.testing.assert_allclose(enhanced.vectors.vectors, expected, atol=1e-7)


def test_disabling_review_under_same_version_withdraws_cached_examples(store):
    publish(store, initialize(store))
    provider, llm = CountingEmbedding(), ReviewedLLM()
    repository = EnhancementRepository(store)
    options = {"prompt_version": "mock-queries-v1", "review_version": "approved-v1"}
    enhancer = CatalogEnhancer(
        store, repository, provider, llm, review=lambda tool, generated: generated[:1], **options
    )
    assert enhancer.run(context()) == "published"
    unreviewed = CatalogEnhancer(store, repository, provider, llm, review=None, **options)
    assert unreviewed.run(context()) == "published"
    assert store.load_snapshot(store.read_control()).tools[0].examples == ()
    assert provider.calls == 2 and llm.calls == 1


def test_reselection_provider_failure_keeps_active_snapshot_and_resumes(store):
    publish(store, initialize(store))
    provider, llm = CountingEmbedding(), ReviewedLLM()
    selection = ("cancel the order",)
    enhancer = CatalogEnhancer(
        store,
        EnhancementRepository(store),
        provider,
        llm,
        prompt_version="mock-queries-v1",
        review=lambda tool, generated: selection,
        review_version="approved-v1",
    )
    assert enhancer.run(context()) == "published"
    active = store.read_control()
    selection = ("find the order",)
    original = provider.embed
    provider.embed = lambda *args: (_ for _ in ()).throw(ProviderError(ErrorCode.UNAVAILABLE))
    assert enhancer.run(context()) == "paused"
    assert store.read_control() == active
    provider.embed = original
    assert enhancer.run(context()) == "published"
    assert store.load_snapshot(store.read_control()).tools[0].examples == selection
    assert provider.calls == 2 and llm.calls == 1


@pytest.mark.parametrize("corruption", ["selection", "digest", "count", "generation"])
def test_cache_restore_rejects_selection_or_integrity_drift(store, corruption):
    publish(store, initialize(store))

    class CorruptRepository(EnhancementRepository):
        corrupt = False

        def cached(self, key):
            entry = super().cached(key)
            if not self.corrupt or entry is None:
                return entry
            if "generated" in entry:
                if corruption == "generation":
                    entry["generated"] = [" "]
            elif corruption == "selection":
                # Same number of vectors and same review_version are insufficient.
                entry["approved"] = ["find the order"]
            elif corruption == "digest":
                entry["approved_digest"] = "0" * 64
            elif corruption == "count":
                entry["vectors"] = entry["vectors"][:-1]
            return entry

    repository = CorruptRepository(store)
    enhancer = CatalogEnhancer(
        store,
        repository,
        MockEmbeddingProvider(4),
        ReviewedLLM(),
        prompt_version="mock-queries-v1",
        review=lambda tool, generated: generated[:1],
        review_version="approved-v1",
    )
    assert enhancer.run(context()) == "published"
    active = store.read_control()
    repository.corrupt = True
    with pytest.raises(StorageError):
        enhancer.run(context())
    assert store.read_control() == active


def test_unreviewed_queries_remain_pending_but_real_description_vectors_publish(store):
    active = publish(store, initialize(store))
    repository = EnhancementRepository(store)
    enhancer = CatalogEnhancer(
        store,
        repository,
        MockEmbeddingProvider(4),
        MockLLMProvider(),
        prompt_version="mock-queries-v1",
    )
    assert enhancer.run(context()) == "published"
    enhanced = store.load_snapshot(store.read_control())
    assert enhanced.catalog_revision == active.catalog_revision
    assert enhanced.tools[0].examples == ()
    assert len(enhanced.vectors.vectors) == 1
    assert enhancer.run(context()) == "unchanged"


def test_interrupted_slice_resumes_cached_items_and_publishes_only_when_complete(store):
    active = publish(store, initialize(store), (tool(), tool("orders___find")))
    provider = CountingEmbedding()
    repository = EnhancementRepository(store)
    enhancer = CatalogEnhancer(store, repository, provider, reserve_seconds=10, max_items=1)
    assert enhancer.run(context()) == "paused"
    assert store.read_control() == active
    assert provider.calls == 1
    assert enhancer.run(context()) == "published"
    assert provider.calls == 2
    assert len(store.load_snapshot(store.read_control()).vectors.vectors) == 2


def test_provider_failure_keeps_base_and_next_tick_resumes(store):
    active = publish(store, initialize(store))
    provider = CountingEmbedding()
    original = provider.embed
    provider.embed = lambda *args: (_ for _ in ()).throw(ProviderError(ErrorCode.UNAVAILABLE))
    enhancer = CatalogEnhancer(store, EnhancementRepository(store), provider)
    assert enhancer.run(context()) == "paused"
    assert store.read_control() == active
    provider.embed = original
    assert enhancer.run(context()) == "published"


def test_reviewed_examples_only_and_schema_invalid_generation_falls_back(store):
    publish(store, initialize(store))

    class InvalidLLM(MockLLMProvider):
        def generate_queries(self, description, languages, count, context):
            return GeneratedQueries(("x" * 257,), self.describe(), "mock-queries-v1")

    invalid = CatalogEnhancer(
        store,
        EnhancementRepository(store),
        MockEmbeddingProvider(4),
        InvalidLLM(),
        prompt_version="mock-queries-v1",
    )
    assert invalid.run(context()) == "published"
    assert store.load_snapshot(store.read_control()).tools[0].examples == ()


def test_descriptor_cache_reused_across_new_catalog_revision(store):
    initial = publish(store, initialize(store))
    provider = CountingEmbedding()
    enhancer = CatalogEnhancer(store, EnhancementRepository(store), provider)
    assert enhancer.run(context()) == "published"
    assert provider.calls == 1
    publish(store, store.read_control(), (tool(), tool("orders___find")))
    assert enhancer.run(context()) == "published"
    assert provider.calls == 2
    assert store.read_control().catalog_revision == initial.catalog_revision + 1


def test_stale_enhancer_publish_rejected_after_base_changes(store):
    active = publish(store, initialize(store))

    class RacingRepository(EnhancementRepository):
        def save_progress(self, key, value):
            super().save_progress(key, value)
            if value["cursor"] == 1:
                publish(store, active, (tool(incarnation="inc-2"),))

    enhancer = CatalogEnhancer(store, RacingRepository(store), MockEmbeddingProvider(4))
    with pytest.raises(PublishConflict):
        enhancer.run(context())
    assert store.read_control().catalog_revision == 2
