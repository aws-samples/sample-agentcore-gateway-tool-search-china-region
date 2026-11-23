"""Pinned botocore models validate the exact SDK calls made by service adapters."""

import io
import time
from datetime import UTC, datetime

import boto3
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

from semantic_core.catalog import GatewayIdentity, digest
from semantic_core.providers import RequestContext
from semantic_service.catalog import TargetStateSource
from semantic_service.storage import ControlRecord, ObjectRef, SnapshotStore

GATEWAY = GatewayIdentity("000000000000", "cn-north-1", "fixture-0123456789")


def aws(service):
    return boto3.client(  # nosec B106
        service,
        region_name="cn-north-1",
        aws_access_key_id="synthetic-access",
        aws_secret_access_key="synthetic-secret",
    )


def context():
    return RequestContext("sdk-contract", time.monotonic() + 45)


def target(target_id):
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    return {
        "gatewayArn": (
            "arn:aws-cn:bedrock-agentcore:cn-north-1:000000000000:gateway/fixture-0123456789"
        ),
        "targetId": target_id,
        "createdAt": timestamp,
        "updatedAt": timestamp,
        "status": "READY",
        "name": "fixture",
        "targetConfiguration": {"mcp": {"mcpServer": {"endpoint": "https://fixture.invalid/mcp"}}},
        "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    }


def summary(target_id):
    full = target(target_id)
    return {key: full[key] for key in ("targetId", "name", "status", "createdAt", "updatedAt")}


def test_target_list_get_pagination_matches_pinned_boto3_service_model():
    client = aws("bedrock-agentcore-control")
    with Stubber(client) as stubber:
        stubber.add_response(
            "list_gateway_targets",
            {"items": [summary("0123456789")], "nextToken": "next"},
            {"gatewayIdentifier": GATEWAY.gateway_id, "maxResults": 100},
        )
        stubber.add_response(
            "get_gateway_target",
            target("0123456789"),
            {"gatewayIdentifier": GATEWAY.gateway_id, "targetId": "0123456789"},
        )
        stubber.add_response(
            "list_gateway_targets",
            {"items": [summary("abcdefghij")]},
            {"gatewayIdentifier": GATEWAY.gateway_id, "maxResults": 100, "nextToken": "next"},
        )
        stubber.add_response(
            "get_gateway_target",
            target("abcdefghij"),
            {"gatewayIdentifier": GATEWAY.gateway_id, "targetId": "abcdefghij"},
        )
        fingerprint = TargetStateSource(client, GATEWAY).fingerprint(context())
        assert len(fingerprint) == 64
        stubber.assert_no_pending_responses()


def test_s3_versioned_conditional_write_and_verification_use_real_shapes():
    s3, dynamo = aws("s3"), aws("dynamodb")
    store = SnapshotStore(
        s3, dynamo, "fixture-bucket", "fixture-table", "fixture-protected", GATEWAY
    )
    key = store.prefix + "versions/build/manifest.json"
    data = b'{"fixture":true}'
    with Stubber(s3) as stubber:
        stubber.add_response(
            "put_object",
            {"VersionId": "synthetic-version"},
            {
                "Bucket": "fixture-bucket",
                "Key": key,
                "Body": data,
                "IfNoneMatch": "*",
                "ServerSideEncryption": "AES256",
            },
        )
        stubber.add_response(
            "get_object",
            {
                "VersionId": "synthetic-version",
                "ContentLength": len(data),
                "Body": StreamingBody(io.BytesIO(data), len(data)),
            },
            {"Bucket": "fixture-bucket", "Key": key, "VersionId": "synthetic-version"},
        )
        reference = store._put(key, data)
        assert reference == ObjectRef(key, "synthetic-version", len(data), digest(data))
        stubber.assert_no_pending_responses()


def test_dynamodb_protected_reads_and_transaction_use_real_attribute_shapes():
    s3, dynamo = aws("s3"), aws("dynamodb")
    store = SnapshotStore(
        s3, dynamo, "fixture-bucket", "fixture-table", "fixture-protected", GATEWAY
    )
    control = ControlRecord(
        GATEWAY, policy=ObjectRef(store.prefix + "policies/fixture.json", "v1", 2, digest(b"{}"))
    )
    with Stubber(dynamo) as stubber:
        protected_read = {
            "TableName": "fixture-protected",
            "Key": {"pk": {"S": store.protected_key}},
            "ConsistentRead": True,
        }
        stubber.add_response("get_item", {"Item": store._protected_item(control)}, protected_read)
        stubber.add_response(
            "get_item",
            {"Item": store._item(control)},
            {"TableName": "fixture-table", "Key": {"pk": {"S": store.key}}, "ConsistentRead": True},
        )
        stubber.add_response("get_item", {"Item": store._protected_item(control)}, protected_read)
        assert store.read_control() == control
        # Stubber validates nested AttributeValue request shapes. The stateful
        # storage race tests additionally evaluate the condition's semantics.
        expected = {
            "TransactItems": [
                {
                    "ConditionCheck": {
                        "TableName": "fixture-protected",
                        "Key": {"pk": {"S": store.protected_key}},
                        "ConditionExpression": ANY,
                        "ExpressionAttributeValues": ANY,
                        "ExpressionAttributeNames": {"#data": "data"},
                    }
                },
                {
                    "Put": {
                        "TableName": "fixture-table",
                        "Item": store._item(control),
                        "ConditionExpression": ANY,
                        "ExpressionAttributeValues": ANY,
                        "ExpressionAttributeNames": {"#data": "data"},
                    }
                },
            ]
        }
        stubber.add_response("transact_write_items", {}, expected)
        store._commit(control, control)
        stubber.assert_no_pending_responses()


def test_administrator_initialization_transaction_uses_real_sdk_shapes(monkeypatch):
    s3, dynamo = aws("s3"), aws("dynamodb")
    store = SnapshotStore(
        s3, dynamo, "fixture-bucket", "fixture-table", "fixture-protected", GATEWAY
    )
    reference = ObjectRef(store.prefix + "policies/fixture.json", "v1", 2, digest(b"{}"))
    monkeypatch.setattr(store, "_put", lambda *args, **kwargs: reference)
    with Stubber(dynamo) as stubber:
        stubber.add_response(
            "transact_write_items",
            {},
            {
                "TransactItems": [
                    {
                        "Put": {
                            "TableName": "fixture-protected",
                            "Item": ANY,
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": "fixture-table",
                            "Item": ANY,
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                ]
            },
        )
        initialized = store.initialize({"epoch": 1})
        assert initialized.policy == reference
        stubber.assert_no_pending_responses()


def test_administrator_tombstone_transaction_uses_real_sdk_shapes():
    s3, dynamo = aws("s3"), aws("dynamodb")
    store = SnapshotStore(
        s3, dynamo, "fixture-bucket", "fixture-table", "fixture-protected", GATEWAY
    )
    control = ControlRecord(
        GATEWAY, policy=ObjectRef(store.prefix + "policies/fixture.json", "v1", 2, digest(b"{}"))
    )
    with Stubber(dynamo) as stubber:
        stubber.add_response(
            "transact_write_items",
            {},
            {
                "TransactItems": [
                    {
                        "ConditionCheck": {
                            "TableName": "fixture-table",
                            "Key": {"pk": {"S": store.key}},
                            "ConditionExpression": ANY,
                            "ExpressionAttributeValues": ANY,
                            "ExpressionAttributeNames": {"#data": "data"},
                        }
                    },
                    {
                        "Put": {
                            "TableName": "fixture-protected",
                            "Item": ANY,
                            "ConditionExpression": ANY,
                            "ExpressionAttributeValues": ANY,
                            "ExpressionAttributeNames": {"#data": "data"},
                        }
                    },
                ]
            },
        )
        deleted = store.tombstone(control)
        assert deleted.tombstoned
        assert deleted.protected_version == control.protected_version + 1
        stubber.assert_no_pending_responses()
