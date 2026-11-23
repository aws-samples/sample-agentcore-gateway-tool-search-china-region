"""Full catalog reconciliation; the base BM25 publish never calls a model."""

from __future__ import annotations

import uuid
from typing import Protocol

from semantic_core import _index_config
from semantic_core.providers import RequestContext
from semantic_core.snapshot import IndexSnapshot
from semantic_core.tokenization import ChineseTokenizer
from semantic_service.catalog import CatalogError, MCPClient, TargetRegistry, _remaining
from semantic_service.storage import (
    SnapshotStore,
    StorageError,
    _SnapshotIncompatible,
    catalog_digest,
)

POLL_SECONDS = 60
INDEXER_BUDGET_SECONDS = 45


class TargetState(Protocol):
    def fingerprint(self, context: RequestContext) -> str: ...
    def validate_registry(self, registry: TargetRegistry) -> None: ...


class CatalogIndexer:
    def __init__(
        self, store: SnapshotStore, registry: TargetRegistry, mcp: MCPClient, targets: TargetState
    ) -> None:
        if store.gateway != registry.gateway:
            raise ValueError("indexer gateway mismatch")
        self.store, self.registry, self.mcp, self.targets = store, registry, mcp, targets

    def run(self, context: RequestContext) -> str:
        _remaining(context)
        control = self.store.read_control()
        if control.tombstoned:
            raise StorageError("gateway permanently deleted")
        before = self.targets.fingerprint(context)
        all_tools = self.registry.materialize(self.mcp.list_tools(context))
        observed_registry = self.registry.subset(all_tools)
        self.targets.validate_registry(observed_registry)
        # tools/list has no catalog consistency token in the configured profile.
        # Two complete reads and unchanged READY state prevent mixed-page publication.
        repeated = self.registry.materialize(self.mcp.list_tools(context))
        after = self.targets.fingerprint(context)
        self.targets.validate_registry(self.registry.subset(repeated))
        if before != after or catalog_digest(all_tools) != catalog_digest(repeated):
            raise CatalogError("catalog changed during complete read")
        _remaining(context)
        if control.catalog_digest == catalog_digest(all_tools):
            current: IndexSnapshot | None
            try:
                current = self.store.load_snapshot(control)
            except _SnapshotIncompatible:
                # 仅完整验证过的旧分词配置可以迁移；损坏和瞬时读取失败仍外抛。
                current = None
            _remaining(context)
            if (
                current is not None
                and current.bm25.tokenizer.config == _index_config.TOKENIZER_CONFIG
            ):
                self.store.reconcile(control, context=context)
                return "reconciled"
            # 词法配置变化也必须发布新目录代次，使旧增强任务的 CAS 失效。
        snapshot = IndexSnapshot.build(
            self.store.gateway,
            self.registry.searchable(all_tools),
            tokenizer=ChineseTokenizer(_index_config.TOKENIZER_CONFIG),
            catalog_revision=control.catalog_revision + 1,
            build_id=uuid.uuid4().hex,
        )
        _remaining(context)
        self.store.publish_base(control, snapshot, all_tools, context=context)
        return "published"
