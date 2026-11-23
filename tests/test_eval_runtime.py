"""Real-mode prerequisites are tested with mocked subprocesses only."""

import json
import subprocess  # nosec B404

import pytest

from semantic_eval.runtime import Credentials, RealModeUnavailable, load_credentials, preflight_real

IDENTITY = {"Account": "synthetic-account", "Arn": "synthetic-arn", "UserId": "synthetic-user"}
SECRET = {"base_url": "https://offline.example/tenant/v1", "api_key": "fixture-key"}


def completed(payload: object, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["aws"],
        returncode=returncode,
        stdout=json.dumps(payload),
        stderr="private fixture text",
    )


def test_load_uses_only_designated_profile_and_secret_without_printing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = []
    results = [completed(IDENTITY), completed({"SecretString": json.dumps(SECRET)})]

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return results.pop(0)

    monkeypatch.setattr(subprocess, "run", run)
    credentials = load_credentials()
    assert credentials.base_url == SECRET["base_url"]
    assert credentials.api_key == SECRET["api_key"]
    assert repr(credentials) == "Credentials()"
    assert len(calls) == 2
    common = [
        "--profile",
        "sample-semantic-eval",
        "--region",
        "cn-north-1",
        "--output",
        "json",
        "--no-cli-pager",
    ]
    assert calls[0][0] == ["aws", "sts", "get-caller-identity", *common]
    assert calls[1][0] == [
        "aws",
        "secretsmanager",
        "get-secret-value",
        "--secret-id",
        "semsearch/model-api",
        *common,
    ]
    for command, kwargs in calls:
        assert SECRET["api_key"] not in repr(command)
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is False
        assert 0 < kwargs["timeout"] <= 30
        assert not kwargs.get("shell", False)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize(
    "failure",
    [
        completed({"private": "fixture-key"}, 1),
        completed({}),
        completed({"Account": "synthetic-account", "Arn": "", "UserId": "synthetic-user"}),
        subprocess.CompletedProcess(["aws"], 0, stdout="fixture-key", stderr="fixture-key"),
        FileNotFoundError("fixture-key"),
        subprocess.TimeoutExpired(["aws"], 20, output="fixture-key", stderr="fixture-key"),
    ],
)
def test_failed_sts_does_not_attempt_secret_or_leak_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: object,
) -> None:
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(RealModeUnavailable) as error:
        preflight_real()
    assert len(calls) == 1
    assert calls[0][1:3] == ["sts", "get-caller-identity"]
    assert str(error.value) == "real model prerequisites are unavailable"
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize(
    "secret_response",
    [
        {},
        {"SecretBinary": "fixture-key"},
        {"SecretString": "fixture-key"},
        {"SecretString": json.dumps([])},
        {"SecretString": json.dumps({"base_url": SECRET["base_url"]})},
        {"SecretString": json.dumps({**SECRET, "api_key": " "})},
        {"SecretString": json.dumps({**SECRET, "api_key": "fixture\nkey"})},
        {"SecretString": json.dumps({**SECRET, "base_url": "http://offline.example"})},
        {"SecretString": json.dumps({**SECRET, "base_url": "https://key@offline.example"})},
        {"SecretString": json.dumps({**SECRET, "base_url": "https://offline.example?key=value"})},
    ],
)
def test_invalid_secret_is_safe_and_does_not_trigger_fallback(
    monkeypatch: pytest.MonkeyPatch,
    secret_response: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    results = [completed(IDENTITY), completed(secret_response)]
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return results.pop(0)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(RealModeUnavailable) as error:
        load_credentials()
    assert len(calls) == 2
    assert str(error.value) == "real model prerequisites are unavailable"
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_secret_process_failure_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    results = [completed(IDENTITY), completed({"error": "fixture-key"}, 1)]

    def run(command, **kwargs):
        return results.pop(0)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(RealModeUnavailable, match="^real model prerequisites are unavailable$"):
        load_credentials()
    assert results == []


def test_credentials_repr_never_contains_either_secret_field() -> None:
    credentials = Credentials(base_url=SECRET["base_url"], api_key=SECRET["api_key"])
    assert SECRET["base_url"] not in repr(credentials)
    assert SECRET["api_key"] not in repr(credentials)


def test_real_mode_uses_configured_profile_region_and_secret(monkeypatch) -> None:
    monkeypatch.setenv("SEMANTIC_EVAL_AWS_PROFILE", "sample-evaluation")
    monkeypatch.setenv("SEMANTIC_EVAL_AWS_REGION", "cn-northwest-1")
    monkeypatch.setenv("SEMANTIC_EVAL_SECRET_ID", "sample/model")
    calls = []
    results = [completed(IDENTITY), completed({"SecretString": json.dumps(SECRET)})]

    def run(command, **kwargs):
        calls.append(command)
        return results.pop(0)

    monkeypatch.setattr(subprocess, "run", run)
    load_credentials()
    for command in calls:
        assert command[command.index("--profile") + 1] == "sample-evaluation"
        assert command[command.index("--region") + 1] == "cn-northwest-1"
    assert calls[1][calls[1].index("--secret-id") + 1] == "sample/model"


@pytest.mark.parametrize(
    "name,value",
    [
        ("SEMANTIC_EVAL_AWS_PROFILE", ""),
        ("SEMANTIC_EVAL_AWS_PROFILE", "--endpoint-url"),
        ("SEMANTIC_EVAL_AWS_REGION", " "),
        ("SEMANTIC_EVAL_SECRET_ID", "\ninvalid"),
    ],
)
def test_invalid_real_mode_selection_fails_before_aws(monkeypatch, name, value) -> None:
    monkeypatch.setenv(name, value)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(RealModeUnavailable):
        load_credentials()
    assert calls == []
