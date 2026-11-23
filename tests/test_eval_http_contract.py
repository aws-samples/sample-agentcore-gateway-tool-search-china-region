"""Pin the private shared HTTP surface consumed by the evaluation selector.

This is a consumer contract, not a public core API. A coordinated refactor must
update this test deliberately; core source is never rewritten by these tests.
"""

from __future__ import annotations

import importlib
import inspect

import pytest

from semantic_core import _http

_HELPER_SIGNATURES = {
    "_HTTPProvider": (
        "(client: 'httpx.Client', config: 'HTTPConfig', profile: 'ModelProfile', "
        "*, credential: 'str | None' = None) -> 'None'"
    ),
    "_list": "(value: 'object') -> 'list[object]'",
    "_object": "(value: 'object') -> 'dict[str, object]'",
    "_usage": "(response: 'Mapping[str, object]') -> 'Mapping[str, int]'",
    "_utf8_size": "(text: 'str') -> 'int'",
    "validate_preset_origin": "(transport: 'HTTPConfig', origin_only: 'bool') -> 'None'",
}
_PROVIDER_SIGNATURES = {
    "__init__": (
        "(self, client: 'httpx.Client', config: 'HTTPConfig', profile: 'ModelProfile', "
        "*, credential: 'str | None' = None) -> 'None'"
    ),
    "describe": "(self) -> 'ModelProfile'",
    "_post": (
        "(self, path: 'str', payload: 'Mapping[str, object]', "
        "context: 'RequestContext') -> 'dict[str, object]'"
    ),
    "_validate_identity": "(self, response: 'Mapping[str, object]') -> 'None'",
}


@pytest.mark.parametrize("name", _HELPER_SIGNATURES)
def test_selector_private_http_helper_contract(name: str) -> None:
    assert hasattr(_http, name), (
        f"selector's private HTTP dependency was renamed or removed: {name}"
    )
    helper = getattr(_http, name)
    assert str(inspect.signature(helper)) == _HELPER_SIGNATURES[name], (
        f"selector's private HTTP dependency changed signature: {name}"
    )
    selector = importlib.import_module("semantic_eval.selection")
    assert getattr(selector, name) is helper, (
        f"selector no longer consumes the pinned helper: {name}"
    )


@pytest.mark.parametrize("name", _PROVIDER_SIGNATURES)
def test_selector_private_http_provider_method_contract(name: str) -> None:
    assert hasattr(_http._HTTPProvider, name), f"selector's HTTP base method was removed: {name}"
    assert (
        str(inspect.signature(getattr(_http._HTTPProvider, name))) == _PROVIDER_SIGNATURES[name]
    ), f"selector's HTTP base method changed signature: {name}"
