"""Resumable offline generation and vectors, fenced by the active catalog CAS."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import asdict
from typing import cast

import numpy as np
from jsonschema import Draft202012Validator, ValidationError

from semantic_core.catalog import ToolRecord, canonical_json, digest
from semantic_core.providers import (
    EmbeddingProvider,
    GeneratedQueries,
    LLMProvider,
    ProviderError,
    RequestContext,
    validate_embedding_result,
)
from semantic_core.snapshot import IndexSnapshot
from semantic_core.vectors import VectorIndex
from semantic_service.storage import (
    ObjectRef,
    SnapshotStore,
    StorageError,
    _decode,
    _integer,
    _object,
    _string,
)

ENHANCER_INTERVAL_SECONDS = 300
ENHANCER_BUDGET_SECONDS = 240
_QUERY_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["queries"],
    "additionalProperties": False,
    "properties": {
        "queries": {
            "type": "array",
            "minItems": 1,
            "maxItems": 6,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 256},
        }
    },
}
Review = Callable[[ToolRecord, tuple[str, ...]], tuple[str, ...]]


class EnhancementRepository:
    """Immutable per-descriptor artifacts plus small revision-scoped cursor records.

    Progress may safely outlive a catalog revision: its namespace contains that
    revision and the final pointer is still conditional on the original control.
    Only one enhancer executes per Gateway; no distributed lease is implied.
    """

    def __init__(self, store: SnapshotStore) -> None:
        self.store = store

    def _key(self, kind: str, key: str) -> str:
        if not key or any(character not in "0123456789abcdef-" for character in key):
            raise StorageError("invalid enhancement key")
        return self.store.prefix + kind + "/" + key

    def _read(self, kind: str, key: str) -> dict[str, object] | None:
        try:
            response = self.store.dynamodb.get_item(
                TableName=self.store.table,
                Key={"pk": {"S": self._key(kind, key)}},
                ConsistentRead=True,
            )
            item = response.get("Item")
            if item is None:
                return None
            return _decode(_string(_object(_object(item)["data"])["S"]).encode())
        except StorageError:
            raise
        except Exception:
            raise StorageError("enhancement record unavailable") from None

    def _write(self, kind: str, key: str, value: dict[str, object], *, immutable: bool) -> None:
        params: dict[str, object] = {
            "TableName": self.store.table,
            "Item": {
                "pk": {"S": self._key(kind, key)},
                "data": {"S": canonical_json(value).decode()},
            },
        }
        if immutable:
            params["ConditionExpression"] = "attribute_not_exists(pk)"
        try:
            self.store.dynamodb.put_item(**params)
        except Exception:
            raise StorageError("enhancement record write failed") from None

    def cached(self, key: str) -> dict[str, object] | None:
        reference = self._read("enhancement-cache", key)
        return (
            _decode(self.store._get(ObjectRef.parse(reference), limit=2 * 1024 * 1024))
            if (reference is not None)
            else None
        )

    def cache(self, key: str, value: dict[str, object]) -> None:
        reference = self.store._put(
            self.store.prefix + "enhancement/" + uuid.uuid4().hex + ".json", canonical_json(value)
        )
        self._write(
            "enhancement-cache", key, cast(dict[str, object], asdict(reference)), immutable=True
        )

    def progress(self, key: str) -> dict[str, object] | None:
        return self._read("enhancement-progress", key)

    def save_progress(self, key: str, value: dict[str, object]) -> None:
        # A bounded prefix cursor is sufficient because every completed descriptor
        # already has an independently durable cache entry keyed by profile/hash.
        self._write("enhancement-progress", key, value, immutable=False)


class CatalogEnhancer:
    def __init__(
        self,
        store: SnapshotStore,
        repository: EnhancementRepository,
        embedding: EmbeddingProvider,
        llm: LLMProvider | None = None,
        *,
        prompt_version: str = "tool-queries-v2",
        languages: tuple[str, ...] = ("zh", "en"),
        review: Review | None = None,
        review_version: str = "unreviewed-v1",
        reserve_seconds: float = 10,
        max_items: int = 20_000,
    ) -> None:
        if (
            not prompt_version
            or not languages
            or any(lang not in ("zh", "en") for lang in languages)
            or len(set(languages)) != len(languages)
            or reserve_seconds < 0
            or max_items < 1
            or (review is not None and review_version == "unreviewed-v1")
        ):
            raise ValueError("invalid enhancer configuration")
        if embedding.describe().dimension is None:
            raise ValueError("enhancer requires a versioned embedding dimension")
        self.store, self.repository, self.embedding, self.llm = store, repository, embedding, llm
        self.prompt_version, self.languages = prompt_version, languages
        self.review, self.review_version = review, review_version
        self.reserve_seconds, self.max_items = reserve_seconds, max_items
        self.profile = {
            "cache_version": 2,
            "embedding": asdict(embedding.describe()),
            "llm": asdict(llm.describe()) if llm is not None else None,
            "prompt": prompt_version,
            "languages": list(languages),
            "review": review_version,
        }
        self.profile_key = digest(canonical_json(self.profile))

    def _key(self, tool: ToolRecord, approved: tuple[str, ...]) -> str:
        return digest(
            canonical_json(
                {
                    "descriptor": tool.descriptor_digest,
                    "profile": self.profile,
                    "approved_digest": digest(canonical_json(list(approved))),
                }
            )
        )

    def _generated(self, tool: ToolRecord, context: RequestContext) -> tuple[str, ...]:
        if self.llm is None:
            return ()
        try:
            generated: GeneratedQueries = self.llm.generate_queries(
                tool.summary, self.languages, 6, context
            )
            if generated.profile != self.llm.describe() or generated.prompt_version != (
                self.prompt_version
            ):
                return ()
            payload = {"queries": list(generated.queries)}
            Draft202012Validator(_QUERY_SCHEMA).validate(payload)
            if any(not query.strip() for query in generated.queries):
                return ()
            return generated.queries
        except (ProviderError, ValidationError, ValueError):
            return ()

    def _approved(self, tool: ToolRecord, context: RequestContext) -> tuple[str, ...]:
        # Generation is independent of a later selection, so a reselection need
        # not call the LLM again. Old combined cache/progress keys are not reused.
        key = digest(
            canonical_json(
                {
                    "kind": "generation",
                    "descriptor": tool.descriptor_digest,
                    "profile": self.profile,
                }
            )
        )
        entry = self.repository.cached(key)
        if entry is None:
            entry = {
                "version": 1,
                "descriptor_digest": tool.descriptor_digest,
                "profile": self.profile,
                "generated": list(self._generated(tool, context)),
            }
            self.repository.cache(key, entry)
        if (
            set(entry) != {"version", "descriptor_digest", "profile", "generated"}
            or entry["version"] != 1
            or entry["descriptor_digest"] != tool.descriptor_digest
            or entry["profile"] != self.profile
            or not isinstance(entry["generated"], list)
        ):
            raise StorageError("generation cache identity mismatch")
        queries = entry["generated"]
        if queries:
            try:
                Draft202012Validator(_QUERY_SCHEMA).validate({"queries": queries})
            except ValidationError:
                raise StorageError("invalid cached generation") from None
        generated = tuple(cast(list[str], queries))
        if any(not query.strip() for query in generated):
            raise StorageError("invalid cached generation")
        approved = self.review(tool, generated) if self.review is not None else ()
        if (
            not isinstance(approved, tuple)
            or any(query not in generated for query in approved)
            or len(set(approved)) != len(approved)
        ):
            raise StorageError("review must select validated generated examples")
        return approved

    def _create(
        self, tool: ToolRecord, approved: tuple[str, ...], context: RequestContext
    ) -> dict[str, object]:
        texts = [tool.summary, *approved]
        result = self.embedding.embed(texts, "document", context)
        vectors = validate_embedding_result(result, self.embedding.describe(), len(texts))
        return {
            "version": 2,
            "descriptor_digest": tool.descriptor_digest,
            "profile": self.profile,
            "approved": list(approved),
            "approved_digest": digest(canonical_json(list(approved))),
            "vectors": vectors.tolist(),
        }

    def _restore(
        self, tool: ToolRecord, approved: tuple[str, ...], entry: dict[str, object]
    ) -> tuple[ToolRecord, list[object]]:
        if (
            set(entry)
            != {
                "version",
                "descriptor_digest",
                "profile",
                "approved",
                "approved_digest",
                "vectors",
            }
            or entry["version"] != 2
            or entry["descriptor_digest"] != tool.descriptor_digest
            or entry["profile"] != self.profile
            or entry["approved"] != list(approved)
            or entry["approved_digest"] != digest(canonical_json(list(approved)))
        ):
            raise StorageError("enhancement cache identity mismatch")
        try:
            record = ToolRecord.from_descriptor(tool.identity, tool.descriptor, approved)
        except ValueError:
            raise StorageError("invalid reviewed enhancement") from None
        vectors = entry["vectors"]
        if not isinstance(vectors, list) or len(vectors) != 1 + len(approved):
            raise StorageError("enhancement vector count mismatch")
        return record, vectors

    def run(self, context: RequestContext) -> str:
        control = self.store.read_control()
        snapshot = self.store.load_snapshot(control)
        progress_key = f"{control.catalog_revision}-{self.profile_key}"
        progress = self.repository.progress(progress_key)
        if progress is None:
            progress = {
                "version": 2,
                "catalog_revision": control.catalog_revision,
                "profile_key": self.profile_key,
                "cursor": 0,
                "completed": False,
                "build_id": "",
                "selection_digest": "",
            }
        if (
            set(progress)
            != {
                "version",
                "catalog_revision",
                "profile_key",
                "cursor",
                "completed",
                "build_id",
                "selection_digest",
            }
            or progress["version"] != 2
            or progress["catalog_revision"] != control.catalog_revision
            or progress["profile_key"] != self.profile_key
            or type(progress["completed"]) is not bool
            or not isinstance(progress["selection_digest"], str)
        ):
            raise StorageError("enhancement progress identity mismatch")
        cursor = _integer(progress["cursor"])
        if cursor > len(snapshot.tools):
            raise StorageError("enhancement cursor exceeds catalog")
        processed = 0
        while cursor < len(snapshot.tools):
            try:
                if context.remaining() <= self.reserve_seconds or processed >= self.max_items:
                    self.repository.save_progress(progress_key, progress)
                    return "paused"
                tool = snapshot.tools[cursor]
                approved = self._approved(tool, context)
                key = self._key(tool, approved)
                if self.repository.cached(key) is None:
                    entry = self._create(tool, approved, context)
                    self.repository.cache(key, entry)
                cursor += 1
                processed += 1
                progress["cursor"] = cursor
                self.repository.save_progress(progress_key, progress)
            except ProviderError:
                self.repository.save_progress(progress_key, progress)
                return "paused"
        tools: list[ToolRecord] = []
        rows: list[object] = []
        parents: list[int] = []
        keys: list[str] = []
        for index, tool in enumerate(snapshot.tools):
            try:
                if context.remaining() <= self.reserve_seconds:
                    return "paused"
                # A persisted cursor/completed flag cannot attest the current
                # review selection. Resolve it again before binding any vectors.
                approved = self._approved(tool, context)
                key = self._key(tool, approved)
                cached_entry = self.repository.cached(key)
                if cached_entry is None:
                    if processed >= self.max_items:
                        return "paused"
                    cached_entry = self._create(tool, approved, context)
                    self.repository.cache(key, cached_entry)
                    processed += 1
            except ProviderError:
                return "paused"
            restored, vectors = self._restore(tool, approved, cached_entry)
            keys.append(key)
            tools.append(restored)
            rows.extend(vectors)
            parents.extend([index] * len(vectors))
        selection_digest = digest(canonical_json(keys))
        if (
            progress["completed"]
            and progress["build_id"] == snapshot.build_id
            and progress["selection_digest"] == selection_digest
        ):
            return "unchanged"
        dimension = self.embedding.describe().dimension
        assert dimension is not None
        matrix = (
            np.asarray(rows, dtype=np.float32) if rows else np.empty((0, dimension), np.float32)
        )
        vector_index = VectorIndex(matrix, parents, len(tools), dimension)
        enhanced = IndexSnapshot.build(
            self.store.gateway,
            tools,
            # 同一目录代次的增强必须沿用原词法配置；仅 Indexer 负责迁移。
            tokenizer=snapshot.bm25.tokenizer,
            catalog_revision=control.catalog_revision,
            build_id=uuid.uuid4().hex,
            vectors=vector_index,
            model_profile=self.embedding.describe(),
        )
        try:
            if context.remaining() <= self.reserve_seconds:
                return "paused"
        except ProviderError:
            return "paused"
        self.store.publish_enhanced(control, enhanced, context=context)
        progress["completed"], progress["build_id"] = True, enhanced.build_id
        progress["selection_digest"] = selection_digest
        self.repository.save_progress(progress_key, progress)
        return "published"
