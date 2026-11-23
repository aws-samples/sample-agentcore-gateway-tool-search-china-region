"""REQUEST package selection is explicit, validated and reflected in the assembly."""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from infra import app as infra_app
from infra.config import DeploymentConfig
from infra.functions import Functions
from scripts import synth

from semantic_service import runtime

SHARED_MODULES = (
    "semantic_core/__init__.py",
    "semantic_service/__init__.py",
    "semantic_service/runtime.py",
    "semantic_service/search.py",
    "semantic_service/indexer.py",
    "semantic_service/enhancer.py",
)
REQUEST_MODULES = (
    "semantic_core/__init__.py",
    "semantic_service/__init__.py",
    "semantic_service/runtime.py",
    "semantic_service/request_runtime.py",
)
REQUEST_HANDLER = "semantic_service.request_runtime.request_handler"
SHARED_HANDLER = "semantic_service.runtime.request_handler"


def archive(path, modules):
    with zipfile.ZipFile(path, "w") as package:
        for module in modules:
            package.writestr(module, "# Offline package fixture.\n")
    return path


def component(shared, request, tmp_path):
    app = cdk.App(outdir=str(tmp_path / "cdk.out"))
    stack = cdk.Stack(app, "fixture")
    return Functions(
        stack,
        "Functions",
        prefix="fixture",
        asset_path=shared,
        request_asset_path=request,
        environments={name: {} for name in ("search", "request", "indexer", "enhancer")},
        permissions={name: [] for name in ("search", "request", "indexer", "enhancer")},
    )


def test_synthesized_request_handler_prepares_the_selected_module(monkeypatch, capsys, tmp_path):
    shared = archive(tmp_path / "lambda.zip", SHARED_MODULES)
    request = archive(tmp_path / "request.zip", REQUEST_MODULES)
    functions = component(shared, request, tmp_path)
    template = Template.from_stack(cdk.Stack.of(functions))
    selected = [
        row["Properties"]
        for row in template.find_resources("AWS::Lambda::Function").values()
        if row["Properties"]["FunctionName"] == functions.names["request"]
    ]
    assert len(selected) == 1
    handler = selected[0]["Handler"]
    module_name, separator, function_name = handler.rpartition(".")
    assert separator
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", selected[0]["FunctionName"])
    monkeypatch.setenv("AWS_LAMBDA_INITIALIZATION_TYPE", "provisioned-concurrency")
    monkeypatch.setenv("_HANDLER", handler)
    monkeypatch.setattr(runtime, "_FIRST_INVOCATION", True)
    calls = []
    monkeypatch.setattr(runtime, "_request", lambda: calls.append("factory"))
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    package_name, _, member_name = module_name.rpartition(".")
    # Module names come only from Handler values in the locally synthesized template.
    # nosemgrep: non-literal-import
    package = importlib.import_module(package_name)
    monkeypatch.setattr(package, member_name, None, raising=False)
    try:
        # Module names come only from Handler values in the locally synthesized template.
        # nosemgrep: non-literal-import
        entry = importlib.import_module(module_name)
        assert callable(getattr(entry, function_name))
        assert calls == ["factory"] and runtime._FIRST_INVOCATION is True
        row = json.loads(capsys.readouterr().out)
        assert row["operation"] == "request_init" and row["status"] == "ready"
        assert row["stage_timings"]["handler.factory"]["count"] == 1
    finally:
        sys.modules.pop(module_name, None)


@pytest.mark.parametrize("package", ["shared", "request"])
@pytest.mark.parametrize("value", ["", " ", "\t", " request.zip", "request.zip ", True, 3, [], {}])
def test_app_rejects_invalid_request_asset_context_before_staging(package, value, monkeypatch):
    config = replace(
        DeploymentConfig.from_file(Path("infra/config.example.json")), request_package=package
    )
    context = {"request_asset": value}
    app = SimpleNamespace(node=SimpleNamespace(try_get_context=context.get), synth=lambda: None)
    monkeypatch.setattr(infra_app.cdk, "App", lambda: app)
    monkeypatch.setattr(infra_app.DeploymentConfig, "from_file", lambda *a, **k: config)
    monkeypatch.setattr(infra_app, "write_configuration", lambda *a: None)
    staged = []
    monkeypatch.setattr(infra_app, "SemsearchStack", lambda *a, **k: staged.append(k))
    with pytest.raises(ValueError, match="nonempty path string"):
        infra_app.main()
    assert staged == []


def test_request_asset_path_requires_persistent_request_package():
    with pytest.raises(ValueError, match="persistent request_package=request"):
        infra_app.request_asset_from_context("shared", "request.zip")


@pytest.mark.parametrize("case", ["directory", "missing", "not-zip", "shared", "directory-member"])
def test_request_asset_is_a_zip_with_the_deployed_entrypoint(case, tmp_path):
    shared = archive(tmp_path / "lambda.zip", SHARED_MODULES)
    request = tmp_path / "request.zip"
    if case == "directory":
        request.mkdir()
    elif case == "not-zip":
        request.write_text("not an archive")
    elif case == "shared":
        request = shared
    elif case == "directory-member":
        archive(request, (*REQUEST_MODULES[:-1], "semantic_service/request_runtime.py/"))
    with pytest.raises(ValueError, match="REQUEST|request"):
        component(shared, request, tmp_path)


def test_request_archive_cannot_be_used_as_shared_asset(tmp_path):
    request = archive(tmp_path / "request.zip", REQUEST_MODULES)
    with pytest.raises(ValueError, match="shared"):
        component(request, None, tmp_path)


@pytest.mark.parametrize("field", ["operation", "artifact_sha256"])
def test_request_asset_rejects_a_stale_or_wrong_manifest(field, tmp_path):
    shared = archive(tmp_path / "lambda.zip", SHARED_MODULES)
    request = archive(tmp_path / "request.zip", REQUEST_MODULES)
    manifest = {
        "operation": "request",
        "artifact_sha256": hashlib.sha256(request.read_bytes()).hexdigest(),
    }
    manifest[field] = "all" if field == "operation" else "0" * 64
    request.with_suffix(".manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifest"):
        component(shared, request, tmp_path)


@pytest.mark.parametrize("package", ["", "REQUEST", True, None])
def test_request_package_config_is_persistent_and_validated(package):
    config = DeploymentConfig.from_file(Path("infra/config.example.json"))
    with pytest.raises(ValueError, match="request_package"):
        replace(config, request_package=package)


@pytest.mark.parametrize("count", [-1, True, "1", 1.5])
def test_request_provisioned_concurrency_requires_a_nonnegative_integer(count):
    config = DeploymentConfig.from_file(Path("infra/config.example.json"))
    with pytest.raises(ValueError, match="request_provisioned_concurrency"):
        replace(config, request_package="request", request_provisioned_concurrency=count)


def test_request_provisioned_concurrency_requires_the_request_package():
    config = DeploymentConfig.from_file(Path("infra/config.example.json"))
    with pytest.raises(ValueError, match="request_package"):
        replace(config, request_provisioned_concurrency=1)


def assembly(directory, handler):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "artifacts": {
                    "fixture": {
                        "type": "aws:cloudformation:stack",
                        "properties": {"templateFile": "fixture.template.json"},
                    }
                }
            }
        )
    )
    (directory / "fixture.template.json").write_text(
        json.dumps(
            {
                "Resources": {
                    "Request": {
                        "Type": "AWS::Lambda::Function",
                        "Properties": {"Handler": handler},
                    }
                }
            }
        )
    )


def run_synth(
    monkeypatch, tmp_path, *, package="request", flags=(), emitted=REQUEST_HANDLER, ecs=False
):
    config = json.loads(Path("infra/config.example.json").read_text())
    config["request_package"] = package
    if ecs:
        config["ecs_search"] = {"fixture": True}
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config))
    output = tmp_path / "assembly"
    calls = []

    def launch(arguments, **kwargs):
        calls.append((arguments, kwargs))
        assembly(output, emitted)

    node = tmp_path / "node"
    node.write_text("#!/bin/sh\nexit 99\n")  # launch is mocked; this must never execute.
    node.chmod(0o755)
    monkeypatch.setattr(synth.shutil, "which", lambda _: str(node))
    monkeypatch.setattr(synth.subprocess, "run", launch)
    monkeypatch.setattr(
        synth.sys,
        "argv",
        [
            "synth.py",
            "--config",
            str(config_file),
            "--region",
            "cn-north-1",
            "--output",
            str(output),
            *flags,
        ],
    )
    synth.main()
    return calls


def test_synth_forwards_explicit_request_asset_to_new_assembly(monkeypatch, tmp_path):
    request = archive(tmp_path / "custom-request.zip", REQUEST_MODULES)
    calls = run_synth(monkeypatch, tmp_path, flags=("--request-asset", str(request)))
    assert len(calls) == 1
    arguments, options = calls[0]
    assert f"request_asset={request}" in arguments
    assert options["env"]["AWS_CONFIG_FILE"] == "/dev/null"
    assert options["env"]["AWS_SHARED_CREDENTIALS_FILE"] == "/dev/null"
    assert options["shell"] is False


def test_synth_fails_if_requested_handler_did_not_reach_the_assembly(monkeypatch, tmp_path):
    with pytest.raises(SystemExit, match="REQUEST handler"):
        run_synth(monkeypatch, tmp_path, emitted=SHARED_HANDLER)


def test_synth_requires_explicit_rollback_from_existing_request_assembly(monkeypatch, tmp_path):
    assembly(tmp_path / "assembly", REQUEST_HANDLER)
    with pytest.raises(SystemExit, match="confirm-request-rollback"):
        run_synth(monkeypatch, tmp_path, package="shared", emitted=SHARED_HANDLER)


def test_synth_can_confirm_request_rollback(monkeypatch, tmp_path):
    assembly(tmp_path / "assembly", REQUEST_HANDLER)
    calls = run_synth(
        monkeypatch,
        tmp_path,
        package="shared",
        flags=("--confirm-request-rollback",),
        emitted=SHARED_HANDLER,
    )
    assert len(calls) == 1


def test_request_alias_alone_can_enable_provisioned_concurrency(tmp_path):
    shared = archive(tmp_path / "lambda.zip", SHARED_MODULES)
    request = archive(tmp_path / "request.zip", REQUEST_MODULES)
    app = cdk.App(outdir=str(tmp_path / "cdk.out"))
    stack = cdk.Stack(app, "fixture")
    Functions(
        stack,
        "Functions",
        prefix="fixture",
        asset_path=shared,
        request_asset_path=request,
        request_provisioned_concurrency=1,
        environments={name: {} for name in ("search", "request", "indexer", "enhancer")},
        permissions={name: [] for name in ("search", "request", "indexer", "enhancer")},
    )
    aliases = Template.from_stack(stack).find_resources("AWS::Lambda::Alias")
    configured = {
        logical_id: value["Properties"]["ProvisionedConcurrencyConfig"]
        for logical_id, value in aliases.items()
        if "ProvisionedConcurrencyConfig" in value["Properties"]
    }
    assert len(configured) == 1
    assert "request" in next(iter(configured))
    assert next(iter(configured.values())) == {"ProvisionedConcurrentExecutions": 1}


@pytest.mark.parametrize("flags", [("--asset", "missing.zip"), ("--stage", "bad;stage")])
def test_synth_rejects_invalid_external_arguments_before_launch(monkeypatch, tmp_path, flags):
    with pytest.raises(SystemExit):
        run_synth(monkeypatch, tmp_path, flags=flags)
    assert not (tmp_path / "assembly" / "manifest.json").exists()
