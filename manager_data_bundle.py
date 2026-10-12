"""Strict portable schema for the manager data component of a ZIP bundle."""

from __future__ import annotations

import copy
import json
import math

from .export_names import export_name
from .identity import authors_match
from .source_bundle import validate_bindings
from .transfer_bundle import MAX_JSON, component, exact_keys

FORMAT = "astrbot-plugin-manager-data"


def validate_manager_data(value) -> dict:
    if (
        not isinstance(value, dict)
        or value.get("format") != FORMAT
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
    ):
        raise ValueError("插件包中的管家数据格式无效，仅支持 v1。")
    exact_keys(value, {"format", "schema_version", "name", "sources", "rule"})
    name = export_name(value["name"])
    if not name or name != value["name"]:
        raise ValueError("管家数据名称不能为空或包含首尾空白。")
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_JSON:
        raise ValueError("管家数据超过 8 MiB 限制。")
    if not isinstance(value["sources"], list) or len(value["sources"]) > 256:
        raise ValueError("管家数据最多包含 256 个插件的来源关联。")
    seen = set()
    for entry in value["sources"]:
        exact_keys(
            entry, {"directory", "name", "author", "bindings", "active_binding_id"}
        )
        component(entry["directory"], module=True)
        component(entry["name"], module=True)
        if (
            not isinstance(entry["author"], str)
            or len(entry["author"]) > 512
            or not authors_match(entry["author"], entry["author"], market=False)
        ):
            raise ValueError("来源关联的插件作者无效。")
        if entry["name"].casefold() in seen:
            raise ValueError("管家数据包含重复插件来源关联。")
        seen.add(entry["name"].casefold())
        validate_bindings(entry["bindings"], entry["active_binding_id"])
    rule = value["rule"]
    exact_keys(rule, {"rule_version", "updated_at", "selections"})
    if type(rule["rule_version"]) is not int or rule["rule_version"] != 1:
        raise ValueError("导出规则版本不受支持。")
    stamp = rule["updated_at"]
    if stamp is not None and (
        type(stamp) not in {int, float} or not math.isfinite(stamp) or stamp < 0
    ):
        raise ValueError("导出规则保存时间无效。")
    validate_selections(rule["selections"], identities=True)
    for selection in rule["selections"]:
        component(selection["name"], module=True)
        if (
            not isinstance(selection["plugin_id"], str)
            or not selection["plugin_id"]
            or len(selection["plugin_id"]) > 1024
        ):
            raise ValueError("导出规则的插件数据标识无效。")
    return copy.deepcopy(value)


def validate_selections(selections, *, identities: bool = False) -> None:
    if not isinstance(selections, list) or len(selections) > 256:
        raise ValueError("最多选择 256 个插件。")
    seen = set()
    for selection in selections:
        expected = {"directory", "config", "data"} | (
            {"name", "plugin_id"} if identities else set()
        )
        exact_keys(selection, expected)
        component(selection["directory"], module=True)
        folded = selection["directory"].casefold()
        if (
            folded in seen
            or type(selection["config"]) is not bool
            or type(selection["data"]) is not bool
        ):
            raise ValueError("插件选择重复或组件标记不是布尔值。")
        seen.add(folded)
