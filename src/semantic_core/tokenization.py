"""Versioned, offline Chinese/identifier tokenization for retrieval copies."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata

# Python requirement is >=3.12,<3.13; no pre-3.7 compatibility.
# nosemgrep: python37-compatibility-importlib2
import importlib.resources
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from threading import RLock
from typing import Protocol, cast

from semantic_core._dictionary import (
    BASE_DICTIONARY_SHA256,
    JIEBA_VERSION,
    load_dictionary,
)
from semantic_core._timing import stage

_VERSION = "tokenizer-v1"
_JIEBA_VERSION = JIEBA_VERSION
_PARTS = re.compile(
    r"[A-Za-z][A-Za-z0-9]*(?:_+[A-Za-z0-9]+)*"
    r"|\d+(?:\.\d+)?"
    r"|[\u3400-\u4dbf\u4e00-\u9fff]+"
    r"|[^\W_]+",
    re.UNICODE,
)
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")
_NUMBER = re.compile(r"\d+(?:\.\d+)?\Z")
_CAMEL_PARTS = re.compile(r"[A-Z]+(?=[A-Z][a-z]|[0-9]|$)|[A-Z]?[a-z]+|[0-9]+")


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).lower()


def _single_term(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 256
        and value == _normalized(value)
        and _PARTS.fullmatch(value) is not None
    )


def _version_label(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 128 and value.strip() == value


@dataclass(frozen=True)
class TokenizerConfig:
    """All vocabulary decisions are persisted, including empty vocabularies.

    Synonyms expand one hop; no stop-word removal drops negation, units, or digits.
    New configurations require a new dictionary/synonym version chosen by the caller.
    """

    version: str = _VERSION
    dictionary_version: str = "business-v1"
    synonyms_version: str = "synonyms-v1"
    dictionary: tuple[tuple[str, int], ...] = ()
    synonyms: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def __post_init__(self) -> None:
        if self.version != _VERSION:
            raise ValueError("unsupported tokenizer version")
        if not all(_version_label(v) for v in (self.dictionary_version, self.synonyms_version)):
            raise ValueError("dictionary and synonym versions must be nonempty labels")
        if not isinstance(self.dictionary, tuple) or not isinstance(self.synonyms, tuple):
            raise ValueError("dictionary and synonyms must be immutable tuples")
        seen: set[str] = set()
        for entry in self.dictionary:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ValueError("invalid dictionary entry")
            term, frequency = entry
            if (
                not _single_term(term)
                or type(frequency) is not int
                or frequency < 1
                or term in seen
            ):
                raise ValueError("dictionary terms/frequencies must be valid and unique")
            seen.add(term)
        seen.clear()
        for synonym_entry in self.synonyms:
            if not isinstance(synonym_entry, tuple) or len(synonym_entry) != 2:
                raise ValueError("invalid synonym entry")
            source, targets = synonym_entry
            if (
                not _single_term(source)
                or source in seen
                or not isinstance(targets, tuple)
                or not targets
                or any(not _single_term(term) for term in targets)
                or len(set(targets)) != len(targets)
                or source in targets
            ):
                raise ValueError("synonym terms must be valid, unique, and one hop")
            seen.add(source)

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "dictionary_version": self.dictionary_version,
            "synonyms_version": self.synonyms_version,
            "dictionary": [[term, frequency] for term, frequency in self.dictionary],
            "synonyms": [[term, list(targets)] for term, targets in self.synonyms],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> TokenizerConfig:
        keys = {"version", "dictionary_version", "synonyms_version", "dictionary", "synonyms"}
        if not isinstance(value, Mapping) or set(value) != keys:
            raise ValueError("invalid tokenizer configuration fields")
        versions = [value[key] for key in ("version", "dictionary_version", "synonyms_version")]
        if any(not isinstance(version, str) for version in versions):
            raise ValueError("tokenizer versions must be strings")
        dictionary_value, synonyms_value = value["dictionary"], value["synonyms"]
        if not isinstance(dictionary_value, list) or not isinstance(synonyms_value, list):
            raise ValueError("dictionary and synonyms must be arrays")
        dictionary: list[tuple[str, int]] = []
        for entry in dictionary_value:
            if (
                not isinstance(entry, list)
                or len(entry) != 2
                or not isinstance(entry[0], str)
                or type(entry[1]) is not int
            ):
                raise ValueError("invalid serialized dictionary entry")
            dictionary.append((entry[0], entry[1]))
        synonyms: list[tuple[str, tuple[str, ...]]] = []
        for entry in synonyms_value:
            if (
                not isinstance(entry, list)
                or len(entry) != 2
                or not isinstance(entry[0], str)
                or not isinstance(entry[1], list)
                or any(not isinstance(term, str) for term in entry[1])
            ):
                raise ValueError("invalid serialized synonym entry")
            synonyms.append((entry[0], tuple(entry[1])))
        return cls(
            version=cast(str, versions[0]),
            dictionary_version=cast(str, versions[1]),
            synonyms_version=cast(str, versions[2]),
            dictionary=tuple(dictionary),
            synonyms=tuple(synonyms),
        )


class _Segmenter(Protocol):
    FREQ: dict[str, int]
    total: int
    initialized: bool

    def add_word(self, word: str, freq: int) -> None: ...

    def cut(self, sentence: str, cut_all: bool, HMM: bool) -> Iterable[str]: ...


class _JiebaModule(Protocol):
    def Tokenizer(self) -> _Segmenter: ...


def _jieba_module() -> _JiebaModule:
    # 包括首次导入或 sys.modules 缓存查找；资源定位不能隐式吞下首次导入。
    with stage("tokenizer.import"):
        return cast(_JiebaModule, importlib.import_module("jieba"))


@lru_cache(maxsize=1)
def _base_dictionary_digest() -> str:
    _jieba_module()
    with stage("tokenizer.base_dictionary"):
        dictionary = importlib.resources.files("jieba").joinpath("dict.txt").read_bytes()
        return hashlib.sha256(dictionary).hexdigest()


_SEGMENTER_LOCK = RLock()


@lru_cache(maxsize=4)
def _shared_cut(dictionary: tuple[tuple[str, int], ...]) -> Callable[[str], tuple[str, ...]]:
    """Publish only a cutting closure after all dictionary mutation has finished."""
    module = _jieba_module()
    segmenter = module.Tokenizer()
    if _base_dictionary_digest() != BASE_DICTIONARY_SHA256:
        raise ValueError("installed jieba dictionary does not match prebuilt dictionary")
    with stage("tokenizer.dictionary"):
        segmenter.FREQ, segmenter.total = load_dictionary()
        segmenter.initialized = True
        # 保留既有业务词条语义；不调用基础词典 initialize/gen_pfdict。
        for term, frequency in dictionary:
            segmenter.add_word(term, freq=frequency)

    def cut(text: str) -> tuple[str, ...]:
        return tuple(segmenter.cut(text, cut_all=False, HMM=False))

    return cut


class ChineseTokenizer:
    """Exact mode, HMM off; reuse up to four immutable business dictionaries.

    Only cutting closures are shared. Dictionary mutation is complete before a
    cache entry becomes visible, and no tokenizer API exposes the jieba builder.
    Initialization is serialized because lru_cache alone permits duplicate fills.
    Profile labels and synonyms remain per instance, outside the dictionary cache.
    """

    def __init__(self, config: TokenizerConfig | None = None) -> None:
        self.config = config if config is not None else TokenizerConfig()
        if importlib.metadata.version("jieba") != _JIEBA_VERSION:
            raise ValueError("installed jieba version does not match tokenizer profile")
        with _SEGMENTER_LOCK:
            self._cut = _shared_cut(self.config.dictionary)
        self._synonyms = dict(self.config.synonyms)

    def tokenize(self, text: str) -> tuple[str, ...]:
        if not isinstance(text, str):
            raise ValueError("retrieval text must be a string")
        result: list[str] = []
        for match in _PARTS.finditer(unicodedata.normalize("NFKC", text)):
            token = match.group()
            normalized = token.lower()
            if _IDENTIFIER.fullmatch(token):
                tokens = [normalized]
                split = tuple(part.lower() for part in _CAMEL_PARTS.findall(token))
                if "_" in token or (len(split) > 1 and any(c.isupper() for c in token)):
                    tokens.extend(split)
            elif _NUMBER.fullmatch(token):
                tokens = [normalized]
            else:
                tokens = list(self._cut(normalized))
            for part in tokens:
                if not part.strip():
                    continue
                result.append(part)
                result.extend(self._synonyms.get(part, ()))
        return tuple(result)

    def to_dict(self) -> dict[str, object]:
        return {
            "config": self.config.to_dict(),
            "jieba_version": _JIEBA_VERSION,
            "base_dictionary_sha256": _base_dictionary_digest(),
            "mode": "exact",
            "hmm": False,
            "stopwords": [],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ChineseTokenizer:
        tokenizer, incompatible = cls._restore(value)
        if incompatible:
            raise ValueError("unsupported tokenizer snapshot profile")
        return tokenizer

    @classmethod
    def _restore(cls, value: Mapping[str, object]) -> tuple[ChineseTokenizer, bool]:
        """Validate using this runtime; retain a base-environment mismatch marker.

        The returned tokenizer is for snapshot validation only when the marker
        is true. Public loaders must reject it after all remaining checks.
        """
        keys = {"config", "jieba_version", "base_dictionary_sha256", "mode", "hmm", "stopwords"}
        if not isinstance(value, Mapping) or set(value) != keys:
            raise ValueError("invalid tokenizer snapshot fields")
        if (
            not _version_label(value["jieba_version"])
            or not isinstance(value["base_dictionary_sha256"], str)
            or re.fullmatch("[0-9a-f]{64}", value["base_dictionary_sha256"]) is None
            or value["mode"] != "exact"
            or value["hmm"] is not False
            or value["stopwords"] != []
        ):
            raise ValueError("unsupported tokenizer snapshot profile")
        config = value["config"]
        if not isinstance(config, Mapping):
            raise ValueError("invalid tokenizer configuration")
        tokenizer = cls(TokenizerConfig.from_dict(config))
        incompatible = (
            value["jieba_version"] != _JIEBA_VERSION
            or value["base_dictionary_sha256"] != _base_dictionary_digest()
        )
        return tokenizer, incompatible
