"""The smaller REQUEST artifact passes the same target-wheel audit as the full ZIP."""

from __future__ import annotations

import json
import sys
import zipfile

from scripts import audit_dependencies, build_lambda


def test_target_audit_selects_request_wheels_and_runtime_roots(tmp_path, monkeypatch):
    wheelhouse = tmp_path / ".artifacts/wheelhouse"
    wheelhouse.mkdir(parents=True)
    wheels = []
    for name in ("root", "excluded"):
        path = wheelhouse / f"{name}-1.0-py3-none-any.whl"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(f"{name}-1.0.dist-info/METADATA", f"Name: {name}\nVersion: 1.0\n")
            archive.writestr(f"{name}-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\n")
        wheels.append(
            {
                "name": name,
                "version": "1.0",
                "filename": path.name,
                "sha256": build_lambda.sha256(path),
            }
        )
    lock = {
        "schema_version": 1,
        "python": "3.12",
        "architecture": "x86_64",
        "platform": "manylinux_2_34_x86_64",
        "source_date_epoch": 315532800,
        "build_backend": {"name": "setuptools", "version": "80.9.0"},
        "wheels": wheels,
    }
    (tmp_path / "lambda-wheels.lock.json").write_text(json.dumps(lock))
    (
        wheelhouse / wheels[1]["filename"]
    ).unlink()  # Excluded wheels are not required for this audit.
    monkeypatch.setattr(audit_dependencies, "ROOT", tmp_path)
    monkeypatch.setattr(build_lambda, "REQUEST_WHEELS", frozenset({"root"}))
    monkeypatch.setitem(sys.modules, "build_lambda", build_lambda)
    result = audit_dependencies.audit_lambda_wheels(
        roots=["root==1.0", "excluded==1.0"],
        locked={"root": "1.0", "excluded": "1.0"},
        lock_text="\n".join(f"--hash=sha256:{wheel['sha256']}" for wheel in wheels),
        operation="request",
    )
    assert result["operation"] == "request"
    assert result["closure_matches_runtime_roots"] is True
    assert [record["name"] for record in result["records"]] == ["root"]
    assert [record["name"] for record in result["excluded_wheels"]] == ["excluded"]
    assert result["closure_sha256"]


def _isolated_audit_main(tmp_path, monkeypatch):
    from types import SimpleNamespace

    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["root==1.0"]\n[project.optional-dependencies]\ndev = []\n'
    )
    (tmp_path / "requirements.lock").write_text("root==1.0\n")
    distribution = SimpleNamespace(
        metadata={"Name": "root"},
        version="1.0",
        requires=[],
        files=[],
        read_text=lambda name: "Name: root\nVersion: 1.0\n",
    )
    monkeypatch.setattr(audit_dependencies, "ROOT", tmp_path)
    monkeypatch.setattr(audit_dependencies.sys, "prefix", str(tmp_path / ".venv"))
    monkeypatch.setattr(
        audit_dependencies.importlib.metadata, "distributions", lambda: [distribution]
    )
    monkeypatch.setattr(
        audit_dependencies,
        "audit_lambda_wheels",
        lambda **kwargs: {"operation": kwargs["operation"]},
    )
    monkeypatch.setattr(audit_dependencies, "audit_node_tooling", lambda: {})


def test_audit_defaults_preserve_both_target_inventories(tmp_path, monkeypatch):
    _isolated_audit_main(tmp_path, monkeypatch)
    for operation in ("all", "request"):
        monkeypatch.setattr(sys, "argv", ["audit_dependencies.py", "--operation", operation])
        audit_dependencies.main()
    shared = tmp_path / ".artifacts/dependency-closure.json"
    request = tmp_path / ".artifacts/request-dependency-closure.json"
    assert request.is_file(), "REQUEST audit overwrote the shared inventory instead of keeping both"
    assert json.loads(shared.read_text())["lambda_target"]["operation"] == "all"
    assert json.loads(request.read_text())["lambda_target"]["operation"] == "request"


def test_audit_explicit_output_remains_supported(tmp_path, monkeypatch):
    _isolated_audit_main(tmp_path, monkeypatch)
    output = tmp_path / "custom-inventory.json"
    monkeypatch.setattr(
        sys, "argv", ["audit_dependencies.py", "--operation", "request", "--output", str(output)]
    )
    audit_dependencies.main()
    assert json.loads(output.read_text())["lambda_target"]["operation"] == "request"
