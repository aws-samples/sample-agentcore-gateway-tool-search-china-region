"""Vector input and score-before-mask safety fixtures."""

from collections.abc import Sequence
from typing import cast

import numpy as np
import pytest
from numpy.typing import ArrayLike, NDArray

from semantic_core.vectors import VectorIndex, normalize_vectors


@pytest.mark.parametrize(
    "vectors",
    [
        [[0.0, 0.0]],
        [[float("nan"), 1.0]],
        [[float("inf"), 1.0]],
        [[1.0]],
        [[1, 2], [3]],
        [[True, 1.0]],
        [["1", "2"]],
        [[1 + 0j, 2]],
        [1.0, 2.0],
    ],
)
def test_invalid_vectors_are_rejected(vectors: object) -> None:
    with pytest.raises(ValueError):
        normalize_vectors(cast(ArrayLike, vectors), dimension=2)


def test_l2_normalizes_extreme_magnitudes_without_overflow() -> None:
    vectors = normalize_vectors([[3e300, 4e300], [3e-300, 4e-300]], dimension=2)
    assert vectors.dtype == np.float32
    assert vectors == pytest.approx(np.array([[0.6, 0.8], [0.6, 0.8]]))
    assert np.linalg.norm(vectors, axis=1) == pytest.approx([1, 1])


def test_mask_precedes_parent_max_and_excludes_all_unauthorized_vectors() -> None:
    index = VectorIndex(
        [[1, 0], [1, 0], [0.8, 0.6], [0.6, 0.8]],
        parent_indices=[0, 0, 1, 2],
        tool_count=3,
        dimension=2,
    )
    scores = index.scores([1, 0], allowed=[False, True, True])
    assert np.isneginf(scores[0])
    assert scores[1:] == pytest.approx([0.8, 0.6])
    assert not index.vectors.flags.writeable
    assert not index.parent_indices.flags.writeable


def test_parent_max_produces_unique_tool_scores() -> None:
    index = VectorIndex(
        [[0, 1], [1, 0], [0.6, 0.8]],
        parent_indices=[0, 0, 1],
        tool_count=2,
        dimension=2,
    )
    assert index.scores([1, 0], [True, True]) == pytest.approx([1, 0.6])


def test_empty_vector_matrix_is_valid() -> None:
    index = VectorIndex([], parent_indices=[], tool_count=2, dimension=2)
    assert index.vectors.shape == (0, 2)
    assert np.all(np.isneginf(index.scores([1, 0], [True, True])))


@pytest.mark.parametrize("parents", [[True], [-1], [1], [0.0], [], [[0]]])
def test_invalid_parent_indices_are_rejected(parents: object) -> None:
    with pytest.raises(ValueError):
        VectorIndex([[1, 0]], cast(Sequence[int], parents), tool_count=1, dimension=2)


@pytest.mark.parametrize("mask", [[1], [False, True], [["true"]], [None]])
def test_invalid_vector_acl_masks_are_rejected(mask: object) -> None:
    index = VectorIndex([[1, 0]], [0], tool_count=1, dimension=2)
    with pytest.raises(ValueError):
        index.scores([1, 0], cast(Sequence[bool], mask))


@pytest.mark.parametrize("query", [[0, 0], [float("nan"), 1], [1], [True, 1], [[1, 0]]])
def test_invalid_query_vectors_are_rejected(query: object) -> None:
    index = VectorIndex([[1, 0]], [0], tool_count=1, dimension=2)
    with pytest.raises(ValueError):
        index.scores(cast(ArrayLike, query), [True])


def test_scores_are_masked_before_aggregation(monkeypatch: pytest.MonkeyPatch) -> None:
    index = VectorIndex([[1, 0], [1, 0], [0, 1]], [0, 0, 1], tool_count=2, dimension=2)
    maximum = np.maximum
    captured: list[NDArray[np.float64]] = []

    class MaximumSpy:
        def __getattr__(self, name: str) -> object:
            return getattr(maximum, name)

        def at(
            self,
            output: NDArray[np.float64],
            parents: NDArray[np.intp],
            scores: NDArray[np.float64],
        ) -> None:
            captured.append(scores.copy())
            maximum.at(output, parents, scores)

    monkeypatch.setattr("semantic_core.vectors.np.maximum", MaximumSpy())
    index.scores([1, 0], [False, True])
    assert len(captured) == 1
    assert np.all(np.isneginf(captured[0][:2]))
    assert captured[0][2] == 0


def test_matrix_is_not_gathered_by_acl_and_stays_immutable() -> None:
    class NoGatherMatrix(np.ndarray):
        def __getitem__(self, key: object) -> object:
            raise AssertionError("the resident vector matrix must not be sliced by ACL")

    index = VectorIndex([[1, 0], [0, 1]], [0, 1], tool_count=2, dimension=2)
    with pytest.raises(ValueError):
        index.vectors.setflags(write=True)
    with pytest.raises(ValueError):
        index.parent_indices.setflags(write=True)
    index.vectors = index.vectors.view(NoGatherMatrix)
    assert index.scores([1, 0], [True, False])[0] == 1


def test_negative_cosines_are_preserved_without_absolute_threshold() -> None:
    index = VectorIndex([[-1, 0], [0, -1]], [0, 1], tool_count=2, dimension=2)
    assert index.scores([1, 0], [True, True]) == pytest.approx([-1, 0])


def test_denied_children_are_erased_before_parent_accumulator_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Observe the accumulator boundary independently of the maximum.at spy."""
    index = VectorIndex(
        [[1, 0], [0.8, 0.6], [0.6, 0.8], [0, 1]],
        [0, 1, 0, 2],
        tool_count=3,
        dimension=2,
    )
    asarray, full = np.asarray, np.full
    children: list[np.ndarray] = []

    def capture_scores(*args: object, **kwargs: object) -> np.ndarray:
        array = asarray(*args, **kwargs)
        if array.shape == (4,) and array.dtype == np.float64:
            children.append(array)
        return array

    def allocate_parents(*args: object, **kwargs: object) -> np.ndarray:
        assert len(children) == 1
        assert np.isneginf(children[0][0]) and np.isneginf(children[0][2])
        assert children[0][1] > 0
        return full(*args, **kwargs)

    monkeypatch.setattr("semantic_core.vectors.np.asarray", capture_scores)
    monkeypatch.setattr("semantic_core.vectors.np.full", allocate_parents)
    scores = index.scores([1, 0], [False, True, True])
    assert np.isneginf(scores[0])
    assert scores[1] == pytest.approx(0.8)
