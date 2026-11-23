"""Private shared numeric kernel for the provider and index vector boundaries."""

from __future__ import annotations

from numbers import Real

import numpy as np
from numpy.typing import NDArray


def normalize_rows(
    vectors: object,
    dimension: int,
    *,
    allow_empty: bool,
    float32_input: bool,
    expected_rows: int | None = None,
) -> NDArray[np.float32]:
    """Validate numeric rows and normalize stably; wrappers own error contracts.

    Provider output must survive conversion to float32 before normalization.
    Index construction accepts finite float64 magnitudes and supports empty
    catalogs. Both use the same shape, finite/nonzero, and L2 implementation.
    """
    try:
        raw = (
            np.asarray(vectors)
            if isinstance(vectors, np.ndarray)
            else np.asarray(vectors, dtype=object)
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("vectors must be a rectangular numeric matrix") from exc
    if allow_empty and raw.shape == (0,):
        raw = raw.reshape(0, dimension)
    if (
        raw.ndim != 2
        or raw.shape[1] != dimension
        or (not allow_empty and raw.shape[0] == 0)
        or (expected_rows is not None and raw.shape[0] != expected_rows)
    ):
        raise ValueError("vector matrix shape mismatch")
    if isinstance(vectors, np.ndarray) and float32_input and raw.dtype.kind not in "iuf":
        raise ValueError("provider arrays must have a real numeric dtype")
    if raw.dtype.kind not in "iuf":
        accepted_types = (int, float, np.integer, np.floating) if float32_input else (Real,)
        if raw.dtype.kind != "O" or any(
            isinstance(value, (bool, np.bool_)) or not isinstance(value, accepted_types)
            for value in raw.flat
        ):
            raise ValueError("vector components must be real numbers, excluding booleans")
    try:
        with np.errstate(over="ignore", invalid="ignore"):
            matrix = np.asarray(raw, dtype=np.float32 if float32_input else np.float64)
        if not np.isfinite(matrix).all():
            raise ValueError("vector components must be finite in the required dtype")
        matrix = np.asarray(matrix, dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid vector components") from exc
    if matrix.shape[0] == 0:
        return np.empty((0, dimension), dtype=np.float32)
    scale = np.max(np.abs(matrix), axis=1, keepdims=True)
    if np.any(scale == 0):
        raise ValueError("zero vectors are invalid")
    scaled = matrix / scale
    norms = np.sqrt(np.sum(scaled * scaled, axis=1, keepdims=True))
    return np.asarray(scaled / norms, dtype=np.float32)
