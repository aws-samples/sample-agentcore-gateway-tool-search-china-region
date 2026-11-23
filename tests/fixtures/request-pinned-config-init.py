"""A pinned configuration successfully cached before a later factory failure."""

import importlib
import json
import os

from scripts.benchmark_runtime_initialization import _configuration

remote = json.loads(_configuration())["settings"]
remote["SEMSEARCH_SUBJECT_CALLS_PER_MINUTE"] = "not-a-number"
os.environ.update(
    {
        "AWS_LAMBDA_FUNCTION_NAME": "fixture-request",
        "_HANDLER": "semantic_service.request_runtime.request_handler",
        "AWS_LAMBDA_INITIALIZATION_TYPE": "provisioned-concurrency",
    }
)
from semantic_service import runtime  # noqa: E402

loads = []


def load(*args):
    loads.append("configuration")
    return dict(remote)


runtime.load_configuration = load
# This is a valid lambda callback, not a module-level return statement.
# nosemgrep: return-not-in-function
runtime._aws = lambda service: object()
# This is a valid lambda callback, not a module-level return statement.
# nosemgrep: return-not-in-function
runtime._http = lambda: object()
try:
    importlib.import_module("semantic_service.request_runtime")
except RuntimeError as exc:
    assert str(exc) == "REQUEST initialization failed"
else:
    raise AssertionError("failed pinned configuration advertised healthy Init")
assert runtime._request.cache_info().currsize == 0
assert runtime._loaded_configuration.cache_info().currsize == 1
assert runtime._search_tool.cache_info().currsize == 1
assert runtime._store.cache_info().currsize == 1
assert runtime._envelope.cache_info().currsize == 1
remote["SEMSEARCH_SUBJECT_CALLS_PER_MINUTE"] = "600"
try:
    runtime._request()
except ValueError:
    pass
else:
    raise AssertionError("pinned configuration unexpectedly changed in the existing environment")
assert loads == ["configuration"]
print("PASS: failed Init retains pinned child configuration; replacement environment required")
