"""Synthetic Gateway metadata remains distinct from runtime argument enforcement."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from infra.config import DeploymentConfig
from infra.stack import documents

from semantic_core.catalog import GatewayIdentity, ToolIdentity, ToolRecord

CAPTURE = Path(__file__).parents[1] / "fixtures/service/search-schema.synthetic.json"


def search_schema(profile="gateway"):
    captured = json.loads(CAPTURE.read_text())
    assert captured["status"] == "READY"
    schema = captured["tool"]["inputSchema"]
    if profile == "constrained":
        schema["additionalProperties"] = False
        schema["properties"]["query"].update(minLength=1, maxLength=2048)
        schema["properties"]["_auth_context"]["maxLength"] = 8192
    return schema


def load_with_schema(tmp_path, schema):
    # Construct the candidate before validation; this remains a focused red
    # regression even when the checked-in example changes to the supported form.
    candidate = json.loads(Path("infra/config.example.json").read_text())
    candidate["search_descriptor"]["inputSchema"] = copy.deepcopy(schema)
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(candidate))
    return DeploymentConfig.from_file(path)


@pytest.mark.parametrize("profile", ["gateway", "constrained"])
def test_search_metadata_profiles_preserve_supplied_schema(tmp_path, profile):
    schema = search_schema(profile)
    config = load_with_schema(tmp_path, schema)
    for operation in ("search", "request"):
        settings = json.loads(documents(config)[operation])["settings"]
        descriptor = json.loads(settings["SEMSEARCH_SEARCH_DESCRIPTOR_JSON"])
        assert descriptor["inputSchema"] == schema
        assert descriptor == config.search_descriptor
    assert schema == search_schema(profile)


@pytest.mark.parametrize("profile", ["gateway", "constrained"])
@pytest.mark.parametrize(
    ("path", "value"),
    [
        ((), "新增的根说明"),
        (("properties", "query"), "新增的查询说明"),
        (("properties", "_auth_context"), None),
        (("properties", "_auth_context"), "改写的封套说明"),
        (
            ("properties", "_auth_context"),
            "Server-managed signed context. Clients must omit this field. ",
        ),
    ],
)
def test_search_schema_rejects_description_changes_that_alter_digest(
    tmp_path, profile, path, value
):
    schema = search_schema(profile)
    config = load_with_schema(tmp_path, schema)
    identity = ToolIdentity(
        GatewayIdentity(config.account, config.region, config.gateway_id),
        config.search_target_id,
        config.search_incarnation,
        config.search_tool_name,
    )
    original = ToolRecord.from_descriptor(identity, config.search_descriptor)
    parent = schema
    for part in path:
        parent = parent[part]
    if value is None:
        parent.pop("description")
    else:
        parent["description"] = value
    changed = ToolRecord.from_descriptor(
        identity, config.search_descriptor | {"inputSchema": schema}
    )
    # 这些说明文字改变的是授权摘要，必须在部署配置阶段拒绝。
    assert changed.schema_digest != original.schema_digest
    with pytest.raises(ValueError):
        load_with_schema(tmp_path, schema)


@pytest.mark.parametrize(
    ("profile", "path", "value"),
    [
        ("constrained", ("type",), "array"),
        ("gateway", ("type",), None),
        ("gateway", ("properties", "query", "type"), "number"),
        ("gateway", ("properties", "_auth_context", "type"), "object"),
        ("gateway", ("properties", "identity"), {"type": "string"}),
        ("gateway", ("required",), []),
        ("gateway", ("required",), ["query", "_auth_context"]),
        ("gateway", ("additionalProperties",), True),
        ("constrained", ("additionalProperties",), 0),
        ("constrained", ("properties", "query", "minLength"), 0),
        ("constrained", ("properties", "query", "minLength"), True),
        ("constrained", ("properties", "query", "maxLength"), 2049),
        ("constrained", ("properties", "query", "maxLength"), 2048.0),
        ("constrained", ("properties", "_auth_context", "maxLength"), 8193),
        ("constrained", ("properties", "_auth_context", "maxLength"), 8192.0),
        ("constrained", ("properties", "_auth_context", "minLength"), 9000),
        ("constrained", ("properties", "_auth_context", "const"), "caller-controlled"),
        ("gateway", ("properties", "query", "enum"), ["one query"]),
        ("gateway", ("properties", "_auth_context", "default"), "caller-controlled"),
        ("gateway", ("properties", "_auth_context", "description"), {"identity": "caller"}),
        ("gateway", ("allOf",), [{"required": ["_auth_context"]}]),
        ("gateway", ("properties", "query", "maxLength"), 2048),
    ],
)
def test_search_metadata_rejects_unapproved_or_contradictory_schema(tmp_path, profile, path, value):
    schema = search_schema(profile)
    parent = schema
    for part in path[:-1]:
        parent = parent[part]
    parent[path[-1]] = value
    with pytest.raises(ValueError):
        load_with_schema(tmp_path, schema)
