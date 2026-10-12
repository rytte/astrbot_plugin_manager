"""Restricted manager state carried only inside a plugin bundle."""

from __future__ import annotations

import asyncio
import copy
import json
import time

from .identity import authors_match
from .manager_data_bundle import FORMAT, validate_manager_data
from .sources import fingerprint
from .transfer_bundle import MAX_JSON, exact_keys, write_json


class ManagerDataService:
    def __init__(self, sources, transfers) -> None:
        self.sources = sources
        self.transfers = transfers

    async def snapshot(self) -> tuple[dict, list[str]]:
        entries, warnings = [], []
        for record in await self.sources.inventory():
            if not (record["current_source"] or record["pending_source"]):
                continue
            local = record["local"]
            if local["reserved"] or local["problem"] or record["identity_changed"]:
                warnings.append(
                    f"{local['directory']}：插件身份不可确认，未导出来源关联。"
                )
                continue
            snapshot, notices = self.sources.portable_sources(record)
            warnings.extend(notices)
            if snapshot["source"]:
                entries.append(
                    {
                        "directory": local["directory"],
                        **snapshot["identity"],
                        "source": snapshot["source"],
                    }
                )
        return validate_manager_data(
            {
                "format": FORMAT,
                "schema_version": 1,
                "name": "管家来源与规则",
                "sources": entries,
                "rule": self.transfers.rule(),
            }
        ), warnings

    def capture(self) -> dict:
        return {
            "sources": self.sources._store(),
            "rule": self.transfers.rule(),
            "sources_exists": self.sources.path.exists(),
            "rule_exists": self.transfers.rule_path.exists(),
        }

    def state(self) -> str:
        return fingerprint(self.capture())

    @staticmethod
    def validate_backup(value: dict) -> None:
        if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_JSON:
            raise ValueError("管家本机恢复资料超过 8 MiB 限制，请精简来源记录后重试。")
        exact_keys(value, {"sources", "rule", "sources_exists", "rule_exists"})
        if any(
            type(value[key]) is not bool for key in ("sources_exists", "rule_exists")
        ):
            raise ValueError("管家恢复资料的文件存在标记无效。")
        exact_keys(value["sources"], {"schema_version", "plugins", "archived"})
        if (
            type(value["sources"]["schema_version"]) is not int
            or value["sources"]["schema_version"] != 1
            or not isinstance(value["sources"]["plugins"], dict)
            or not isinstance(value["sources"]["archived"], list)
        ):
            raise ValueError("管家恢复资料的来源记录无效。")
        validate_manager_data(
            {
                "format": FORMAT,
                "schema_version": 1,
                "name": "管家来源与规则",
                "sources": [],
                "rule": value["rule"],
            }
        )

    async def restore(self, before: dict) -> None:
        self.validate_backup(before)
        async with self.sources.record_lock:
            for path, key in (
                (self.sources.path, "sources"),
                (self.transfers.rule_path, "rule"),
            ):
                if before[key + "_exists"]:
                    await asyncio.to_thread(write_json, path, before[key])
                else:
                    await asyncio.to_thread(path.unlink, missing_ok=True)

    async def plan(self, value: dict, replacements: dict | None = None) -> dict:
        records = await self.sources.inventory()
        plugins = []
        for index, incoming in enumerate(value["sources"]):
            item = {
                "id": str(index),
                "name": incoming["name"],
                "display_name": incoming["name"],
                "directory": "",
                "status": "blocked",
                "problem": "",
                "count": 0,
            }
            matches = [
                record
                for record in records
                if record["local"]["name"] == incoming["name"]
                and authors_match(
                    record["local"]["author"], incoming["author"], market=False
                )
            ]
            try:
                if len(matches) != 1:
                    raise ValueError("未找到唯一的同身份已安装插件。")
                record = matches[0]
                self.sources._editable(record)
                if record["metadata_changed"]:
                    raise ValueError("请先更新插件信息，再重新预检。")
                missing = not (record["current_source"] or record["pending_source"])
                item.update(
                    directory=record["local"]["directory"],
                    display_name=record["local"]["display_name"] or incoming["name"],
                    status="ready" if missing else "skip",
                    count=1 if missing else 0,
                )
            except ValueError as exc:
                item["problem"] = str(exc)
            plugins.append(item)
        rule = {"ready": True, "problem": "", "selections": []}
        mapped = set()
        replacements = replacements or {}
        try:
            for selection in value["rule"]["selections"]:
                candidates = {
                    record["local"]["directory"]: record["local"]["name"]
                    for record in records
                }
                candidates.update(
                    {
                        key: source["metadata"]["name"]
                        for key, source in replacements.items()
                    }
                )
                matches = [
                    key for key, name in candidates.items() if name == selection["name"]
                ]
                if len(matches) != 1:
                    raise ValueError(f"{selection['name']}：未找到唯一的已安装插件。")
                source = replacements.get(matches[0]) or await asyncio.to_thread(
                    self.transfers._source, matches[0]
                )
                if source["metadata"]["plugin_id"] != selection["plugin_id"]:
                    raise ValueError(f"{selection['name']}：插件身份不一致。")
                current = {
                    "directory": source["directory"],
                    "config": selection["config"],
                    "data": selection["data"],
                }
                try:
                    self.transfers._check_components(source, current)
                except ValueError as exc:
                    raise ValueError(f"{selection['name']}：{exc}") from exc
                if current["directory"] in mapped:
                    raise ValueError("规则中的目标插件重复。")
                mapped.add(current["directory"])
                rule["selections"].append(
                    {
                        **current,
                        "name": source["metadata"]["name"],
                        "plugin_id": source["metadata"]["plugin_id"],
                    }
                )
        except (ValueError, OSError) as exc:
            rule.update(
                ready=False,
                problem=str(exc)
                if isinstance(exc, ValueError)
                else "读取规则目标失败，请检查插件文件权限。",
            )
        return {
            "plugins": plugins,
            "rule": rule,
        }

    async def apply(self, value: dict, owner: str) -> list[str]:
        value = validate_manager_data(value)
        plan = await self.plan(value)
        if not plan["rule"]["ready"]:
            raise ValueError("管家导出规则无法恢复：" + plan["rule"]["problem"])
        warnings = [
            f"{item['name']}：未恢复来源关联，{item['problem']}"
            for item in plan["plugins"]
            if item["problem"]
        ]
        async with self.sources.record_lock:
            store = self.sources._store()
            for item, incoming in zip(plan["plugins"], value["sources"]):
                if item["status"] != "ready":
                    continue
                record = store["plugins"][item["directory"]]
                record["pending_source"] = copy.deepcopy(incoming["source"])
                self.sources._event(record, "manager_data_imported", owner)
            await asyncio.to_thread(write_json, self.sources.path, store)
            await asyncio.to_thread(
                write_json,
                self.transfers.rule_path,
                {
                    "rule_version": 1,
                    "updated_at": time.time(),
                    "selections": plan["rule"]["selections"],
                },
            )
        return warnings
