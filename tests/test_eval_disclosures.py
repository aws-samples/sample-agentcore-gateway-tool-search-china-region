from __future__ import annotations

import pytest

from semantic_eval.reporting import render_knee_markdown, render_markdown


@pytest.mark.parametrize("mode", ["mock", "real"])
def test_regression_discloses_shared_synthetic_vocabulary_in_every_mode(mode: str) -> None:
    rendered = render_markdown({"metadata": {"mode": mode}, "runs": []})

    assert "Query and descriptor vocabularies are shared by construction" in rendered
    assert "lexical arms are a plumbing/ordering regression" in rendered
    assert "rather than a relevance measurement" in rendered


def test_mock_knee_discloses_that_selector_accuracy_is_not_informative() -> None:
    rendered = render_knee_markdown({"mode": "mock", "runs": []})

    assert "Selection accuracy is not an informative axis under the mock selector" in rendered
    assert "mock knee is a latency/retrieval-recall baseline only" in rendered
    assert "accuracy knee requires the real selector" in rendered


def test_real_knee_does_not_claim_its_selector_accuracy_is_uninformative() -> None:
    rendered = render_knee_markdown({"mode": "real", "runs": []})

    assert "Selection accuracy is not an informative axis under the mock selector" not in rendered
    assert "mock knee is a latency/retrieval-recall baseline only" not in rendered
    assert "accuracy knee requires the real selector" not in rendered
    assert "do not establish a production breakpoint" in rendered
