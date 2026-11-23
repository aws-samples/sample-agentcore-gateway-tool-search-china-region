"""All ordinary tests are offline; live fixtures must be explicitly supplied."""

import copy
import json
import os
import socket

import pytest
from botocore.loaders import Loader
from botocore.session import Session


def _control_plane_session(model_paths):
    session = Session()
    session.register_component(
        "data_loader",
        Loader(
            extra_search_paths=[str(path) for path in model_paths],
            include_default_search_paths=False,
        ),
    )
    return session


@pytest.fixture
def reviewed_control_plane_session(tmp_path):
    """Synthesize an endpoint-only compatibility model from the locked public SDK."""
    loader = Loader(
        extra_search_paths=[Loader.BUILTIN_DATA_PATH], include_default_search_paths=False
    )
    model = copy.deepcopy(loader.load_service_model("bedrock-agentcore-control", "service-2"))
    members = model["shapes"]["McpServerTargetConfiguration"]["members"]
    members.pop("listingMode")
    members.pop("mcpToolSchema")
    directory = tmp_path / "bedrock-agentcore-control" / model["metadata"]["apiVersion"]
    directory.mkdir(parents=True)
    (directory / "service-2.json").write_text(json.dumps(model))
    return _control_plane_session([tmp_path, Loader.BUILTIN_DATA_PATH])


@pytest.fixture
def packaged_control_plane_session():
    """仅从已安装的 botocore 包读取模型，独立验证当前 SDK 的真实行为。"""
    return _control_plane_session([Loader.BUILTIN_DATA_PATH])


def pytest_addoption(parser):
    parser.addoption("--run-integration", action="store_true", default=False)


def pytest_collection_modifyitems(config, items):
    enabled = (
        config.getoption("--run-integration")
        and os.environ.get("SEMANTIC_CORE_RUN_INTEGRATION") == "1"
    )
    for item in items:
        if "integration" in item.keywords and not enabled:
            item.add_marker(
                pytest.mark.skip(reason="live integration requires both explicit switches")
            )


@pytest.fixture(autouse=True)
def no_network(request, monkeypatch):
    if "integration" in request.keywords:
        return

    def blocked(*args, **kwargs):
        raise AssertionError("network access forbidden in offline test")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
