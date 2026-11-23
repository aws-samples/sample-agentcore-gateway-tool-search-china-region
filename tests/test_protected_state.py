"""Administrative authorization state cannot be overwritten by background publication."""

import copy
import json
from dataclasses import replace

import pytest
from botocore.exceptions import ClientError
from test_service_storage import GW, DynamoStub, S3Stub, initialize, publish

from semantic_core.catalog import canonical_json
from semantic_service.storage import PublishConflict, SnapshotStore, StorageError


@pytest.fixture
def store():
    return SnapshotStore(S3Stub(), DynamoStub(), "bucket-test", "table-test", "table-protected", GW)


def policy(epoch):
    return {"version": 1, "epoch": epoch, "principals": [], "tools": {}}


def test_physical_tables_hold_separate_initialization_items(store):
    initialize(store)
    operations = store.dynamodb.calls[-1][1]["TransactItems"]
    assert {
        (operation["Put"]["TableName"], operation["Put"]["Item"]["pk"]["S"])
        for operation in operations
    } == {
        ("table-protected", store.protected_key),
        ("table-test", store.key),
    }


def test_physical_protected_reads_never_use_the_content_table(store):
    active = initialize(store)
    store.dynamodb.calls.clear()
    assert store.read_control() == active
    assert [
        (parameters["TableName"], parameters["Key"]["pk"]["S"])
        for kind, parameters in store.dynamodb.calls
        if kind == "get"
    ] == [
        ("table-protected", store.protected_key),
        ("table-test", store.key),
        ("table-protected", store.protected_key),
    ]


def test_physical_protected_guard_and_admin_write_use_the_protected_table(store):
    active = initialize(store)
    store.dynamodb.calls.clear()
    updated = store.reconcile(active)
    transaction = store.dynamodb.calls[-1][1]["TransactItems"]
    assert transaction[0]["ConditionCheck"]["TableName"] == "table-protected"
    assert transaction[1]["Put"]["TableName"] == "table-test"
    store.update_policy(updated, policy(2))
    transaction = store.dynamodb.calls[-1][1]["TransactItems"]
    assert transaction[0]["ConditionCheck"]["TableName"] == "table-test"
    assert transaction[1]["Put"]["TableName"] == "table-protected"


@pytest.mark.parametrize("protected_table", ["table-test", "", "../protected", None])
def test_store_refuses_missing_invalid_or_shared_protected_table(protected_table):
    with pytest.raises(ValueError, match="table"):
        SnapshotStore(S3Stub(), DynamoStub(), "bucket-test", "table-test", protected_table, GW)


def test_shared_table_protected_item_requires_explicit_admin_migration(store):
    active = publish(store, initialize(store))
    # Existing shared-table layouts must not authorize reads or satisfy the
    # protected guard after deployment changes the table binding.
    legacy = store.dynamodb.items.pop((store.protected_table, store.protected_key))
    store.dynamodb.items[(store.table, store.protected_key)] = legacy
    before = copy.deepcopy(store.dynamodb.items)
    with pytest.raises(StorageError):
        store.read_control()
    with pytest.raises(PublishConflict):
        store.reconcile(active)
    with pytest.raises(PublishConflict):
        store.update_policy(active, policy(2))
    with pytest.raises(PublishConflict):
        initialize(store)
    assert store.dynamodb.items == before
    assert (store.protected_table, store.protected_key) not in store.dynamodb.items


@pytest.mark.parametrize("delete", [False, True])
def test_stale_protected_shadow_in_state_table_cannot_override_physical_guard(store, delete):
    active = publish(store, initialize(store))
    store.dynamodb.items[(store.table, store.protected_key)] = copy.deepcopy(
        store.dynamodb.items[(store.protected_table, store.protected_key)]
    )
    updated = store.tombstone(active) if delete else store.update_policy(active, policy(2))
    assert store.read_control() == updated
    with pytest.raises(PublishConflict):
        store.reconcile(active, reconciled_at=active.reconciled_at + 60)
    assert store.read_control() == updated
    assert store.dynamodb.items[(store.table, store.protected_key)] == store._protected_item(active)


def test_protected_table_outage_never_falls_back_to_state_table(store, monkeypatch):
    active = publish(store, initialize(store))
    store.dynamodb.items[(store.table, store.protected_key)] = store._protected_item(active)
    original = store.dynamodb.get_item
    reads = []

    def missing_table(**kwargs):
        reads.append(kwargs["TableName"])
        if kwargs["TableName"] == store.protected_table:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetItem")
        return original(**kwargs)

    monkeypatch.setattr(store.dynamodb, "get_item", missing_table)
    with pytest.raises(StorageError, match="control unavailable"):
        store.read_control()
    assert reads == [store.protected_table]


def test_surviving_protected_tombstone_prevents_state_table_reinitialization(store):
    deleted = store.tombstone(initialize(store))
    store.dynamodb.items.pop((store.table, store.key))
    with pytest.raises(PublishConflict):
        initialize(store)
    assert set(store.dynamodb.items) == {(store.protected_table, store.protected_key)}
    assert store.dynamodb.items[(store.protected_table, store.protected_key)] == (
        store._protected_item(deleted)
    )


def test_authorization_state_is_absent_from_writer_owned_control(store):
    active = publish(store, initialize(store))
    protected_key = store.prefix + "PROTECTED"
    assert (store.protected_table, protected_key) in store.dynamodb.items
    content = json.loads(store.dynamodb.items[(store.table, store.key)]["data"]["S"])
    protected = json.loads(
        store.dynamodb.items[(store.protected_table, protected_key)]["data"]["S"]
    )
    for field in ("policy", "policy_epoch", "tombstoned", "protected_version"):
        assert field not in content
        assert protected[field] == active.as_dict()[field]
    assert store.read_control() == active


@pytest.mark.parametrize(
    "changes",
    [
        {"policy_epoch": 2},
        {"tombstoned": True},
        {"policy": None},
    ],
)
def test_background_commit_refuses_administrative_state_changes(store, changes):
    active = publish(store, initialize(store))
    with pytest.raises(StorageError):
        store._commit(active, replace(active, **changes))
    assert store.read_control() == active


def test_background_transaction_can_only_write_control_and_checks_protected(store):
    active = publish(store, initialize(store))
    store.dynamodb.calls.clear()
    store.reconcile(active)
    transactions = [params for kind, params in store.dynamodb.calls if kind == "transact"]
    assert len(transactions) == 1
    operations = transactions[0]["TransactItems"]
    assert [next(iter(operation)) for operation in operations] == ["ConditionCheck", "Put"]
    assert operations[0]["ConditionCheck"]["Key"]["pk"]["S"] == store.prefix + "PROTECTED"
    assert operations[1]["Put"]["Item"]["pk"]["S"] == store.key
    assert "policy_epoch" not in operations[1]["Put"]["Item"]


def test_legacy_combined_control_requires_explicit_migration(store):
    active = publish(store, initialize(store))
    legacy = active.as_dict()
    legacy.pop("protected_version", None)
    store.dynamodb.items = {
        (store.table, store.key): {
            "pk": {"S": store.key},
            "data": {"S": canonical_json(legacy).decode()},
            "manifest_version": {"N": str(active.manifest_version)},
            "catalog_revision": {"N": str(active.catalog_revision)},
            "safety_revision": {"N": str(active.safety_revision)},
            "policy_epoch": {"N": str(active.policy_epoch)},
            "tombstoned": {"BOOL": active.tombstoned},
        }
    }
    with pytest.raises(StorageError):
        store.read_control()
    with pytest.raises(PublishConflict):
        initialize(store)
    assert set(store.dynamodb.items) == {(store.table, store.key)}


def test_injected_control_policy_cannot_shadow_administrator_state(store):
    active = publish(store, initialize(store))
    changed = store.update_policy(active, policy(2))
    content = json.loads(store.dynamodb.items[(store.table, store.key)]["data"]["S"])
    content["policy_epoch"] = active.policy_epoch
    content["policy"] = active.as_dict()["policy"]
    store.dynamodb.items[(store.table, store.key)]["data"]["S"] = canonical_json(content).decode()
    with pytest.raises(StorageError):
        store.read_control()
    assert changed.policy_epoch == 2


def test_policy_change_during_strong_read_is_rejected(store, monkeypatch):
    active = publish(store, initialize(store))
    original = store.dynamodb.get_item
    changed = False

    def racing_read(**kwargs):
        nonlocal changed
        if (
            kwargs["TableName"] == store.table
            and kwargs["Key"]["pk"]["S"] == store.key
            and not changed
        ):
            changed = True
            store.update_policy(active, policy(2))
        return original(**kwargs)

    monkeypatch.setattr(store.dynamodb, "get_item", racing_read)
    with pytest.raises(StorageError):
        store.read_control()
    assert changed


@pytest.mark.parametrize("delete", [False, True])
def test_administrative_change_wins_inflight_background_transaction(store, monkeypatch, delete):
    active = publish(store, initialize(store))
    original = store.dynamodb.transact_write_items
    changed = False

    def racing_write(**kwargs):
        nonlocal changed
        if not changed:
            changed = True
            if delete:
                store.tombstone(active)
            else:
                store.update_policy(active, policy(2))
        return original(**kwargs)

    monkeypatch.setattr(store.dynamodb, "transact_write_items", racing_write)
    with pytest.raises(PublishConflict):
        store.reconcile(active, reconciled_at=active.reconciled_at + 60)
    current = store.read_control()
    assert current.reconciled_at == active.reconciled_at
    assert current.tombstoned if delete else current.policy_epoch == 2


def test_tombstone_is_permanent_even_if_background_control_is_restored(store):
    active = publish(store, initialize(store))
    old_content = copy.deepcopy(store.dynamodb.items[(store.table, store.key)])
    deleted = store.tombstone(active)
    store.dynamodb.items[(store.table, store.key)] = old_content
    assert store.read_control().tombstoned
    with pytest.raises(PublishConflict):
        initialize(store)
    with pytest.raises(PublishConflict):
        store.update_policy(deleted, policy(2))


def test_administrator_policy_change_cannot_overwrite_a_newer_catalog(store):
    active = publish(store, initialize(store))
    newer = publish(store, active, ())
    with pytest.raises(PublishConflict):
        store.update_policy(active, policy(2))
    assert store.read_control() == newer


@pytest.mark.parametrize("field", ["policy_epoch", "protected_version", "tombstoned", "data"])
def test_protected_cas_columns_must_match_immutable_document(store, field):
    publish(store, initialize(store))
    item = store.dynamodb.items[(store.protected_table, store.protected_key)]
    if field == "tombstoned":
        item[field] = {"BOOL": True}
    elif field == "data":
        item[field] = {"S": "{}"}
    else:
        item[field] = {"N": "999"}
    with pytest.raises(StorageError):
        store.read_control()


def test_deletion_during_strong_read_cannot_return_a_live_policy(store, monkeypatch):
    active = publish(store, initialize(store))
    original = store.dynamodb.get_item
    changed = False

    def racing_read(**kwargs):
        nonlocal changed
        if (
            kwargs["TableName"] == store.table
            and kwargs["Key"]["pk"]["S"] == store.key
            and not changed
        ):
            changed = True
            store.tombstone(active)
        return original(**kwargs)

    monkeypatch.setattr(store.dynamodb, "get_item", racing_read)
    with pytest.raises(StorageError):
        store.read_control()
    assert changed


def test_protected_update_cannot_regress_epoch_or_mutate_content(store):
    active = publish(store, initialize(store))
    active = store.update_policy(active, policy(2))
    for updated in (
        replace(
            active,
            protected_version=active.protected_version + 1,
            policy_epoch=active.policy_epoch - 1,
        ),
        replace(
            active,
            protected_version=active.protected_version + 1,
            catalog_revision=active.catalog_revision + 1,
        ),
    ):
        with pytest.raises(StorageError):
            store._commit_protected(active, updated)
    assert store.read_control() == active
