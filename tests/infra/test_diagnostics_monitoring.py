"""The same bounded invocation fields drive CloudWatch search metrics."""

from test_components import components  # noqa: F401


def test_search_diagnostic_filters_cover_final_outcomes(components):  # noqa: F811
    filters = {
        item["Properties"]["MetricTransformations"][0]["MetricName"]: item["Properties"][
            "FilterPattern"
        ]
        for item in components.values()
        if item["Type"] == "AWS::Logs::MetricFilter"
    }
    for mode in ("bm25", "rrf", "vector", "vector_bm25_fallback", "rerank"):
        assert filters[f"retrieval-{mode}"] == (
            f'{{ $.operation = "search" && $.ranking_mode = "{mode}" }}'
        )
    for field in ("empty_result", "below_threshold", "degraded"):
        assert filters[f"search-{field.replace('_', '-')}"] == (
            f'{{ $.operation = "search" && $.{field} IS TRUE }}'
        )
    assert filters["search-unranked"] == ('{ $.operation = "search" && $.ranking_mode IS NULL }')
    for state in ("applied", "disabled", "skipped", "failed"):
        assert filters[f"rerank-{state}"] == (
            f'{{ $.operation = "search" && $.rerank_state = "{state}" }}'
        )
    for state in ("available", "disabled", "unavailable", "failed", "not_attempted"):
        assert filters[f"vector-{state}"] == (
            f'{{ $.operation = "search" && $.vector_availability = "{state}" }}'
        )
    for mode in ("rrf", "vector", "vector_bm25_fallback"):
        assert filters[f"fusion-{mode}"] == (
            f'{{ $.operation = "search" && $.configured_fusion = "{mode}" }}'
        )
