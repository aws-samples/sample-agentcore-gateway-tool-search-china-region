"""同词典跨快照版本复用切词器，但同义词输出仍由各自配置决定。"""

import pytest

from semantic_core import tokenization
from semantic_core.tokenization import ChineseTokenizer, TokenizerConfig


@pytest.fixture(autouse=True)
def isolated_dictionary_cache():
    tokenization._shared_cut.cache_clear()
    yield
    tokenization._shared_cut.cache_clear()


def test_same_dictionary_shares_segmenter_without_sharing_synonyms(monkeypatch):
    loads = []
    original_load = tokenization.load_dictionary

    def loading():
        loads.append("load")
        return original_load()

    monkeypatch.setattr(tokenization, "load_dictionary", loading)
    configs = (
        TokenizerConfig(),
        TokenizerConfig(synonyms_version="synonyms-v2", synonyms=(("create", ("新增",)),)),
        TokenizerConfig(
            dictionary_version="business-relabeled",
            synonyms_version="synonyms-v3",
            synonyms=(("create", ("新建",)),),
        ),
    )
    tokenizers = [ChineseTokenizer(config) for config in configs]

    assert loads == ["load"]
    assert all(tokenizer._cut is tokenizers[0]._cut for tokenizer in tokenizers)
    assert [tokenizer.tokenize("create") for tokenizer in tokenizers] == [
        ("create",),
        ("create", "新增"),
        ("create", "新建"),
    ]
    assert [tokenizer.to_dict()["config"] for tokenizer in tokenizers] == [
        config.to_dict() for config in configs
    ]
    info = tokenization._shared_cut.cache_info()
    assert (info.hits, info.misses, info.maxsize, info.currsize) == (2, 1, 4, 1)


def test_different_business_dictionary_keeps_segmenter_and_output_isolated():
    phrase = "缓存迁移专用词条"
    original = ChineseTokenizer()
    before = original.tokenize(phrase)
    custom = ChineseTokenizer(TokenizerConfig(dictionary=((phrase, 1_000_000_000),)))

    assert len(before) > 1
    assert custom.tokenize(phrase) == (phrase,)
    assert custom._cut is not original._cut
    assert original.tokenize(phrase) == before
    assert tokenization._shared_cut.cache_info().currsize == 2


def test_dictionary_cache_stays_bounded_and_evicts_least_recent_dictionary():
    configs = [
        TokenizerConfig(dictionary=(("缓存迁移专用词条", frequency),)) for frequency in range(1, 6)
    ]
    first = ChineseTokenizer(configs[0])
    for config in configs[1:]:
        ChineseTokenizer(config)
    info = tokenization._shared_cut.cache_info()
    assert (info.misses, info.maxsize, info.currsize) == (5, 4, 4)

    replacement = ChineseTokenizer(configs[0])
    assert replacement._cut is not first._cut
    assert tokenization._shared_cut.cache_info().misses == 6
    assert tokenization._shared_cut.cache_info().currsize == 4
    assert replacement.tokenize("缓存迁移专用词条") == first.tokenize("缓存迁移专用词条")
