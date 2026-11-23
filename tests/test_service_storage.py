"""Remote durability and publish races, using a stateful AWS-shaped stub."""

import io
from dataclasses import replace

import pytest
from botocore.exceptions import ClientError

from semantic_core.catalog import GatewayIdentity, ToolIdentity, ToolRecord
from semantic_core.snapshot import IndexSnapshot
from semantic_service.storage import PublishConflict, SnapshotReader, SnapshotStore, StorageError

GW = GatewayIdentity("123456789012", "cn-north-1", "gateway-test")


class S3Stub:
    def __init__(self):
        self.objects = {}
        self.calls = []

    def put_object(self, **kwargs):
        self.calls.append(("put", kwargs))
        key = kwargs["Key"]
        if key in self.objects and kwargs.get("IfNoneMatch") == "*":
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        version = f"version-{len(self.objects)}"
        self.objects[key] = (version, kwargs["Body"])
        return {"VersionId": version}

    def get_object(self, **kwargs):
        self.calls.append(("get", kwargs))
        version, body = self.objects[kwargs["Key"]]
        assert version == kwargs["VersionId"]
        return {"VersionId": version, "ContentLength": len(body), "Body": io.BytesIO(body)}


class DynamoStub:
    def __init__(self):
        self.items = {}
        self.calls = []

    def get_item(self, **kwargs):
        self.calls.append(("get", kwargs))
        assert kwargs["ConsistentRead"] is True
        item = self.items.get((kwargs["TableName"], kwargs["Key"]["pk"]["S"]))
        return {"Item": item} if item is not None else {}

    def put_item(self, **kwargs):
        self.calls.append(("put", kwargs))
        item = kwargs["Item"]
        key = (kwargs["TableName"], item["pk"]["S"])
        if self._conflict(kwargs, key):
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[key] = item
        return {}

    def _conflict(self, kwargs, key):
        existing = self.items.get(key)
        cond = kwargs.get("ConditionExpression", "")
        if cond == "attribute_not_exists(pk)":
            return existing is not None
        if not cond:
            return False
        names = kwargs.get("ExpressionAttributeNames", {})
        vals = kwargs["ExpressionAttributeValues"]
        return existing is None or any(
            existing.get(names.get(field, field)) != vals[token]
            for field, token in (term.split(" = ") for term in cond.split(" AND "))
        )

    def transact_write_items(self, **kwargs):
        self.calls.append(("transact", kwargs))
        writes = []
        reasons = []
        for operation in kwargs["TransactItems"]:
            kind, params = next(iter(operation.items()))
            assert kind in {"Put", "ConditionCheck"}
            item_key = params["Item"]["pk"]["S"] if kind == "Put" else params["Key"]["pk"]["S"]
            key = (params["TableName"], item_key)
            reasons.append(
                {"Code": "ConditionalCheckFailed" if self._conflict(params, key) else "None"}
            )
            if kind == "Put":
                writes.append((key, params["Item"]))
        if any(reason["Code"] != "None" for reason in reasons):
            raise ClientError(
                {
                    "Error": {"Code": "TransactionCanceledException"},
                    "CancellationReasons": reasons,
                },
                "TransactWriteItems",
            )
        self.items.update(writes)
        return {}


@pytest.fixture
def store():
    return SnapshotStore(S3Stub(), DynamoStub(), "bucket-test", "table-test", "table-protected", GW)


def tool(name="orders___cancel", incarnation="inc-1"):
    return ToolRecord.from_descriptor(
        ToolIdentity(GW, "target-1", incarnation, name),
        {"name": name, "description": "Cancel order", "inputSchema": {"type": "object"}},
    )


def initialize(store):
    return store.initialize(
        {"version": 1, "epoch": 1, "principals": [], "tools": {}}, policy_epoch=1
    )


def publish(store, control, tools=None):
    records = (tool(),) if tools is None else tools
    snapshot = IndexSnapshot.build(
        GW,
        records,
        catalog_revision=control.catalog_revision + 1,
        build_id=f"build-{control.catalog_revision + 1}",
    )
    return store.publish_base(control, snapshot, records)


def test_atomic_publish_read_and_strong_control(store):
    initial = initialize(store)
    active = publish(store, initial)
    snapshot = SnapshotReader(store).current(active)
    assert snapshot.tools == (tool(),)
    assert active.catalog_revision == 1 and active.manifest_version == 1
    assert store.load_safety(active) == {tool().tool_id: tool().schema_digest}
    assert store.read_control() == active
    assert all(call[1]["IfNoneMatch"] == "*" for call in store.s3.calls if call[0] == "put")


def test_conflicting_writer_cannot_publish_or_resurrect(store):
    initial = initialize(store)
    active = publish(store, initial)
    with pytest.raises(PublishConflict):
        publish(store, initial)
    deleted = store.tombstone(active)
    assert deleted.tombstoned
    with pytest.raises(PublishConflict):
        publish(store, active)
    with pytest.raises(PublishConflict):
        initialize(store)
    with pytest.raises(StorageError):
        SnapshotReader(store).current(deleted)


def test_same_digest_updates_reconciliation_only_and_empty_is_valid(store):
    initial = initialize(store)
    active = publish(store, initial)
    reconciled = store.reconcile(active, reconciled_at=active.reconciled_at + 60)
    assert reconciled.manifest_version == active.manifest_version
    assert reconciled.catalog_revision == active.catalog_revision
    assert reconciled.reconciled_at > active.reconciled_at
    empty = publish(store, reconciled, ())
    assert SnapshotReader(store).current(empty).tools == ()


def test_enhancer_rejected_when_catalog_changes(store):
    initial = initialize(store)
    active = publish(store, initial)
    next_active = publish(store, active, (tool(incarnation="inc-2"),))
    enhanced = IndexSnapshot.build(GW, [tool()], catalog_revision=1, build_id="enhanced")
    with pytest.raises(PublishConflict):
        store.publish_enhanced(active, enhanced)
    assert store.read_control() == next_active


def test_reader_refreshes_security_change_and_detects_object_tampering(store):
    initial = initialize(store)
    active = publish(store, initial)
    reader = SnapshotReader(store, clock=lambda: 0)
    assert reader.current(active).tools[0].identity.incarnation == "inc-1"
    second = publish(store, active, (tool(incarnation="inc-2"),))
    assert reader.current(second).tools[0].identity.incarnation == "inc-2"
    reference = second.manifest
    version, blob = store.s3.objects[reference.key]
    store.s3.objects[reference.key] = (version, blob + b" ")
    with pytest.raises(StorageError):
        SnapshotReader(store).current(second)


def test_reader_can_keep_same_safety_content_until_30_second_check(store):
    now = [0.0]
    active = publish(store, initialize(store))
    reader = SnapshotReader(store, clock=lambda: now[0])
    old = reader.current(active)
    enhanced = IndexSnapshot.build(GW, [tool()], catalog_revision=1, build_id="enhanced")
    new = store.publish_enhanced(active, enhanced)
    assert reader.current(new) is old
    now[0] = 30
    assert reader.current(new).build_id == "enhanced"


def test_policy_epoch_race_is_cas_protected(store):
    active = publish(store, initialize(store))
    store.update_policy(active, {"version": 1, "epoch": 2, "principals": [], "tools": {}})
    with pytest.raises(PublishConflict):
        store.reconcile(active)


def test_remote_reference_cannot_escape_gateway_namespace(store):
    active = publish(store, initialize(store))
    malicious = replace(active, manifest=replace(active.manifest, key="other/manifest.json"))
    with pytest.raises(StorageError):
        SnapshotReader(store).current(malicious)


def test_partial_upload_never_changes_active_pointer(store, monkeypatch):
    active = publish(store, initialize(store))
    original = store.s3.put_object

    def failing(**kwargs):
        if kwargs["Key"].endswith("bm25.json"):
            raise OSError("offline failure")
        return original(**kwargs)

    monkeypatch.setattr(store.s3, "put_object", failing)
    with pytest.raises(StorageError):
        publish(store, active, ())
    assert store.read_control() == active


def test_policy_update_advances_epoch_and_cannot_overwrite_concurrent_catalog(store):
    active = publish(store, initialize(store))
    changed = store.update_policy(active, {"version": 1, "epoch": 2, "principals": [], "tools": {}})
    assert changed.policy_epoch == 2
    assert changed.catalog_revision == active.catalog_revision
    assert store.load_policy(changed)["epoch"] == 2
    with pytest.raises(PublishConflict):
        store.update_policy(active, {"version": 1, "epoch": 2, "principals": [], "tools": {}})


def test_deadline_exhausted_during_upload_prevents_final_pointer_publish(store, monkeypatch):
    from semantic_core.providers import ErrorCode, ProviderError

    active = publish(store, initialize(store))
    expired = [False]
    original = store.s3.put_object

    class Deadline:
        def remaining(self):
            if expired[0]:
                raise ProviderError(ErrorCode.TIMEOUT)
            return 5.0

    def expire(**kwargs):
        result = original(**kwargs)
        expired[0] = True
        return result

    monkeypatch.setattr(store.s3, "put_object", expire)
    snapshot = IndexSnapshot.build(GW, (), catalog_revision=2, build_id="timeout")
    with pytest.raises(StorageError):
        store.publish_base(active, snapshot, (), context=Deadline())
    assert store.read_control() == active


def test_reconciliation_does_not_invalidate_enhancement_or_restore_old_timestamp(store):
    active = publish(store, initialize(store))
    reconciled = store.reconcile(active, reconciled_at=active.reconciled_at + 60)
    enhanced = IndexSnapshot.build(GW, [tool()], catalog_revision=1, build_id="enhanced")
    result = store.publish_enhanced(active, enhanced)
    assert result.reconciled_at == reconciled.reconciled_at
    assert result.manifest_version == reconciled.manifest_version + 1
    assert result.catalog_revision == reconciled.catalog_revision
