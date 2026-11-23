"""Query-budget errors and normalized values agree across configuration entry points."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import numpy as np
import pytest

from semantic_service.config import (
    parse_embedding_timeout_seconds,
    parse_search_config,
    validate_embedding_timeout_seconds,
)
from semantic_service.models import ModelSettings

FIELD = "MODEL_EMBEDDING_TIMEOUT_SECONDS"
ERROR = (
    f"{FIELD} must be in (0, 6] seconds "
    "(numeric string in configuration; built-in int or float when passed directly)"
)


def model_values():
    return {
        "MODEL_ALLOWED_HOSTS": "validation-model.invalid",
        "MODEL_EMBEDDING_DIMENSIONS": "2",
    }


@pytest.mark.parametrize(
    "seconds", [0, -1, 6.001, 800, float("nan"), float("inf"), float("-inf"), 10**400]
)
def test_invalid_numeric_budget_has_one_contract_at_every_entry_point(seconds):
    values = model_values() | {FIELD: str(seconds)}
    model = ModelSettings.from_env(model_values())
    calls = (
        lambda: validate_embedding_timeout_seconds(seconds),
        lambda: parse_embedding_timeout_seconds(values),
        lambda: parse_search_config(values),
        lambda: ModelSettings.from_env(values),
        lambda: replace(model, embedding_timeout_seconds=seconds),
    )
    for call in calls:
        with pytest.raises(ValueError) as error:
            call()
        assert str(error.value) == ERROR


@pytest.mark.parametrize("raw", [None, True, 0.8, "", "private-config-marker"])
def test_invalid_environment_format_uses_the_contract_without_echoing_input(raw):
    with pytest.raises(ValueError) as error:
        parse_embedding_timeout_seconds({FIELD: raw})
    assert str(error.value) == ERROR
    assert "private-config-marker" not in str(error.value)


@pytest.mark.parametrize("value", [None, True, "0.8", np.float64(0.8)])
def test_direct_validator_keeps_its_builtin_numeric_type_policy(value):
    with pytest.raises(ValueError) as error:
        validate_embedding_timeout_seconds(value)
    assert str(error.value) == ERROR


@pytest.mark.parametrize("seconds", [0.000001, 0.45, 0.8, 1, 6])
def test_direct_model_construction_stores_normalized_float_and_remains_frozen(seconds):
    model = replace(ModelSettings.from_env(model_values()), embedding_timeout_seconds=seconds)
    assert model.embedding_timeout_seconds == seconds
    assert type(model.embedding_timeout_seconds) is float
    assert model.document_timeout_seconds == 30
    assert model.chat_timeout_seconds == 30
    assert model.rerank_timeout_seconds == 0.45
    with pytest.raises(FrozenInstanceError):
        model.embedding_timeout_seconds = 0.8


@pytest.mark.parametrize("raw", ["0.45", "0.8", "1", "6", "1e-6", " 0.8 "])
def test_valid_environment_formats_keep_their_existing_values(raw):
    parsed = parse_embedding_timeout_seconds({FIELD: raw})
    assert parsed == float(raw)
    assert type(parsed) is float


def test_default_embedding_budget_is_one_second():
    assert parse_embedding_timeout_seconds({}) == 1.0
    assert ModelSettings.from_env(model_values()).embedding_timeout_seconds == 1.0
