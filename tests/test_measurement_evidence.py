"""Offline evidence joins retain quality and original captured Gateway errors."""

import copy
import importlib.util
import json
import subprocess  # nosec B404
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/enrich_search_measurement.py"


def module():
    spec = importlib.util.spec_from_file_location("measurement_evidence", SCRIPT)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def data():
    row = {
        "arm": "before",
        "correlation_id": "request",
        "trace_root": "trace",
        "request_sequence": 0,
        "query_sha256": "query-hash",
        "phase": "first",
        "status": "ok",
        "backends": [{"backend": "direct_lambda", "platform_request_id": "platform"}],
    }
    search = {
        "operation": "search",
        "aws_request_id": "platform",
        "vector_availability": "available",
        "degraded": False,
        "_event_id": "event",
        "_log_stream": "stream",
        "_source_function": "search",
    }
    client = {
        **row,
        "record_state": "finished",
        "response": {"result": {"isError": False}},
        "response_sha256": "wire-hash",
        "http_status": 200,
    }
    return row, search, client


def test_quality_is_exactly_joined_to_original_event_and_does_not_mutate_inputs():
    row, search, client = data()
    result = module().enrich([row], {"before": [search]}, {"before": [client]})
    backend = result[0]["backends"][0]
    assert backend["vector_availability"] == "available"
    assert backend["degraded"] is False
    assert backend["quality_state"] == "OBSERVED"
    assert backend["quality_source"]["event_id"] == "event"
    assert "degraded" not in row["backends"][0]


@pytest.mark.parametrize("value", [None, "ecs", {}, [None], ["ecs"], [{}], [{"backend": []}]])
def test_malformed_backend_collection_has_a_validation_error(value):
    row, search, client = data()
    row["backends"] = value
    with pytest.raises(ValueError, match="backend"):
        module().enrich([row], {"before": [search]}, {"before": [client]})


def test_missing_backend_collection_has_a_validation_error():
    row, search, client = data()
    row.pop("backends")
    with pytest.raises(ValueError, match="backend"):
        module().enrich([row], {"before": [search]}, {"before": [client]})


@pytest.mark.parametrize("observations,state", [([], "MISSING"), ([0, 0], "AMBIGUOUS")])
def test_missing_or_duplicate_quality_is_unknown_not_assumed_healthy(observations, state):
    row, search, client = data()
    values = [dict(search, _event_id=f"event-{i}") for i, _ in enumerate(observations)]
    backend = module().enrich([row], {"before": values}, {"before": [client]})[0]["backends"][0]
    assert backend["quality_state"] == state
    assert backend["degraded"] is None
    assert backend["vector_availability"] is None


def test_partial_quality_is_not_promoted_to_a_complete_observation():
    row, search, client = data()
    search.pop("degraded")
    backend = module().enrich([row], {"before": [search]}, {"before": [client]})[0]["backends"][0]
    assert backend["quality_state"] == "INCOMPLETE"
    assert backend["degraded"] is None


def test_gateway_error_keeps_captured_text_code_and_wire_digest():
    row, _, client = data()
    row.update(status="mcp_error", backends=[])
    client.update(
        status="mcp_error",
        response={"error": {"code": -32603, "message": "An internal error occurred."}},
    )
    value = module().enrich([row], {"before": []}, {"before": [client]})[0]
    assert value["gateway_error"]["response"] == client["response"]
    assert value["gateway_error"]["response_sha256"] == "wire-hash"
    assert value["backends"] == []


def test_same_correlation_with_wrong_query_or_duplicate_client_is_rejected():
    row, search, client = data()
    with pytest.raises(ValueError, match="client"):
        module().enrich([row], {"before": [search]}, {"before": [client, client]})
    client["query_sha256"] = "another-query"
    with pytest.raises(ValueError, match="client"):
        module().enrich([row], {"before": [search]}, {"before": [client]})


def test_existing_quality_cannot_disagree_with_source():
    row, search, client = data()
    row["backends"][0]["vector_availability"] = "failed"
    with pytest.raises(ValueError, match="quality"):
        module().enrich([row], {"before": [search]}, {"before": [client]})


@pytest.mark.parametrize(
    "raw",
    [
        '{"degraded": true, "degraded": false}',
        '{"message": {"degraded": true, "degraded": false}}',
    ],
)
def test_duplicate_json_keys_cannot_hide_a_degraded_observation(raw):
    with pytest.raises(ValueError, match="duplicate"):
        module()._object(raw)


def test_one_search_event_cannot_be_reused_as_two_clients_quality_evidence():
    row, search, client = data()
    second = copy.deepcopy(row)
    second.update(correlation_id="another-request", trace_root="another-trace", request_sequence=1)
    other_client = dict(client, **second)
    with pytest.raises(ValueError, match="reused"):
        module().enrich([row, second], {"before": [search]}, {"before": [client, other_client]})


def test_duplicate_ledger_request_is_rejected_even_with_one_matching_client():
    row, search, client = data()
    with pytest.raises(ValueError, match="duplicate"):
        module().enrich([row, copy.deepcopy(row)], {"before": [search]}, {"before": [client]})


@pytest.mark.parametrize(
    "field", ["correlation_id", "trace_root", "query_sha256", "phase", "request_sequence"]
)
def test_missing_identity_in_both_files_is_not_evidence_of_a_match(field):
    row, search, client = data()
    row.pop(field)
    client.pop(field)
    with pytest.raises(ValueError, match="identity"):
        module().enrich([row], {"before": [search]}, {"before": [client]})


def test_cli_preserves_inputs_and_does_not_replace_existing_output(tmp_path):
    row, search, client = data()
    records = tmp_path / "original.jsonl"
    records.write_text(json.dumps(row) + "\n")
    original = records.read_bytes()
    (tmp_path / "measurement-before-finished.jsonl").write_text(json.dumps(client) + "\n")
    (tmp_path / "before-log-events-search.json").write_text(
        json.dumps(
            {
                "events": [
                    {"eventId": "event", "logStreamName": "stream", "message": json.dumps(search)}
                ]
            }
        )
    )
    output = tmp_path / "enriched.jsonl"
    command = [
        sys.executable,
        str(SCRIPT),
        "--records",
        str(records),
        "--exports",
        str(tmp_path),
        "--output",
        str(output),
    ]
    # Fixed local test program and fixture paths; argv is not request-controlled.
    # nosemgrep: dangerous-subprocess-use-audit
    assert subprocess.run(command, capture_output=True).returncode == 0  # nosec B603
    actual = output.read_bytes()
    assert json.loads(actual)["backends"][0]["quality_source"]["event_id"] == "event"
    # Fixed local test program and fixture paths; argv is not request-controlled.
    # nosemgrep: dangerous-subprocess-use-audit
    assert subprocess.run(command, capture_output=True).returncode != 0  # nosec B603
    assert records.read_bytes() == original
    assert output.read_bytes() == actual
