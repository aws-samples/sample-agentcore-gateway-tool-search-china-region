"""Independent BM25 fixtures and catalog-wide statistics invariants."""

import copy
import importlib
import json
import math
from collections.abc import Sequence
from typing import cast

import numpy as np
import pytest

from semantic_core.bm25 import BM25Index, DocumentFields
from semantic_core.tokenization import ChineseTokenizer, TokenizerConfig


def catalog() -> list[DocumentFields]:
    return [
        DocumentFields(name="alpha", description="beta beta"),
        DocumentFields(name="beta", description="gamma"),
    ]


def test_independent_weighted_bm25_math_fixture() -> None:
    index = BM25Index.build(catalog())
    # Document lengths are 3 and 2. Name/description weights are 3 and 2.
    # beta occurs in both catalog documents, giving IDF=ln(1.2).
    expected = [
        math.log(1.2) * 4 * 2.2 / (4 + 1.2 * (0.25 + 0.75 * 3 / 2.5)),
        math.log(1.2) * 3 * 2.2 / (3 + 1.2 * (0.25 + 0.75 * 2 / 2.5)),
    ]
    assert index.document_count == 2
    assert index.average_length == 2.5
    assert index.document_frequencies == {"alpha": 1, "beta": 2, "gamma": 1}
    assert index.idf["beta"] == pytest.approx(math.log(1.2))
    assert index._score("beta") == pytest.approx(expected)


def test_acl_masks_do_not_recompute_catalog_statistics() -> None:
    index = BM25Index.build(catalog())
    before = copy.deepcopy(index.to_dict())
    unrestricted = index.masked_scores("beta", [True, True])
    only_first = index.masked_scores("beta", [True, False])
    only_second = index.masked_scores("beta", [False, True])
    assert only_first[0] == unrestricted[0]
    assert only_second[1] == unrestricted[1]
    assert np.isneginf(only_first[1])
    assert np.isneginf(only_second[0])
    assert index.to_dict() == before
    # Rebuilding an ACL subset changes IDF and must not be the implementation.
    subset = BM25Index.build(catalog()[:1])
    assert subset.idf["beta"] != index.idf["beta"]


def test_empty_catalog_and_unknown_terms() -> None:
    empty = BM25Index.build([])
    assert empty.document_count == 0
    assert empty.average_length == 0
    assert empty.masked_scores("中文", []).shape == (0,)
    assert np.array_equal(BM25Index.build(catalog())._score("missing"), [0, 0])


@pytest.mark.parametrize("mask", [[True], [1, 0], [True, "yes"], [[True, False]]])
def test_bm25_rejects_invalid_acl_mask(mask: object) -> None:
    with pytest.raises(ValueError):
        BM25Index.build(catalog()).masked_scores("beta", cast(Sequence[bool], mask))


def test_field_weights_caps_and_length_are_independent_of_repetitions() -> None:
    short = DocumentFields(name="alpha", description="alpha alpha", examples=("alpha",))
    stuffed = DocumentFields(
        name="alpha",
        description=" ".join(["alpha"] * 1000),
        parameters="alpha",
        examples=(" ".join(["alpha"] * 1000),),
    )
    first, second = BM25Index.build([short]), BM25Index.build([stuffed])
    # Three occurrences across all fields: 3(name)+2+2(description)=7 weighted TF.
    # Neither parameter nor example repetitions can add another occurrence.
    assert first.document_lengths.tolist() == second.document_lengths.tolist() == [3.0]
    assert first._score("alpha") == pytest.approx([math.log(4 / 3) * 7 * 2.2 / 8.2])
    assert np.array_equal(first._score("alpha"), second._score("alpha"))
    assert np.array_equal(first._score("alpha alpha"), first._score("alpha"))


def test_document_and_example_token_budgets() -> None:
    words = " ".join(f"item{i:04}" for i in range(600))
    real = BM25Index.build([DocumentFields(name="", description=words)])
    examples = BM25Index.build([DocumentFields(name="", description="", examples=(words,))])
    assert real.document_lengths.tolist() == [512.0]
    assert examples.document_lengths.tolist() == [128.0]
    assert real._score("item0511")[0] > 0
    assert real._score("item0512")[0] == 0
    assert examples._score("item0127")[0] > 0
    assert examples._score("item0128")[0] == 0


def test_chinese_identifiers_digits_negation_and_units() -> None:
    tokens = ChineseTokenizer().tokenize(
        "取消订单，不删除数据。cancel_order getHTTPServer ＡＰＩ １２３ 2.5 GB"
    )
    assert {"取消", "订单", "不", "删除", "数据"} <= set(tokens)
    assert {"cancel_order", "cancel", "order", "gethttpserver", "get", "http", "server"} <= set(
        tokens
    )
    assert {"api", "123", "2.5", "gb"} <= set(tokens)
    assert not {"。", "，"}.intersection(tokens)


def test_custom_dictionary_synonyms_are_versioned_and_round_trip() -> None:
    tokenizer = ChineseTokenizer(
        TokenizerConfig(
            dictionary_version="domain-2",
            synonyms_version="synonyms-3",
            dictionary=(("星际订单", 1000000),),
            synonyms=(("星际订单", ("order",)), ("cancel", ("取消",))),
        )
    )
    index = BM25Index.build([DocumentFields(name="cancel", description="取消星际订单")], tokenizer)
    restored = BM25Index.from_dict(json.loads(json.dumps(index.to_dict())))
    assert "星际订单" in tokenizer.tokenize("取消星际订单")
    assert restored.tokenizer.tokenize("cancel 星际订单") == tokenizer.tokenize("cancel 星际订单")
    assert restored.to_dict() == index.to_dict()
    assert restored._score("order")[0] > 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", "bm25-v2"),
        ("document_count", True),
        ("document_lengths", [3.0, 100.0]),
        ("average_length", 9.0),
        ("document_frequencies", {"alpha": 2, "beta": 2, "gamma": 1}),
        ("idf", {"alpha": 1.0, "beta": 1.0, "gamma": 1.0}),
        ("term_frequencies", [{"alpha": 100.0}, {"beta": 3.0, "gamma": 2.0}]),
    ],
)
def test_bm25_snapshot_rejects_corrupted_statistics(field: str, value: object) -> None:
    snapshot = BM25Index.build(catalog()).to_dict()
    snapshot[field] = value
    with pytest.raises(ValueError):
        BM25Index.from_dict(snapshot)


def test_bm25_snapshot_rejects_configuration_drift() -> None:
    snapshot = BM25Index.build(catalog()).to_dict()
    tokenizer = cast(dict[str, object], snapshot["tokenizer"])
    tokenizer["base_dictionary_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="tokenizer"):
        BM25Index.from_dict(snapshot)


def test_saved_statistics_and_lengths_cannot_be_mutated() -> None:
    index = BM25Index.build(catalog())
    with pytest.raises(ValueError):
        index.document_lengths.setflags(write=True)
    mutable_idf = cast(dict[str, float], index.idf)
    with pytest.raises(TypeError):
        mutable_idf["alpha"] = 0.0


def test_loading_persisted_statistics_does_not_tokenize_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = BM25Index.build(
        [
            DocumentFields(name="cancel_order", description="取消尚未发货的订单"),
            DocumentFields(name="get_order", description="查询订单状态"),
        ]
    )
    payload = original.to_dict()
    calls: list[str] = []
    tokenize = ChineseTokenizer.tokenize

    def recording_tokenize(self: ChineseTokenizer, text: str) -> tuple[str, ...]:
        calls.append(text)
        return tokenize(self, text)

    with monkeypatch.context() as scoped:
        scoped.setattr(ChineseTokenizer, "tokenize", recording_tokenize)
        restored = BM25Index.from_dict(payload)
    assert calls == [], "loading must validate persisted rows without re-tokenizing the corpus"
    assert restored.to_dict() == payload
    np.testing.assert_array_equal(restored._score("取消订单"), original._score("取消订单"))


def test_tokenizer_instances_share_one_initialized_dictionary_per_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semantic_core import tokenization

    module = importlib.import_module("jieba")
    factory = module.Tokenizer
    builds: list[object] = []

    def recording_factory() -> object:
        segmenter = factory()
        builds.append(segmenter)
        return segmenter

    monkeypatch.setattr(module, "Tokenizer", recording_factory)
    config = TokenizerConfig(dictionary_version="reuse-fixture", synonyms_version="reuse-fixture")
    tokenization._shared_cut.cache_clear()
    try:
        first, second = ChineseTokenizer(config), ChineseTokenizer(config)
        assert first.tokenize("取消订单") == second.tokenize("取消订单")
        assert len(builds) == 1, (
            "equal immutable configs must reuse the same initialized dictionary"
        )
    finally:
        tokenization._shared_cut.cache_clear()


def test_shared_dictionary_customization_cannot_mutate_another_profile() -> None:
    phrase = "火星专用交换指令"
    baseline = ChineseTokenizer(TokenizerConfig(dictionary_version="isolation-fixture"))
    baseline_tokens = baseline.tokenize(phrase)
    custom = ChineseTokenizer(
        TokenizerConfig(dictionary_version="custom-fixture", dictionary=((phrase, 1000000),))
    )
    assert custom.tokenize(phrase) == (phrase,)
    assert baseline.tokenize(phrase) == baseline_tokens
    assert baseline_tokens != (phrase,)


def test_average_length_remains_catalog_wide_after_each_mask() -> None:
    documents = [
        DocumentFields(name="item", description=" ".join(f"term{i}" for i in range(count)))
        for count in (1, 7, 21)
    ]
    restored = BM25Index.from_dict(BM25Index.build(documents).to_dict())
    expected_average = (2 + 8 + 22) / 3
    for allowed in ([True, False, False], [False, True, True], [False, False, False]):
        restored.masked_scores("item", allowed)
        assert restored.average_length == expected_average
        assert restored.document_lengths.tolist() == [2.0, 8.0, 22.0]


@pytest.mark.parametrize("tf", [0.0, -0.5, 7.5, 8.5, 9.5, True, 3, float("nan"), float("inf")])
def test_persisted_tf_requires_achievable_weighted_occurrences(tf: object) -> None:
    payload = BM25Index.build([DocumentFields("alpha", "")]).to_dict()
    payload["term_frequencies"] = [{"alpha": tf}]
    with pytest.raises(ValueError, match="frequency"):
        BM25Index.from_dict(payload)


@pytest.mark.parametrize(("tf", "length"), [(9.0, 1.0), (0.5, 2.0), (3.0, 4.0), (3.0, 1)])
def test_persisted_lengths_must_match_feasible_retained_counts(tf: float, length: object) -> None:
    payload = BM25Index.build([DocumentFields("alpha", "")]).to_dict()
    payload["term_frequencies"] = [{"alpha": tf}]
    payload["document_lengths"] = [length]
    payload["average_length"] = length
    with pytest.raises(ValueError, match="length"):
        BM25Index.from_dict(payload)


def test_persisted_length_cannot_hide_excess_example_occurrences() -> None:
    # Each weighted TF=1 may represent one parameter token or two examples.
    # Length=130 forces all 65 terms to be two examples, exceeding the cap=128.
    payload = BM25Index.build([DocumentFields("alpha", "")]).to_dict()
    terms = {f"term{i}": 1.0 for i in range(65)}
    payload["term_frequencies"] = [terms]
    payload["document_lengths"] = [130.0]
    payload["average_length"] = 130.0
    payload["document_frequencies"] = dict.fromkeys(terms, 1)
    payload["idf"] = dict.fromkeys(terms, math.log(4 / 3))
    with pytest.raises(ValueError, match="example token budget"):
        BM25Index.from_dict(payload)


def test_example_budget_boundary_loads_without_tokenizing() -> None:
    words = " ".join(f"term{i} term{i}" for i in range(64))
    original = BM25Index.build([DocumentFields("", "", examples=(words,))])
    assert original.document_lengths.tolist() == [128.0]
    assert BM25Index.from_dict(original.to_dict()).to_dict() == original.to_dict()
