"""Capture construction is exercised with explicit stubs; no SDK session is opened."""

from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "collect_ecs_live_evidence.py"
NOW = datetime(2026, 10, 16, 8, tzinfo=UTC)
STACK_ID = (
    "arn:aws-cn:cloudformation:cn-north-1:111122223333:"
    "stack/fixture-dev/00000000-1111-2222-3333-444444444444"
)


def module():
    spec = importlib.util.spec_from_file_location("capture_script", SCRIPT)
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


class Clock:
    def __init__(self):
        self.seconds = 0.0
        self.wall_offset = 0.0

    def wall(self):
        return NOW + timedelta(seconds=self.seconds + self.wall_offset)

    def monotonic(self):
        return self.seconds


class Client:
    """Only the approved three CloudFormation API methods exist."""

    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.mutate = lambda operation, result, count: None
        self.template = {
            "Resources": {
                "One": {"Type": "AWS::Logs::LogGroup"},
                "Two": {"Type": "AWS::Logs::LogGroup"},
            }
        }
        self.stack = {
            "StackId": STACK_ID,
            "StackName": "fixture-dev",
            "StackStatus": "UPDATE_COMPLETE",
            "CreationTime": NOW - timedelta(days=1),
            "LastUpdatedTime": NOW - timedelta(minutes=5),
            "Parameters": [],
            "Outputs": [],
        }

    def _response(self, operation, request, result):
        self.calls.append((operation, deepcopy(request)))
        self.clock.seconds += 1.0
        result = deepcopy(result)
        result["ResponseMetadata"] = {"RequestId": str(len(self.calls))}
        self.mutate(operation, result, len(self.calls))
        return result

    def describe_stacks(self, **kwargs):
        return self._response("describe_stacks", kwargs, {"Stacks": [self.stack]})

    def get_template(self, **kwargs):
        return self._response("get_template", kwargs, {"TemplateBody": self.template})

    def list_stack_resources(self, **kwargs):
        name = "Two" if "NextToken" in kwargs else "One"
        result = {
            "StackResourceSummaries": [
                {
                    "LogicalResourceId": name,
                    "PhysicalResourceId": name.lower(),
                    "ResourceType": "AWS::Logs::LogGroup",
                    "ResourceStatus": "UPDATE_COMPLETE",
                    "LastUpdatedTimestamp": NOW,
                }
            ]
        }
        if "NextToken" not in kwargs:
            result["NextToken"] = "page-two"
        return self._response("list_stack_resources", kwargs, result)


def collect(client=None, clock=None):
    clock = clock or Clock()
    client = client or Client(clock)
    return module().collect(client, STACK_ID, wall_clock=clock.wall, monotonic=clock.monotonic)


def test_capture_is_directly_accepted_by_existing_offline_validator():
    from infra.live_evidence import template_digest, validate_live_evidence

    clock = Clock()
    client = Client(clock)
    template, capture = collect(client, clock)
    # Actual boto3 datetimes survive both output JSON serialization and validation.
    capture = json.loads(json.dumps(capture, allow_nan=False))
    checked = validate_live_evidence(template, capture, STACK_ID, now=clock.wall())
    assert set(checked.inventory) == {"One", "Two"}
    assert capture["version"] == 1
    assert capture["expected_stack_id"] == capture["stack_id"] == STACK_ID
    assert capture["account"] == "111122223333"
    assert capture["region"] == "cn-north-1"
    assert capture["captured_at"] == NOW.isoformat()
    assert capture["expires_at"] == (NOW + timedelta(seconds=900)).isoformat()
    assert capture["collection_completed_at"] == clock.wall().isoformat()
    assert capture["template_sha256"] == template_digest(template)
    assert client.calls == [
        ("describe_stacks", {"StackName": STACK_ID}),
        ("get_template", {"StackName": STACK_ID, "TemplateStage": "Original"}),
        ("list_stack_resources", {"StackName": STACK_ID}),
        ("list_stack_resources", {"StackName": STACK_ID, "NextToken": "page-two"}),
        ("describe_stacks", {"StackName": STACK_ID}),
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"StackStatus": "UPDATE_IN_PROGRESS"},
        {"StackId": STACK_ID + "-other"},
        {"LastUpdatedTime": NOW},
        {"Outputs": [{"OutputKey": "Endpoint", "OutputValue": "changed"}]},
        {"Parameters": [{"ParameterKey": "Config", "ParameterValue": "changed"}]},
    ],
)
def test_final_describe_rejects_a_stack_that_changed_during_collection(change):
    clock = Clock()
    client = Client(clock)

    def mutate(operation, result, count):
        if operation == "describe_stacks" and count > 1:
            result["Stacks"][0].update(change)

    client.mutate = mutate
    with pytest.raises(ValueError, match="changed|stable|identity"):
        collect(client, clock)


@pytest.mark.parametrize("failure", ["cycle", "truncated", "wrong-type", "duplicate"])
def test_capture_rejects_bad_inventory_instead_of_emitting_a_partial_bundle(failure):
    clock = Clock()
    client = Client(clock)

    def mutate(operation, result, count):
        if operation == "list_stack_resources":
            if failure == "cycle":
                result["NextToken"] = "page-two"
            elif failure == "truncated":
                result.pop("NextToken", None)
            elif failure == "wrong-type":
                result["StackResourceSummaries"][0]["ResourceType"] = "AWS::S3::Bucket"
            elif count == 4:
                result["StackResourceSummaries"][0]["LogicalResourceId"] = "One"

    client.mutate = mutate
    with pytest.raises(ValueError, match="inventory|pagination|token"):
        collect(client, clock)
    assert len(client.calls) <= 5


@pytest.mark.parametrize("failure", ["elapsed", "wall-forward", "wall-backward"])
def test_collection_rejects_elapsed_or_wall_clock_outside_original_window(failure):
    clock = Clock()
    client = Client(clock)

    def mutate(operation, result, count):
        if count == 4:
            if failure == "elapsed":
                clock.seconds = 901.0
            else:
                clock.wall_offset = 901.0 if failure == "wall-forward" else -10.0

    client.mutate = mutate
    with pytest.raises(ValueError, match="900|fresh|clock"):
        collect(client, clock)
    assert len(client.calls) == 4  # No further request after the expired budget.


@pytest.mark.parametrize("boundary_clock", ["elapsed", "wall", "both"])
def test_collection_accepts_exact_900_second_clock_boundary(boundary_clock):
    from infra.live_evidence import validate_live_evidence

    clock = Clock()
    client = Client(clock)

    def complete_at_boundary(operation, result, count):
        if count == 5:
            assert operation == "describe_stacks"
            clock.seconds = 899.0 if boundary_clock == "wall" else 900.0
            clock.wall_offset = (
                -1.0 if boundary_clock == "elapsed" else 1.0 if boundary_clock == "wall" else 0.0
            )

    client.mutate = complete_at_boundary
    template, capture = collect(client, clock)
    assert len(client.calls) == 5
    assert capture["captured_at"] == NOW.isoformat()
    assert capture["collection_completed_at"] == clock.wall().isoformat()
    assert capture["expires_at"] == (NOW + timedelta(seconds=900)).isoformat()
    assert set(validate_live_evidence(template, capture, STACK_ID, now=clock.wall()).inventory) == {
        "One",
        "Two",
    }
    if boundary_clock in {"wall", "both"}:
        assert capture["collection_completed_at"] == capture["expires_at"]
        # Capture acceptance at the exact boundary leaves no later usable
        # freshness window; this test does not promise an execution reserve.
        with pytest.raises(ValueError, match="fresh"):
            validate_live_evidence(
                template, capture, STACK_ID, now=clock.wall() + timedelta(microseconds=1)
            )


def test_monotonic_deadline_rejects_elapsed_time_even_if_wall_clock_is_reset():
    clock = Clock()
    client = Client(clock)

    def mutate(operation, result, count):
        if count == 4:
            clock.seconds = 901.0
            clock.wall_offset = -900.0

    client.mutate = mutate
    with pytest.raises(ValueError, match="900"):
        collect(client, clock)


@pytest.mark.parametrize("stage", ["initial", "final"])
def test_describe_rejects_extra_stacks_or_unfollowed_pages(stage):
    clock = Clock()
    client = Client(clock)

    def mutate(operation, result, count):
        if operation == "describe_stacks" and (count == 1) == (stage == "initial"):
            result["NextToken"] = "another-stack"

    client.mutate = mutate
    with pytest.raises(ValueError, match="exactly|changed"):
        collect(client, clock)


def test_original_template_json_string_is_supported_without_new_dependencies():
    clock = Clock()
    client = Client(clock)
    expected = deepcopy(client.template)
    client.template = json.dumps(client.template)
    template, capture = collect(client, clock)
    assert template == expected
    assert capture["get_template"]["request"]["TemplateStage"] == "Original"


@pytest.mark.parametrize("body", ['{"Resources":{},"Resources":{}}', "Resources:\n  One: {}"])
def test_non_json_or_duplicate_template_keys_are_not_silently_normalized(body):
    clock = Clock()
    client = Client(clock)
    client.template = body
    with pytest.raises(ValueError):
        collect(client, clock)


@pytest.mark.parametrize(
    "stack_id",
    ["fixture-dev", STACK_ID.replace("111122223333", "123"), STACK_ID + "\n"],
)
def test_full_stack_arn_is_required_before_any_client_operation(stack_id):
    clock = Clock()
    client = Client(clock)
    with pytest.raises(ValueError, match="full CloudFormation"):
        module().collect(client, stack_id, wall_clock=clock.wall, monotonic=clock.monotonic)
    assert client.calls == []


def test_cli_requires_collect_before_constructing_sdk_client(tmp_path, monkeypatch):
    loaded = module()
    monkeypatch.setattr(loaded, "_client", lambda *_: pytest.fail("SDK session opened"))
    with pytest.raises(SystemExit) as error:
        loaded.main(["--expected-stack-id", STACK_ID, "--output-dir", str(tmp_path / "capture")])
    assert error.value.code == 2
    assert not (tmp_path / "capture").exists()


def test_cli_help_is_offline(monkeypatch):
    loaded = module()
    monkeypatch.setattr(loaded, "_client", lambda *_: pytest.fail("SDK session opened"))
    with pytest.raises(SystemExit) as error:
        loaded.main(["--help"])
    assert error.value.code == 0


def test_cli_never_overwrites_an_existing_capture(tmp_path, monkeypatch):
    loaded = module()
    monkeypatch.setattr(loaded, "_client", lambda *_: pytest.fail("SDK session opened"))
    saved = tmp_path / "live-evidence.json"
    saved.write_text("old capture")
    with pytest.raises(SystemExit) as error:
        loaded.main(["--collect", "--expected-stack-id", STACK_ID, "--output-dir", str(tmp_path)])
    assert error.value.code == 2
    assert saved.read_text() == "old capture"


def test_cli_writes_two_valid_files_using_only_explicit_stub(tmp_path, monkeypatch, capsys):
    loaded = module()
    clock = Clock()
    client = Client(clock)
    requested = []
    original_collect = loaded.collect

    def stub_client(region, profile):
        requested.append((region, profile))
        return client

    monkeypatch.setattr(loaded, "_client", stub_client)
    monkeypatch.setattr(
        loaded,
        "collect",
        lambda connection, stack_id: original_collect(
            connection, stack_id, wall_clock=clock.wall, monotonic=clock.monotonic
        ),
    )
    target = tmp_path / "capture"
    assert (
        loaded.main(
            [
                "--collect",
                "--expected-stack-id",
                STACK_ID,
                "--profile",
                "explicit-test-profile",
                "--output-dir",
                str(target),
            ]
        )
        == 0
    )
    assert requested == [("cn-north-1", "explicit-test-profile")]
    assert sorted(path.name for path in target.iterdir()) == [
        "live-evidence.json",
        "live-template.json",
    ]
    capture = json.loads((target / "live-evidence.json").read_text())
    assert capture["captured_at"] == NOW.isoformat()
    assert "does not authorize" in capsys.readouterr().out


def test_sdk_factory_has_only_explicit_cloudformation_region_and_bounded_reads(monkeypatch):
    import boto3

    loaded = module()
    calls = []

    class Session:
        def __init__(self, **kwargs):
            calls.append(("session", kwargs))

        def client(self, service, **kwargs):
            calls.append((service, kwargs))
            return "stub"

    monkeypatch.setattr(boto3, "Session", Session)
    assert loaded._client("cn-north-1", "fixture") == "stub"
    assert calls[0] == ("session", {"profile_name": "fixture", "region_name": "cn-north-1"})
    assert calls[1][0] == "cloudformation"
    assert calls[1][1]["region_name"] == "cn-north-1"
    config = calls[1][1]["config"]
    assert config.connect_timeout == 5
    assert config.read_timeout == 20
    assert config.retries == {"mode": "standard", "total_max_attempts": 3}
