"""Render complete local report fixtures without borrowing historical conclusions."""

from __future__ import annotations

import importlib.util
import json
from dataclasses import asdict
from pathlib import Path
from types import ModuleType

import pytest

from semantic_eval.dataset import EvalQuery
from semantic_eval.metrics import Observation, summarize


@pytest.fixture
def reporter() -> ModuleType:
    script = Path(__file__).parents[1] / "reports/tuning/summarize_tuning.py"
    spec = importlib.util.spec_from_file_location("future_report_wording_fixture", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _save(output: Path, name: str, value: object) -> None:
    (output / name).write_text(json.dumps(value), encoding="utf-8")


def _fixtures(
    output: Path, *, future: bool, ungated_control: bool = False
) -> tuple[EvalQuery, ...]:
    queries = (
        EvalQuery("positive", "Create record", "en", "gateway", ("truth",), ("truth",), "100%"),
        EvalQuery("empty", "No such tool", "en", "gateway", ("truth",), (), "100%"),
    )
    observations = [
        Observation(
            query.id,
            query.language,
            query.permission_group,
            query.relevant_ids,
            query.allowed_ids,
            ("truth",),
            1.0,
        )
        for query in queries
    ]

    def run(name: str) -> dict[str, object]:
        return {
            "name": name,
            "observations": [asdict(row) for row in observations],
            "summary": summarize(observations, bootstrap_samples=2),
            "mode_counts": {"vector": 2},
        }

    recommendation: dict[str, object] = {
        "chosen_fusion_arm": "vector",
        "chosen_threshold_arm": "cosine_0.6",
        "selection_rule": "Fixture selection rule.",
    }
    if future:
        recommendation.update(
            dev_finished_at="2026-10-11T00:00:00Z",
            split_manifest_sha256="fixture",
            config={"fusion_strategy": "rrf", "min_vector_similarity": 0.35},
        )
    else:
        recommendation["frozen_before_test"] = True
    _save(output, "recommendation.json", recommendation)
    _save(output, "dev-fusion.json", {"runs": [run("rrf"), run("vector")]})
    _save(
        output,
        "dev-threshold.json",
        {
            "runs": [
                run("ungated"),
                *[
                    run(f"cosine_{value}")
                    for value in ((0.55, 0.6, 0.65) if future else (0.5, 0.6, 0.7))
                ],
            ]
        },
    )
    _save(output, "dev-rerank.json", {"runs": [run("rrf_rerank"), run("vector_rerank")]})
    arms = ["current_rrf", "recommended", "recommended_rerank"]
    if ungated_control:
        arms.append("vector_ungated")
    _save(output, "test-retrieval.json", {"runs": [run(name) for name in arms]})
    _save(
        output,
        "test-knee.json",
        {
            "complete": True,
            "query_ids": ["positive"],
            "observations": [
                {
                    "query_id": "positive",
                    "tool_count": size,
                    "arm": arm,
                    "correct": True,
                    "selected_id": "truth",
                    "expected_id": "truth",
                    "candidate_ids": ["truth"],
                    "candidate_count": 1,
                    "latency_ms": 1.0,
                    "search_mode": "vector",
                    "error": None,
                    "usage": {"prompt_tokens": 10},
                }
                for size in (20, 50, 100, 200, 500)
                for arm in ("full_load", "search_first", "search_rerank")
            ],
        },
    )
    return queries


@pytest.mark.parametrize("future", [False, True])
def test_rendered_dev_describes_its_own_grid_and_provenance(
    reporter: ModuleType, tmp_path: Path, future: bool
) -> None:
    queries = _fixtures(tmp_path, future=future)
    reporter.summarize_dev(tmp_path, queries)
    text = (tmp_path / "dev-report.md").read_text()
    if future:
        assert "The historical cosine grid has 0.1 spacing" not in text
        assert "75 test texts had been observed" not in text
        assert "0.05" in text
        assert "not authenticated" in text
    else:
        assert "The historical cosine grid has 0.1 spacing" in text
        assert "75 test texts had been observed" in text
        assert "internal claim hashes do not establish chronology" in text


@pytest.mark.parametrize("future", [False, True])
@pytest.mark.parametrize("ungated_control", [False, True])
def test_rendered_test_describes_the_recorded_control_arm(
    reporter: ModuleType, tmp_path: Path, future: bool, ungated_control: bool
) -> None:
    queries = _fixtures(tmp_path, future=future, ungated_control=ungated_control)
    reporter.summarize_test(tmp_path, queries)
    text = (tmp_path / "final-test-report.md").read_text()
    if ungated_control:
        assert "omitted ungated vector" not in text
        assert "cannot be separated on test" not in text
        assert "vector_ungated" in text
        assert "gate cost" in text
    elif not future:
        assert "The historical test omitted ungated vector" in text
    else:
        assert "No ungated-vector arm was recorded" in text
    if future:
        assert "offline correction of the one completed test report" not in text
        assert "75 test texts had been observed" not in text
        assert "Its historical vector + 0.6 results" not in text
        assert "not authenticated" in text
    else:
        assert "offline correction of the one completed test report" in text


def test_empty_breakdown_uses_the_same_unicode_text_groups_as_the_split(
    reporter: ModuleType,
) -> None:
    queries = tuple(
        EvalQuery(str(index), text, "en", "gateway", ("tool",), (), "100%")
        for index, text in enumerate(("same text", "ＳＡＭＥ　ＴＥＸＴ"))
    )
    observations = [
        asdict(
            Observation(
                query.id,
                query.language,
                query.permission_group,
                query.relevant_ids,
                query.allowed_ids,
                (),
                1.0,
            )
        )
        for query in queries
    ]
    result = reporter.empty_breakdown({"observations": observations}, queries)
    assert result["outside_catalog"]["n"] == 2
    assert result["outside_catalog"]["unique_texts"] == 1
