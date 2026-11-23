"""Run deliberate in-process safety mutants; never edit the working-tree source.

A mutant run must fail its assertions (pytest exit 1). The unmutated control must
pass. These logs complement implementation red-first tests with evidence that
plausible but wrong algorithms are detected, even when their final Top-K agrees.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
TESTS = {
    "late-child-mask": [
        "tests/test_vectors.py::test_scores_are_masked_before_aggregation",
        "tests/test_vectors.py::test_denied_children_are_erased_before_parent_accumulator_exists",
    ],
    "acl-average-length": [
        "tests/test_bm25.py::test_acl_masks_do_not_recompute_catalog_statistics",
        "tests/test_bm25.py::test_average_length_remains_catalog_wide_after_each_mask",
    ],
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mutation", choices=tuple(TESTS))
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "src"))
    vectors = importlib.import_module("semantic_core.vectors")
    bm25 = importlib.import_module("semantic_core.bm25")

    if args.mutation == "late-child-mask":

        def wrong_vector_scores(index, query_vector, allowed):
            mask = vectors.validate_mask(allowed, index.tool_count)
            query = vectors.normalize_vectors(
                np.asarray(query_vector).reshape(1, -1), index.dimension
            )[0]
            child_scores = np.asarray(index.vectors @ query, dtype=np.float64)
            scores = np.full(index.tool_count, -np.inf, dtype=np.float64)
            # Deliberate defect: denied children reach the parent accumulator.
            np.maximum.at(scores, index.parent_indices, child_scores)
            scores[~mask] = -np.inf
            return scores

        vectors.VectorIndex.scores = wrong_vector_scores
    elif args.mutation == "acl-average-length":

        def wrong_lexical_scores(index, query, allowed):
            mask = vectors.validate_mask(allowed, index.document_count)
            # Deliberate defect: a request rewrites full-catalog length statistics.
            index.average_length = float(index.document_lengths[mask].mean()) if mask.any() else 0.0
            scores = index._score(query)
            scores[~mask] = -np.inf
            return scores

        bm25.BM25Index.masked_scores = wrong_lexical_scores

    selected = (
        TESTS[args.mutation]
        if args.mutation
        else [test for tests in TESTS.values() for test in tests]
    )
    print(f"Mutation: {args.mutation or 'none (production control)'}", flush=True)
    return int(pytest.main([*selected, "-vv", "--tb=short"]))


if __name__ == "__main__":
    raise SystemExit(main())
