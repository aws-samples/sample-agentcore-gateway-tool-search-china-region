"""The synthetic corpus is a reproducible regression fixture, never a holdout."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from semantic_core.catalog import GatewayIdentity, ToolIdentity, ToolRecord
from semantic_eval.dataset import generate_dataset, load_dataset, write_dataset


def test_generation_is_reproducible_and_seeded(tmp_path: Path) -> None:
    first = generate_dataset()
    second = generate_dataset()
    assert first == second
    assert first != generate_dataset(seed=first.seed + 1)
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    write_dataset(first, first_path)
    write_dataset(second, second_path)
    assert first_path.read_bytes() == second_path.read_bytes()
    assert load_dataset(first_path) == first
    payload = json.loads(first_path.read_text())
    assert payload["schema_version"] == "synthetic-v1"
    assert payload["synthetic"] is True
    assert payload["quality_acceptance"] is False
    assert "SYNTHETIC" in payload["notice"]
    assert payload["example_provider"] == "mock-llm/fixture-v1"


def test_catalog_has_confusable_crud_tools_and_valid_original_descriptors() -> None:
    dataset = generate_dataset()
    assert len(dataset.tools) == 500
    assert len({tool.id for tool in dataset.tools}) == 500
    assert set(Counter(tool.gateway_id for tool in dataset.tools).values()) == {100}
    assert len({tool.system for tool in dataset.tools}) == 5
    assert set(tool.language for tool in dataset.tools) == {"zh", "en", "mixed"}
    families = Counter((tool.gateway_id, tool.family) for tool in dataset.tools)
    assert set(families.values()) == {4}
    for tool in dataset.tools:
        name = tool.descriptor["name"]
        assert isinstance(name, str)
        identity = ToolIdentity(
            GatewayIdentity("synthetic", "local", tool.gateway_id),
            tool.system,
            "synthetic-v1",
            name,
        )
        ToolRecord.from_descriptor(identity, tool.descriptor, tool.examples)
        assert len(tool.examples) == 6


def test_queries_cover_m17_language_permission_and_authorized_cardinality() -> None:
    dataset = generate_dataset()
    assert len(dataset.queries) >= 300
    languages = Counter(query.language for query in dataset.queries)
    assert languages["zh"] / len(dataset.queries) >= 0.5
    assert set(languages) == {"zh", "en", "mixed"}
    assert {query.permission_group for query in dataset.queries} == {
        "0%",
        "1%",
        "10%",
        "100%",
    }
    for language in languages:
        cardinalities = {
            min(len(set(query.relevant_ids) & set(query.allowed_ids)), 6)
            for query in dataset.queries
            if query.language == language
        }
        assert cardinalities == set(range(7))
    gateways = {
        gateway: {tool.id for tool in dataset.tools if tool.gateway_id == gateway}
        for gateway in {tool.gateway_id for tool in dataset.tools}
    }
    for query in dataset.queries:
        catalog_ids = gateways[query.gateway_id]
        assert set(query.allowed_ids) <= catalog_ids
        assert set(query.relevant_ids) <= catalog_ids
        assert len(query.allowed_ids) == len(set(query.allowed_ids))
        assert len(query.relevant_ids) == len(set(query.relevant_ids))
        assert len(query.allowed_ids) / len(catalog_ids) == (
            int(query.permission_group.removesuffix("%")) / 100
        )
    assert any(set(query.relevant_ids) - set(query.allowed_ids) for query in dataset.queries)
    assert any(not query.relevant_ids for query in dataset.queries)
    assert any(not query.allowed_ids for query in dataset.queries)


def test_semantic_labels_are_derived_from_requested_system_resource_and_actions() -> None:
    dataset = generate_dataset()
    for query in dataset.queries:
        tags = dict(tag.split(":", 1) for tag in query.tags if ":" in tag)
        if tags.get("intent") == "outside_catalog":
            assert not query.relevant_ids
            continue
        expected = {
            tool.id
            for tool in dataset.tools
            if tool.gateway_id == query.gateway_id
            and tool.system == tags["system"]
            and tool.family.split(":", 1)[1] in tags["resources"].split(",")
            and str(tool.descriptor["name"]).split("___", 1)[1].split("_", 1)[0]
            in tags["actions"].split(",")
        }
        assert set(query.relevant_ids) == expected


def test_query_truth_does_not_reuse_generated_examples() -> None:
    dataset = generate_dataset()
    examples = {example for tool in dataset.tools for example in tool.examples}
    assert not examples & {query.text for query in dataset.queries}
    assert all("task" not in query.text for query in dataset.queries)


@pytest.mark.parametrize("field", ["synthetic", "quality_acceptance", "schema_version"])
def test_loader_rejects_dataset_provenance_changes(tmp_path: Path, field: str) -> None:
    path = tmp_path / "dataset.json"
    write_dataset(generate_dataset(), path)
    payload = json.loads(path.read_text())
    payload[field] = {"synthetic": False, "quality_acceptance": True, "schema_version": "v2"}[field]
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        load_dataset(path)


@pytest.mark.parametrize("bad_reference", ["unknown", "cross_gateway"])
def test_loader_rejects_unknown_or_cross_gateway_truth(tmp_path: Path, bad_reference: str) -> None:
    path = tmp_path / "dataset.json"
    write_dataset(generate_dataset(), path)
    payload = json.loads(path.read_text())
    reference = (
        "unknown"
        if bad_reference == "unknown"
        else next(
            tool["id"]
            for tool in payload["tools"]
            if tool["gateway_id"] != payload["queries"][0]["gateway_id"]
        )
    )
    payload["queries"][0]["relevant_ids"] = [reference]
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        load_dataset(path)


def test_versioned_fixture_matches_default_generator(tmp_path: Path) -> None:
    path = tmp_path / "regenerated.json"
    write_dataset(generate_dataset(), path)
    fixture = Path(__file__).parents[1] / "data" / "synthetic-v1.json"
    assert fixture.read_bytes() == path.read_bytes()
