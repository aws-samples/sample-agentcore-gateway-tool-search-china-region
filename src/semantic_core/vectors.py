"""Finite L2 vectors, full-matrix scoring, then ACL mask and parent max."""

from __future__ import annotations

from collections.abc import Sequence
from numbers import Integral

import numpy as np
from numpy.typing import ArrayLike, NDArray

from semantic_core import _normalization


def _count(value: int, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return int(value)


def normalize_vectors(vectors: ArrayLike, dimension: int) -> NDArray[np.float32]:
    """Index wrapper: finite float64 inputs, empty catalogs, and ValueError.

    The returned float32 matrix is writable; VectorIndex freezes its resident
    copy. Provider output uses its separate nonempty/float32/error wrapper.
    """
    dimension = _count(dimension, "dimension", 1)
    return _normalization.normalize_rows(vectors, dimension, allow_empty=True, float32_input=False)


def validate_mask(allowed: Sequence[bool], tool_count: int) -> NDArray[np.bool_]:
    """Reject truthy integers, nested masks, and length mismatches."""
    tool_count = _count(tool_count, "tool_count", 0)
    try:
        raw = np.asarray(allowed, dtype=object)
    except (TypeError, ValueError) as exc:
        raise ValueError("authorization mask must be a boolean vector") from exc
    if raw.ndim != 1 or raw.size != tool_count:
        raise ValueError("authorization mask length mismatch")
    if any(not isinstance(value, (bool, np.bool_)) for value in raw):
        raise ValueError("authorization mask must contain only booleans")
    return np.asarray(raw, dtype=np.bool_)


class VectorIndex:
    """An immutable normalized matrix; ACLs allocate score/mask vectors only."""

    def __init__(
        self,
        vectors: ArrayLike,
        parent_indices: Sequence[int],
        tool_count: int,
        dimension: int,
    ) -> None:
        self.tool_count = _count(tool_count, "tool_count", 0)
        self.dimension = _count(dimension, "dimension", 1)
        normalized = normalize_vectors(vectors, self.dimension)
        try:
            parents = np.asarray(parent_indices, dtype=object)
        except (TypeError, ValueError) as exc:
            raise ValueError("parent indices must be a one-dimensional integer array") from exc
        if parents.ndim != 1 or parents.size != normalized.shape[0]:
            raise ValueError("every vector must have exactly one parent index")
        if any(
            isinstance(parent, (bool, np.bool_))
            or not isinstance(parent, Integral)
            or int(parent) < 0
            or int(parent) >= self.tool_count
            for parent in parents
        ):
            raise ValueError("parent indices must reference existing catalog tools")
        # Immutable bytes also prevent a caller from re-enabling WRITEABLE.
        self.vectors: NDArray[np.float32] = np.frombuffer(
            normalized.tobytes(), dtype=np.float32
        ).reshape(normalized.shape)
        parent_array = np.asarray(parents, dtype=np.intp)
        self.parent_indices: NDArray[np.intp] = np.frombuffer(parent_array.tobytes(), dtype=np.intp)

    def scores(self, query_vector: ArrayLike, allowed: Sequence[bool]) -> NDArray[np.float64]:
        mask = validate_mask(allowed, self.tool_count)
        try:
            query = np.asarray(query_vector, dtype=object)
        except (TypeError, ValueError) as exc:
            raise ValueError("query must be a one-dimensional numeric vector") from exc
        if query.ndim != 1:
            raise ValueError("query must be a one-dimensional numeric vector")
        normalized_query = normalize_vectors(query.reshape(1, -1), self.dimension)[0]
        # Never gather self.vectors by ACL. Score every row of the resident matrix.
        vector_scores = np.asarray(self.vectors @ normalized_query, dtype=np.float64)
        vector_scores[~mask[self.parent_indices]] = -np.inf
        # Mask is already applied to every child when parent aggregation begins.
        tool_scores = np.full(self.tool_count, -np.inf, dtype=np.float64)
        np.maximum.at(tool_scores, self.parent_indices, vector_scores)
        return tool_scores
