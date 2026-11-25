"""Offline regressions for public release review findings."""

import hashlib
import hmac
import importlib.util
import json
import logging
import os
import runpy
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from semantic_core.providers import ErrorCode, ProviderError, RequestContext
from semantic_eval import runtime, tuning_runtime
from semantic_service.models import ModelSettings, RotatingModelProviders
from semantic_service.secrets import SecretsCache

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def restore_script_process_settings(monkeypatch):
    previous = logging.root.manager.disable
    monkeypatch.setenv("AWS_REGION", "fixture-region")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "fixture-region")
    yield
    logging.disable(previous)


@pytest.mark.parametrize(("arguments", "code"), [(["--help"], 0), ([], 2), (["--bad"], 2)])
def test_reference_argparse_exit_codes(arguments, code):
    result = subprocess.run(
        [sys.executable, str(ROOT / "reports/real/run_reference.py"), *arguments],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == code
    assert "Run refused" not in result.stdout + result.stderr


def reference_arguments(tmp_path, embedding="example-embedding-model"):
    return [
        "run_reference.py",
        "--mode",
        "real",
        "--output-dir",
        str(tmp_path / "output"),
        "--embedding-model",
        embedding,
        "--chat-model",
        "example-chat-model",
    ]


@pytest.mark.parametrize("missing_switch", [False, True])
def test_reference_configuration_failure_reports_original_error(
    tmp_path, monkeypatch, capsys, missing_switch
):
    monkeypatch.setattr(
        sys,
        "argv",
        reference_arguments(tmp_path, "example-embedding-model" if missing_switch else " "),
    )
    monkeypatch.delenv("SEMANTIC_EVAL_REAL", raising=False)
    monkeypatch.setattr(runtime, "load_credentials", lambda: pytest.fail("credentials touched"))
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(ROOT / "reports/real/run_reference.py"), run_name="__main__")
    assert error.value.code == 1
    output = capsys.readouterr()
    expected = "SEMANTIC_EVAL_REAL=1" if missing_switch else "explicit version identifiers"
    assert expected in output.err + output.out
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("failure", [runtime.RealModeUnavailable, ValueError, RuntimeError])
def test_reference_credential_failure_keeps_operational_classification(
    tmp_path, monkeypatch, capsys, failure
):
    monkeypatch.setattr(sys, "argv", reference_arguments(tmp_path))
    monkeypatch.setenv("SEMANTIC_EVAL_REAL", "1")
    original_subprocess = runtime.subprocess.run

    def unavailable():
        if failure is runtime.RealModeUnavailable:
            raise failure()
        raise failure("private credential details")

    monkeypatch.setattr(runtime, "load_credentials", unavailable)
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(ROOT / "reports/real/run_reference.py"), run_name="__main__")
    assert error.value.code == 1
    status = json.loads((tmp_path / "output/run-status.json").read_text())
    assert status["reason"] == failure.__name__
    assert runtime.subprocess.run is original_subprocess
    output = capsys.readouterr()
    assert "private credential details" not in output.out + output.err
    assert "check the explicit configuration" not in output.out + output.err


@pytest.mark.parametrize("base", [None, 5, "", "  "])
@pytest.mark.parametrize("environment_base", [None, "", "  "])
def test_missing_or_invalid_secret_endpoint_is_provider_unavailable(base, environment_base):
    settings = {
        "MODEL_ALLOWED_HOSTS": "models.example.com",
        "MODEL_EMBEDDING_MODEL": "example-embedding-model",
        "MODEL_CHAT_MODEL": "example-chat-model",
    }
    if environment_base is not None:
        settings["MODEL_BASE_URL"] = environment_base

    class SecretClient:
        def get_secret_value(self, **kwargs):
            value = {"api_key": "fixture"}
            if base is not None:
                value["base_url"] = base
            return {"VersionId": "v1", "SecretString": json.dumps(value)}

    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("HTTP touched"))
    ) as client:
        providers = RotatingModelProviders(
            SecretsCache(SecretClient()), ModelSettings.from_env(settings), client
        )
        with pytest.raises(ProviderError) as error:
            providers.embedding.embed(
                ["query"], "query", RequestContext("fixture", time.monotonic() + 10)
            )
        assert error.value.code is ErrorCode.UNAVAILABLE


@pytest.mark.parametrize("base", ["", " \t "])
def test_blank_endpoint_override_uses_secret(base):
    settings = ModelSettings.from_env(
        {
            "MODEL_ALLOWED_HOSTS": "models.example.com",
            "MODEL_BASE_URL": base,
            "MODEL_EMBEDDING_MODEL": "example-embedding-model",
            "MODEL_CHAT_MODEL": "example-chat-model",
        }
    )
    assert settings.base_url is None


def test_endpoint_identity_normalizes_equivalent_urls_and_uses_private_salt(tmp_path):
    salt = tmp_path / ".local/endpoint-salt"
    variants = [
        "https://models.example.com/v1",
        "https://models.example.com/v1/",
        "HTTPS://MODELS.EXAMPLE.COM:443/v1/",
    ]
    fingerprints = [tuning_runtime.endpoint_fingerprint(url, salt, create=True) for url in variants]
    assert len(set(fingerprints)) == 1
    key = salt.read_bytes()
    assert len(key) == 32
    assert salt.stat().st_mode & 0o777 == 0o600
    assert fingerprints[0] == hmac.new(key, variants[0].encode(), hashlib.sha256).hexdigest()
    assert fingerprints[0] != hashlib.sha256(variants[0].encode()).hexdigest()
    assert tuning_runtime.endpoint_fingerprint(variants[0], salt) == fingerprints[0]
    for other in [
        "https://other.example.com/v1",
        "https://models.example.com/v2",
        "https://models.example.com:8443/v1",
    ]:
        assert tuning_runtime.endpoint_fingerprint(other, salt) != fingerprints[0]
    other_salt = tmp_path / ".local/another-salt"
    assert (
        tuning_runtime.endpoint_fingerprint(variants[0], other_salt, create=True) != fingerprints[0]
    )
    assert other_salt.read_bytes() != key


@pytest.mark.parametrize("state", ["missing", "short", "public", "symlink"])
def test_endpoint_identity_rejects_missing_or_unsafe_salt(tmp_path, state):
    salt = tmp_path / "salt"
    if state != "missing":
        salt.write_bytes(b"x" * (4 if state == "short" else 32))
        salt.chmod(0o644 if state == "public" else 0o600)
    if state == "symlink":
        salt.rename(tmp_path / "target")
        salt.symlink_to(tmp_path / "target")
    with pytest.raises(tuning_runtime.RunStopped, match="endpoint identity salt"):
        tuning_runtime.endpoint_fingerprint("https://models.example.com/v1", salt)


@pytest.mark.parametrize("suffix", ["?", "#", "?query", "#fragment"])
def test_endpoint_identity_rejects_route_separators_before_creating_salt(tmp_path, suffix):
    salt = tmp_path / ".local/endpoint-salt"
    with pytest.raises(tuning_runtime.RunStopped, match="validated HTTPS endpoint"):
        tuning_runtime.endpoint_fingerprint(
            "https://models.example.com/v1" + suffix, salt, create=True
        )
    assert not salt.exists()


def test_tuning_freeze_accepts_normalized_url_but_rejects_lost_salt(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("fix2_tuning", ROOT / "scripts/run_tuning.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    from semantic_eval.dataset import generate_dataset

    monkeypatch.setattr(runner, "load_dataset", lambda _: generate_dataset())
    urls = iter(["https://models.example.com/v1", "HTTPS://MODELS.EXAMPLE.COM:443/v1/"])
    monkeypatch.setattr(
        runner, "load_credentials", lambda: runtime.Credentials(next(urls), "fixture")
    )
    monkeypatch.setattr(runner, "dev", lambda *args: None)
    monkeypatch.setenv("SEMANTIC_EVAL_REAL", "1")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_tuning.py",
            "--phase",
            "dev",
            "--real",
            "--output-dir",
            str(tmp_path / "output"),
            "--embedding-model",
            "example-embedding-model",
            "--chat-model",
            "example-chat-model",
            "--rerank-model",
            "example-rerank-model",
            "--rerank-path",
            "/rerank",
        ],
    )
    assert runner.main() == 0
    before = (tmp_path / "output/model-config.json").read_bytes()
    assert runner.main() == 0
    assert (tmp_path / "output/model-config.json").read_bytes() == before
    assert b"models.example.com" not in before and b"fixture" not in before
    monkeypatch.setattr(runner.httpx, "Client", lambda **kwargs: pytest.fail("HTTP client touched"))
    monkeypatch.setattr(runner, "DocumentCache", lambda *args: pytest.fail("cache touched"))
    for suffix in ("?", "#"):
        monkeypatch.setattr(
            runner,
            "load_credentials",
            lambda suffix=suffix: runtime.Credentials(
                "https://models.example.com/v1" + suffix, "fixture"
            ),
        )
        assert runner.main() == 1
        assert (tmp_path / "output/model-config.json").read_bytes() == before
    salts = list((tmp_path / ".local").glob("*salt*"))
    assert len(salts) == 1
    salts[0].unlink()
    monkeypatch.setattr(
        runner,
        "load_credentials",
        lambda: runtime.Credentials("https://models.example.com/v1", "fixture"),
    )
    assert runner.main() == 1
    assert (tmp_path / "output/model-config.json").read_bytes() == before
    assert not salts[0].exists()
