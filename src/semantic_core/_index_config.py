"""新索引采用的版本化分词配置；旧快照始终恢复其持久化配置。"""

from semantic_core.tokenization import TokenizerConfig

# 词组只表示业务操作和资源，不把“查找工具”等发现意图当成 read。
# 修改内容时同时递增版本；文档和查询使用同一组单跳扩展。
_GROUPS = (
    ("create", "新增", "新建", "创建", "添加"),
    ("read", "查看", "查询", "读取"),
    ("update", "更新", "修改", "编辑"),
    ("delete", "删除", "移除"),
    ("product", "商品", "产品"),
)
TOKENIZER_CONFIG = TokenizerConfig(
    synonyms_version="synonyms-v2",
    synonyms=tuple(
        (term, tuple(alias for alias in group if alias != term))
        for group in _GROUPS
        for term in group
    ),
)
