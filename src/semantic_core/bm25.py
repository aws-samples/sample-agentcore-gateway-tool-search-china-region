"""BM25 with persisted full-catalog statistics and score-before-mask ACLs.

The version-1 field convention is deliberately explicit:
* tokenize name, description, parameters, then examples in that priority order;
* retain at most 512 tokens/document, 128 from examples, and 3 occurrences/term
  across the whole document (all caps include synonym/identifier expansions);
* weighted TF sums 3, 2, 1, 0.5 for retained occurrences in those fields;
* document length counts retained occurrences WITHOUT field weights;
* IDF(t) = log(1 + (N - DF(t) + 0.5) / (DF(t) + 0.5));
* score uses k1=1.2, b=0.75 and one contribution per unique query term.

All statistics use the same complete Gateway catalog and remain unchanged for
every ACL. Empty/unknown-term queries score zero; there is no absolute threshold.
"""

from __future__ import annotations

import json
import math
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import product
from types import MappingProxyType
from typing import cast

import numpy as np
from numpy.typing import NDArray

from semantic_core.tokenization import ChineseTokenizer
from semantic_core.vectors import validate_mask

_VERSION = "bm25-v1"
_K1 = 1.2
_B = 0.75
_MAX_DOCUMENT_TOKENS = 512
_MAX_EXAMPLE_TOKENS = 128
_MAX_TERM_FREQUENCY = 3
_PARAMETERS: dict[str, object] = {
    "k1": _K1,
    "b": _B,
    "field_weights": [3.0, 2.0, 1.0, 0.5],
    "max_document_tokens": _MAX_DOCUMENT_TOKENS,
    "max_example_tokens": _MAX_EXAMPLE_TOKENS,
    "max_term_frequency": _MAX_TERM_FREQUENCY,
    "length_convention": "unweighted-retained-tokens",
    "query_tf": "unique",
}


def _tf_constraints() -> dict[float, tuple[int, int, int, int]]:
    """Map weighted TF to min/max count, free count increments, min examples.

    For v1 weights, every possible retained count is contiguous and adding an
    occurrence costs either zero or two example tokens. Thus a row's exact
    minimum required examples can be found without an expensive subset search.
    """
    options: dict[float, dict[int, int]] = {}
    for count in range(1, _MAX_TERM_FREQUENCY + 1):
        for weights in product((3.0, 2.0, 1.0, 0.5), repeat=count):
            weighted_tf, examples = sum(weights), weights.count(0.5)
            counts = options.setdefault(weighted_tf, {})
            counts[count] = min(counts.get(count, _MAX_TERM_FREQUENCY + 1), examples)
    result: dict[float, tuple[int, int, int, int]] = {}
    for weighted_tf, counts in options.items():
        minimum, maximum = min(counts), max(counts)
        base_examples = counts[minimum]
        free = max(count for count, examples in counts.items() if examples == base_examples)
        result[weighted_tf] = (minimum, maximum, free - minimum, base_examples)
    return result


_TF_CONSTRAINTS = _tf_constraints()


def _same_json(actual: object, expected: object) -> bool:
    """Compare JSON types exactly, including bool/int and int/float differences."""
    try:
        return json.dumps(actual, sort_keys=True, allow_nan=False) == json.dumps(
            expected, sort_keys=True, allow_nan=False
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("BM25 snapshot must contain finite JSON values") from exc


def _persisted_row(value: object, length: object) -> Mapping[str, float]:
    if (
        type(length) is not float
        or not math.isfinite(length)
        or not length.is_integer()
        or not 0 <= length <= _MAX_DOCUMENT_TOKENS
        or not isinstance(value, dict)
    ):
        raise ValueError("invalid BM25 term row or document length")
    minimum = maximum = free_increments = base_examples = 0
    for term, tf in value.items():
        if (
            not isinstance(term, str)
            or not term
            or any(character.isspace() for character in term)
            or term != unicodedata.normalize("NFKC", term).lower()
            or type(tf) is not float
            or not math.isfinite(tf)
            or tf not in _TF_CONSTRAINTS
        ):
            raise ValueError("invalid BM25 token or weighted term frequency")
        low, high, free, examples = _TF_CONSTRAINTS[tf]
        minimum += low
        maximum += high
        free_increments += free
        base_examples += examples
    if not minimum <= length <= maximum:
        raise ValueError("BM25 document length is inconsistent with retained term counts")
    required_examples = base_examples + 2 * max(0, int(length) - minimum - free_increments)
    if required_examples > _MAX_EXAMPLE_TOKENS:
        raise ValueError("BM25 term row exceeds the example token budget")
    return MappingProxyType(cast(dict[str, float], dict(value)))


@dataclass(frozen=True)
class DocumentFields:
    name: str
    description: str
    parameters: str = ""
    examples: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) for value in (self.name, self.description, self.parameters)
        ):
            raise ValueError("document fields must be strings")
        if not isinstance(self.examples, tuple) or any(
            not isinstance(example, str) for example in self.examples
        ):
            raise ValueError("examples must be an immutable tuple of strings")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "examples": list(self.examples),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> DocumentFields:
        if not isinstance(value, Mapping) or set(value) != {
            "name",
            "description",
            "parameters",
            "examples",
        }:
            raise ValueError("invalid document fields")
        if any(not isinstance(value[key], str) for key in ("name", "description", "parameters")):
            raise ValueError("document text must be a string")
        examples = value["examples"]
        if not isinstance(examples, list) or any(not isinstance(item, str) for item in examples):
            raise ValueError("document examples must be a string array")
        return cls(
            cast(str, value["name"]),
            cast(str, value["description"]),
            cast(str, value["parameters"]),
            tuple(examples),
        )


def _terms(document: DocumentFields, tokenizer: ChineseTokenizer) -> tuple[dict[str, float], int]:
    counts: Counter[str] = Counter()
    weighted: dict[str, float] = {}
    length = 0
    example_length = 0
    fields = (
        (document.name, 3.0, False),
        (document.description, 2.0, False),
        (document.parameters, 1.0, False),
        (" ".join(document.examples), 0.5, True),
    )
    for text, weight, example in fields:
        for term in tokenizer.tokenize(text):
            if length >= _MAX_DOCUMENT_TOKENS or (
                example and example_length >= _MAX_EXAMPLE_TOKENS
            ):
                break
            if counts[term] >= _MAX_TERM_FREQUENCY:
                continue
            counts[term] += 1
            weighted[term] = weighted.get(term, 0.0) + weight
            length += 1
            if example:
                example_length += 1
    return weighted, length


class BM25Index:
    """Build once per complete catalog; all public statistics are read-only."""

    def __init__(
        self,
        documents: tuple[DocumentFields, ...],
        tokenizer: ChineseTokenizer,
        term_frequencies: tuple[Mapping[str, float], ...],
        lengths: NDArray[np.float64],
        frequencies: Mapping[str, int],
    ) -> None:
        self.documents = documents
        self.tokenizer = tokenizer
        self._term_frequencies = term_frequencies
        self.document_count = len(documents)
        self.document_lengths: NDArray[np.float64] = np.frombuffer(
            lengths.tobytes(), dtype=np.float64
        )
        self.average_length = float(lengths.mean()) if len(lengths) else 0.0
        self.document_frequencies = MappingProxyType(dict(frequencies))
        self.idf = MappingProxyType(
            {
                term: math.log(1 + (self.document_count - count + 0.5) / (count + 0.5))
                for term, count in frequencies.items()
            }
        )

    @classmethod
    def build(
        cls,
        documents: Sequence[DocumentFields],
        tokenizer: ChineseTokenizer | None = None,
    ) -> BM25Index:
        active_tokenizer = tokenizer if tokenizer is not None else ChineseTokenizer()
        full_catalog = tuple(documents)
        if any(not isinstance(document, DocumentFields) for document in full_catalog):
            raise ValueError("catalog must contain DocumentFields")
        frequencies: Counter[str] = Counter()
        weighted: list[Mapping[str, float]] = []
        lengths: list[int] = []
        for document in full_catalog:
            terms, length = _terms(document, active_tokenizer)
            weighted.append(MappingProxyType(terms))
            lengths.append(length)
            frequencies.update(terms.keys())
        return cls(
            full_catalog,
            active_tokenizer,
            tuple(weighted),
            np.asarray(lengths, dtype=np.float64),
            frequencies,
        )

    def _score(self, query: str) -> NDArray[np.float64]:
        """Internal full-catalog scoring; public callers use masked_scores."""
        query_terms = set(self.tokenizer.tokenize(query))
        scores = np.zeros(self.document_count, dtype=np.float64)
        if not query_terms or self.average_length == 0.0:
            return scores
        for index, terms in enumerate(self._term_frequencies):
            normalization = _K1 * (1 - _B + _B * self.document_lengths[index] / self.average_length)
            for term in sorted(query_terms.intersection(terms)):
                tf = terms[term]
                scores[index] += self.idf[term] * tf * (_K1 + 1) / (tf + normalization)
        return scores

    def masked_scores(self, query: str, allowed: Sequence[bool]) -> NDArray[np.float64]:
        mask = validate_mask(allowed, self.document_count)
        scores = self._score(query)
        scores[~mask] = -np.inf
        return scores

    def to_dict(self) -> dict[str, object]:
        return {
            "version": _VERSION,
            "parameters": {
                **_PARAMETERS,
                "field_weights": list(cast(list[float], _PARAMETERS["field_weights"])),
            },
            "tokenizer": self.tokenizer.to_dict(),
            "documents": [document.to_dict() for document in self.documents],
            "document_count": self.document_count,
            "document_lengths": self.document_lengths.tolist(),
            "average_length": self.average_length,
            "document_frequencies": dict(self.document_frequencies),
            "idf": dict(self.idf),
            "term_frequencies": [dict(terms) for terms in self._term_frequencies],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> BM25Index:
        """Validate persisted statistics without re-tokenizing the complete corpus.

        TF occurrence counts, example budgets, document lengths, DF, IDF, and
        average length must agree exactly. Raw-document-to-token correspondence
        is established at build time and bound by the snapshot manifest SHA.
        The caller must load from trusted storage; local hashes do not authenticate
        an attacker who can replace both documents and every persisted statistic.
        No inconsistent statistics are silently repaired, and ACLs are not inputs.
        """
        restored, incompatible = cls._restore(value)
        if incompatible:
            raise ValueError("unsupported tokenizer snapshot profile")
        return restored

    @classmethod
    def _restore(cls, value: Mapping[str, object]) -> tuple[BM25Index, bool]:
        """Validate statistics before a snapshot decides on tokenizer compatibility."""
        expected_keys = {
            "version",
            "parameters",
            "tokenizer",
            "documents",
            "document_count",
            "document_lengths",
            "average_length",
            "document_frequencies",
            "idf",
            "term_frequencies",
        }
        if not isinstance(value, Mapping) or set(value) != expected_keys:
            raise ValueError("invalid BM25 snapshot fields")
        if value["version"] != _VERSION:
            raise ValueError("unsupported BM25 snapshot version")
        if not _same_json(value["parameters"], _PARAMETERS):
            raise ValueError("unsupported BM25 parameters")
        tokenizer_value, documents_value = value["tokenizer"], value["documents"]
        if not isinstance(tokenizer_value, Mapping) or not isinstance(documents_value, list):
            raise ValueError("invalid BM25 tokenizer or documents")
        tokenizer, incompatible = ChineseTokenizer._restore(tokenizer_value)
        documents: list[DocumentFields] = []
        for document in documents_value:
            if not isinstance(document, Mapping):
                raise ValueError("invalid BM25 document")
            documents.append(DocumentFields.from_dict(document))
        count, lengths, rows = (
            value["document_count"],
            value["document_lengths"],
            value["term_frequencies"],
        )
        if (
            type(count) is not int
            or count != len(documents)
            or not isinstance(lengths, list)
            or not isinstance(rows, list)
            or len(lengths) != count
            or len(rows) != count
        ):
            raise ValueError("BM25 snapshot counts do not match its catalog")
        frequencies: Counter[str] = Counter()
        weighted: list[Mapping[str, float]] = []
        for row, length in zip(rows, lengths, strict=True):
            terms = _persisted_row(row, length)
            weighted.append(terms)
            frequencies.update(terms.keys())
        restored = cls(
            tuple(documents),
            tokenizer,
            tuple(weighted),
            np.asarray(lengths, dtype=np.float64),
            frequencies,
        )
        for actual, expected in (
            (value["document_frequencies"], dict(restored.document_frequencies)),
            (value["idf"], dict(restored.idf)),
            (value["average_length"], restored.average_length),
        ):
            if not _same_json(actual, expected):
                raise ValueError("BM25 snapshot statistics do not match its persisted rows")
        return restored, incompatible
