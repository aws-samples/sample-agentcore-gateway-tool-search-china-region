"""读取随包生成的基础词典；不执行代码、不写缓存、不回退到运行时构建。"""

from __future__ import annotations

import hashlib

# Python requirement is >=3.12,<3.13; no pre-3.7 compatibility.
# nosemgrep: python37-compatibility-importlib2
import importlib.resources
import json
from typing import cast

from semantic_core._timing import stage

JIEBA_VERSION = "0.42.1"
BASE_DICTIONARY_SHA256 = "7197c3211ddd98962b036cdf40324d1ea2bfaa12bd028e68faa70111a88e12a8"
ARTIFACT_NAME = "jieba-prefix-v2.json"
ARTIFACT_SHA256 = "5556137584bb76827f0aee36254839e2f4efb43ea894f0cba94968007d8b5521"


def load_dictionary() -> tuple[dict[str, int], int]:
    """每次返回独立词频表，避免自定义词条污染其它不可变配置。"""
    with stage("tokenizer.dictionary.read"):
        try:
            data = (
                importlib.resources.files("semantic_core")
                .joinpath("data", ARTIFACT_NAME)
                .read_bytes()
            )
        except OSError:
            raise ValueError(
                "prebuilt tokenizer dictionary is missing; rebuild the package"
            ) from None
        # 校验固定构建产物后才解码；这里不是来自快照或外部请求的可变缓存。
        if hashlib.sha256(data).hexdigest() != ARTIFACT_SHA256:
            raise ValueError("prebuilt tokenizer dictionary digest mismatch")
    with stage("tokenizer.dictionary.decode"):
        value = json.loads(data)
        # 两列数组避免 JSON 解码器为近五十万个对象键建立临时映射；
        # 词频表恢复属于解码成本，必须包含在同一个计时阶段内。
        frequencies = dict(zip(value["words"], value["frequencies"], strict=True))
    # SHA-256 已绑定完整结构、所有词频与 total；无需再次遍历近五十万项。
    return cast(dict[str, int], frequencies), cast(int, value["total"])
