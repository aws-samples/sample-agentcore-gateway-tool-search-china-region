"""Versioned SYNTHETIC regression data; never a human quality acceptance set.

Semantic labels are derived from CRUD/system/resource intent before applying
ACLs. Evaluation must intersect ``relevant_ids`` with ``allowed_ids``. Example
queries come only from the retrieval core's deterministic mock LLM and never supply test truth.
"""

from __future__ import annotations

import json
import random
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

from semantic_core.catalog import GatewayIdentity, ToolIdentity, ToolRecord
from semantic_core.providers import MockLLMProvider, RequestContext

SCHEMA_VERSION = "synthetic-v1"
DEFAULT_SEED = 20261010
NOTICE = (
    "SYNTHETIC DATA / 合成数据. Deterministic offline regression fixture only; "
    "not human-labeled holdout data, not quality acceptance, and not approval "
    "to enable reranking."
)
_ACTIONS = (("create", "新建"), ("read", "查看"), ("update", "修改"), ("delete", "删除"))
_SYSTEMS = (
    (
        "crm",
        "customer relationship",
        "客户关系",
        (
            ("customer", "客户"),
            ("contact", "联系人"),
            ("lead", "销售线索"),
            ("opportunity", "商机"),
            ("account", "企业账户"),
        ),
    ),
    (
        "finance",
        "finance",
        "财务",
        (
            ("invoice", "发票"),
            ("payment", "付款"),
            ("refund", "退款"),
            ("budget", "预算"),
            ("expense", "费用"),
        ),
    ),
    (
        "hr",
        "human resources",
        "人力资源",
        (
            ("employee", "员工"),
            ("leave", "请假申请"),
            ("payroll", "薪资记录"),
            ("candidate", "候选人"),
            ("department", "部门"),
        ),
    ),
    (
        "inventory",
        "inventory",
        "库存",
        (
            ("product", "商品"),
            ("stock", "库存记录"),
            ("warehouse", "仓库"),
            ("shipment", "发货单"),
            ("supplier", "供应商"),
        ),
    ),
    (
        "support",
        "customer support",
        "客服",
        (
            ("ticket", "工单"),
            ("incident", "故障事件"),
            ("knowledge", "知识条目"),
            ("sla", "服务协议"),
            ("feedback", "反馈"),
        ),
    ),
)


@dataclass(frozen=True)
class EvalTool:
    id: str
    gateway_id: str
    system: str
    family: str
    language: str
    descriptor: dict[str, object]
    examples: tuple[str, ...]


@dataclass(frozen=True)
class EvalQuery:
    id: str
    text: str
    language: str
    gateway_id: str
    allowed_ids: tuple[str, ...]
    relevant_ids: tuple[str, ...]
    permission_group: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Dataset:
    seed: int
    tools: tuple[EvalTool, ...]
    queries: tuple[EvalQuery, ...]


def _descriptor(
    system_id: str,
    system_en: str,
    system_zh: str,
    resource: tuple[str, str],
    action: tuple[str, str],
    language: str,
) -> dict[str, object]:
    entity_en, entity_zh = resource
    action_en, action_zh = action
    descriptions = {
        "zh": f"在{system_zh}系统中{action_zh}{entity_zh}记录；仅操作该资源，不处理关联资源。",
        "en": (
            f"{action_en.capitalize()} a {entity_en} record in the {system_en} system. "
            "Only this resource is affected, not related resources."
        ),
        "mixed": (
            f"{system_id.upper()} {system_zh}: {action_en} {entity_en} {entity_zh} record；"
            "仅操作该资源，不处理关联资源。"
        ),
    }
    properties: dict[str, object] = {
        "record_id": {"type": "string", "description": "Exact record identifier / 记录ID"}
    }
    required = ["record_id"]
    if action_en == "create":
        properties = {
            "name": {"type": "string", "description": "New record name / 新记录名称"},
            "fields": {"type": "object", "description": "Initial field values / 初始字段"},
        }
        required = ["name", "fields"]
    elif action_en == "update":
        properties["changes"] = {"type": "object", "description": "Changed fields / 待修改字段"}
        required.append("changes")
    return {
        "name": f"{system_id}___{action_en}_{entity_en}",
        "description": descriptions[language],
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


def _tools() -> tuple[EvalTool, ...]:
    provider = MockLLMProvider()
    tools: list[EvalTool] = []
    for gateway_index in range(5):
        gateway = f"gateway-{gateway_index + 1}"
        for system_id, system_en, system_zh, resources in _SYSTEMS:
            for resource in resources:
                for action in _ACTIONS:
                    language = ("zh", "en", "mixed")[len(tools) % 3]
                    descriptor = _descriptor(
                        system_id, system_en, system_zh, resource, action, language
                    )
                    description = cast(str, descriptor["description"])
                    examples = provider.generate_queries(
                        description,
                        ("zh", "en"),
                        6,
                        RequestContext(f"synthetic-example-{len(tools)}", time.monotonic() + 10),
                    ).queries
                    tools.append(
                        EvalTool(
                            f"{gateway}/{descriptor['name']}",
                            gateway,
                            system_id,
                            f"{system_id}:{resource[0]}",
                            language,
                            descriptor,
                            examples,
                        )
                    )
    return tuple(tools)


def _query_text(
    system_en: str,
    system_zh: str,
    resources: tuple[tuple[str, str], ...],
    actions: tuple[tuple[str, str], ...],
    style: int,
) -> str:
    entities_en = ", ".join(resource[0] for resource in resources)
    entities_zh = "、".join(resource[1] for resource in resources)
    actions_en = ", ".join(action[0] for action in actions)
    actions_zh = "、".join(action[1] for action in actions)
    if style == 0:
        return f"查找{system_zh}系统中支持{actions_zh}{entities_zh}记录的工具。"
    if style == 1:
        return f"我需要处理{system_zh}的{entities_zh}，操作范围为{actions_zh}；找出相应工具。"
    if style == 2:
        return (
            f"Find tools in the {system_en} system for these operations: {actions_en}; "
            f"resource types: {entities_en}."
        )
    return (
        f"在{system_zh}系统执行 {actions_en}；资源类型为 {entities_en}（{entities_zh}），"
        "请找出相应工具。"
    )


def _queries(tools: tuple[EvalTool, ...], rng: random.Random) -> tuple[EvalQuery, ...]:
    queries: list[EvalQuery] = []
    permission_sizes: tuple[tuple[int, tuple[int, ...]], ...] = (
        (100, (0, 1, 2, 3, 4, 5, 20)),
        (10, (0, 1, 2, 3, 4, 5, 10)),
        (1, (0, 1)),
        (0, (0,)),
    )
    for gateway_index in range(5):
        gateway = f"gateway-{gateway_index + 1}"
        gateway_tools = tuple(tool for tool in tools if tool.gateway_id == gateway)
        for style in range(4):
            language = ("zh", "zh", "en", "mixed")[style]
            for allowed_count, cardinalities in permission_sizes:
                for desired_count in cardinalities:
                    system_id, system_en, system_zh, all_resources = rng.choice(_SYSTEMS)
                    resources: tuple[tuple[str, str], ...]
                    if desired_count <= 4:
                        resources = (rng.choice(all_resources),)
                        actions = tuple(rng.sample(_ACTIONS, max(desired_count, 1)))
                    elif desired_count == 5:
                        resources = all_resources
                        actions = (rng.choice(_ACTIONS),)
                    else:
                        resources = all_resources
                        actions = _ACTIONS
                    resource_names = {resource[0] for resource in resources}
                    action_names = {action[0] for action in actions}
                    relevant = tuple(
                        tool.id
                        for tool in gateway_tools
                        if tool.system == system_id
                        and tool.family.split(":", 1)[1] in resource_names
                        and str(tool.descriptor["name"]).split("___", 1)[1].split("_", 1)[0]
                        in action_names
                    )
                    text = _query_text(system_en, system_zh, resources, actions, style)
                    intent = "crud"
                    if desired_count == 0 and allowed_count == 100:
                        intent = "outside_catalog"
                        text = (
                            "寻找控制深空望远镜镜头朝向的工具。"
                            if language == "zh"
                            else "Find a tool to steer a deep-space telescope."
                            if language == "en"
                            else "寻找调整 deep-space telescope 镜头朝向的工具。"
                        )
                        relevant = ()
                    authorized_matches = rng.sample(relevant, min(desired_count, len(relevant)))
                    irrelevant = [tool.id for tool in gateway_tools if tool.id not in relevant]
                    allowed = authorized_matches + rng.sample(
                        irrelevant, allowed_count - len(authorized_matches)
                    )
                    tags: tuple[str, ...] = (
                        f"intent:{intent}",
                        f"system:{system_id}",
                        "resources:" + ",".join(resource[0] for resource in resources),
                        "actions:" + ",".join(action[0] for action in actions),
                        f"authorized_count:{desired_count}",
                        "label_source:synthetic_rule",
                    )
                    if desired_count == 0 and relevant:
                        tags += ("permission_negative",)
                    queries.append(
                        EvalQuery(
                            f"query-{len(queries) + 1:04d}",
                            text,
                            language,
                            gateway,
                            tuple(sorted(allowed)),
                            tuple(sorted(relevant)),
                            f"{allowed_count}%",
                            tags,
                        )
                    )
    return tuple(queries)


def generate_dataset(seed: int = DEFAULT_SEED) -> Dataset:
    """Generate 500 tools and 340 queries without network or credentials."""
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("dataset seed must be an integer")
    tools = _tools()
    return Dataset(seed, tools, _queries(tools, random.Random(seed)))  # nosec B311


def _validate(dataset: Dataset) -> None:
    if isinstance(dataset.seed, bool) or not isinstance(dataset.seed, int):
        raise ValueError("invalid dataset seed")
    tool_by_id = {tool.id: tool for tool in dataset.tools}
    if len(tool_by_id) != len(dataset.tools):
        raise ValueError("duplicate tool ID")
    if len({query.id for query in dataset.queries}) != len(dataset.queries):
        raise ValueError("duplicate query ID")
    for tool in dataset.tools:
        if tool.language not in ("zh", "en", "mixed"):
            raise ValueError("invalid tool language")
        identity = ToolIdentity(
            GatewayIdentity("synthetic", "local", tool.gateway_id),
            tool.system,
            SCHEMA_VERSION,
            _string(tool.descriptor, "name"),
        )
        ToolRecord.from_descriptor(identity, tool.descriptor, tool.examples)
    gateway_counts = Counter(tool.gateway_id for tool in dataset.tools)
    for query in dataset.queries:
        if query.language not in ("zh", "en", "mixed"):
            raise ValueError("invalid query language")
        if query.permission_group not in ("0%", "1%", "10%", "100%"):
            raise ValueError("invalid permission group")
        for ids in (query.allowed_ids, query.relevant_ids):
            if len(set(ids)) != len(ids) or any(
                tool_id not in tool_by_id or tool_by_id[tool_id].gateway_id != query.gateway_id
                for tool_id in ids
            ):
                raise ValueError("invalid or cross-gateway query reference")
        expected_allowed = gateway_counts[query.gateway_id] * int(query.permission_group[:-1]) / 100
        if len(query.allowed_ids) != expected_allowed:
            raise ValueError("permission group does not match allowed tool proportion")


def write_dataset(dataset: Dataset, path: Path) -> None:
    """Write deterministic UTF-8 JSON; metadata cannot imply real quality."""
    _validate(dataset)
    value = {
        "schema_version": SCHEMA_VERSION,
        "synthetic": True,
        "quality_acceptance": False,
        "notice": NOTICE,
        "example_provider": "mock-llm/fixture-v1",
        "example_prompt_version": "mock-queries-v1",
        "label_source": "independent deterministic CRUD/system/resource rules",
        "relevance_definition": "relevant_authorized = relevant_ids intersect allowed_ids",
        **asdict(dataset),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("expected JSON object")
    return cast(dict[str, object], value)


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("expected JSON array")
    return cast(list[object], value)


def _string(value: dict[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"invalid dataset field {key}")
    return item


def _strings(value: object) -> tuple[str, ...]:
    items = _array(value)
    if any(not isinstance(item, str) or not item.strip() for item in items):
        raise ValueError("invalid string array")
    return tuple(cast(list[str], items))


def load_dataset(path: Path) -> Dataset:
    """Read a versioned synthetic corpus and reject invalid references/provenance."""
    value = _object(json.loads(path.read_text(encoding="utf-8")))
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or value.get("synthetic") is not True
        or value.get("quality_acceptance") is not False
        or value.get("notice") != NOTICE
        or value.get("example_provider") != "mock-llm/fixture-v1"
    ):
        raise ValueError("invalid synthetic dataset provenance")
    seed = value.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("invalid dataset seed")
    tools: list[EvalTool] = []
    for item in _array(value.get("tools")):
        tool = _object(item)
        tools.append(
            EvalTool(
                _string(tool, "id"),
                _string(tool, "gateway_id"),
                _string(tool, "system"),
                _string(tool, "family"),
                _string(tool, "language"),
                _object(tool.get("descriptor")),
                _strings(tool.get("examples")),
            )
        )
    queries: list[EvalQuery] = []
    for item in _array(value.get("queries")):
        query = _object(item)
        queries.append(
            EvalQuery(
                _string(query, "id"),
                _string(query, "text"),
                _string(query, "language"),
                _string(query, "gateway_id"),
                _strings(query.get("allowed_ids")),
                _strings(query.get("relevant_ids")),
                _string(query, "permission_group"),
                _strings(query.get("tags")),
            )
        )
    dataset = Dataset(seed, tuple(tools), tuple(queries))
    _validate(dataset)
    return dataset
