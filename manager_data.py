"""Migrate associations and export rules without inheriting executable state."""

from __future__ import annotations

import asyncio
import copy
import json
import math
import time
import uuid

from .export_names import export_name
from .identity import authors_match
from .manifests import _file_work
from .source_bundle import portable_binding, validate_bindings
from .sources import fingerprint
from .transfer_bundle import MAX_JSON, component, exact_keys, safe_path, write_json
from .transfers import PREVIEW_TTL, TransferService

FORMAT = "astrbot-plugin-manager-data"


def validate_manager_data(value) -> dict:
    if (
        not isinstance(value, dict)
        or value.get("format") != FORMAT
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
    ):
        raise ValueError("仅支持管家数据 JSON v1，请选择本功能导出的文件。")
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
    TransferService._validate_selections(rule["selections"], identities=True)
    for selection in rule["selections"]:
        component(selection["name"], module=True)
        if (
            not isinstance(selection["plugin_id"], str)
            or not selection["plugin_id"]
            or len(selection["plugin_id"]) > 1024
        ):
            raise ValueError("导出规则的插件数据标识无效。")
    return copy.deepcopy(value)


class ManagerDataService:
    def __init__(self, sources, transfers) -> None:
        self.sources = sources
        self.transfers = transfers
        self.operations = sources.operations
        self.previews = {}

    async def export(self, owner: str, name: str = "") -> dict:
        self.operations.ensure_writable()
        self.transfers._ensure_idle()
        async with self.transfers.operation_lock, self.operations.manager._pm_lock:
            self.transfers._ensure_idle()
            records = await self.sources.inventory()
            entries, warnings = [], []
            for record in records:
                if not record["bindings"]:
                    continue
                local = record["local"]
                if local["reserved"] or local["problem"] or record["identity_changed"]:
                    warnings.append(
                        f"{local['directory']}：插件身份不可确认，未导出来源关联。"
                    )
                    continue
                snapshot, notices = self.sources.portable_sources(record)
                warnings.extend(notices)
                if snapshot["bindings"]:
                    entries.append(
                        {
                            "directory": local["directory"],
                            **snapshot["identity"],
                            "bindings": snapshot["bindings"],
                            "active_binding_id": snapshot["active_binding_id"],
                        }
                    )
            value = validate_manager_data(
                {
                    "format": FORMAT,
                    "schema_version": 1,
                    "name": export_name(name) or "astrbot-manager-data",
                    "sources": entries,
                    "rule": self.transfers.rule(),
                }
            )
            job = self.transfers._new_job(owner, "manager_data_export")
            job["name"] = value["name"]
            try:
                await _file_work(
                    write_json,
                    safe_path(
                        self.transfers.job_directory(job["id"]), "manager-data.json"
                    ),
                    value,
                )
            except BaseException as exc:
                job.update(
                    status="interrupted"
                    if isinstance(exc, asyncio.CancelledError)
                    else "failed",
                    phase="导出未完成",
                    error="管家数据导出被中断。"
                    if isinstance(exc, asyncio.CancelledError)
                    else "管家数据导出失败。",
                )
                self.transfers._save(job)
                raise
            job.update(
                status="completed_with_warning" if warnings else "completed",
                phase="管家数据已导出",
                warnings=warnings,
            )
            self.transfers._save(job)
            return self.transfers.public_job(job)

    @staticmethod
    def _new_bindings(record: dict, incoming: dict) -> list[dict]:
        known = set()
        for binding in record["bindings"]:
            try:
                portable = portable_binding(binding)
                if portable:
                    known.add(portable["id"])
            except ValueError:
                pass
        return [
            binding for binding in incoming["bindings"] if binding["id"] not in known
        ]

    async def _plan(self, value: dict) -> dict:
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
                added = self._new_bindings(record, incoming)
                if len(record["bindings"]) + len(added) > 256:
                    raise ValueError("合并后的来源关联超过 256 条限制。")
                item.update(
                    directory=record["local"]["directory"],
                    display_name=record["local"]["display_name"] or incoming["name"],
                    status="ready" if added else "skip",
                    count=len(added),
                )
            except ValueError as exc:
                item["problem"] = str(exc)
            plugins.append(item)
        rule = {"ready": True, "problem": "", "selections": []}
        mapped = set()
        try:
            for selection in value["rule"]["selections"]:
                matches = [
                    record
                    for record in records
                    if record["local"]["name"] == selection["name"]
                ]
                if len(matches) != 1:
                    raise ValueError(f"{selection['name']}：未找到唯一的已安装插件。")
                source = await asyncio.to_thread(
                    self.transfers._source, matches[0]["local"]["directory"]
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
            "check": fingerprint(
                {
                    "store": self.sources._store(),
                    "rule": self.transfers.rule(),
                    "locals": [record["local"] for record in records],
                    "plan": [plugins, rule],
                }
            ),
        }

    async def preview(self, owner: str, value: dict) -> dict:
        value = validate_manager_data(value)
        self.operations.ensure_writable()
        self.transfers._ensure_idle()
        async with self.transfers.operation_lock, self.operations.manager._pm_lock:
            self.transfers._ensure_idle()
            plan = await self._plan(value)
        self.previews = {
            key: item
            for key, item in self.previews.items()
            if item["expires_at"] > time.time()
        }
        if len(self.previews) >= 32:
            raise ValueError("管家数据预检数量已达上限，请稍后重试。")
        identifier = uuid.uuid4().hex
        expires = time.time() + PREVIEW_TTL
        self.previews[identifier] = {
            "owner": owner,
            "value": value,
            "plan": plan,
            "expires_at": expires,
        }
        return {
            "id": identifier,
            "name": value["name"],
            "expires_at": expires,
            "plugins": plan["plugins"],
            "rule": plan["rule"],
        }

    def start(
        self,
        owner: str,
        identifier: str,
        sources: list[str],
        rule: bool,
        acknowledged: bool,
    ) -> dict:
        self.operations.ensure_writable()
        self.transfers._ensure_idle()
        preview = self.previews.get(identifier)
        if (
            not preview
            or preview["owner"] != owner
            or preview["expires_at"] <= time.time()
        ):
            raise ValueError("管家数据预检不存在或已过期，请重新上传。")
        allowed = {
            item["id"]
            for item in preview["plan"]["plugins"]
            if item["status"] == "ready"
        }
        if (
            acknowledged is not True
            or type(rule) is not bool
            or not isinstance(sources, list)
            or any(not isinstance(item, str) for item in sources)
            or len(set(sources)) != len(sources)
            or not set(sources) <= allowed
            or (not sources and not rule)
            or (rule and not preview["plan"]["rule"]["ready"])
        ):
            raise ValueError("请选择通过预检的来源关联或导出规则，并确认恢复范围。")
        job = self.transfers._new_job(owner, "manager_data_import")
        job.update(
            name=preview["value"]["name"],
            manager_data=copy.deepcopy(preview["value"]),
            manager_data_check=preview["plan"]["check"],
            sources=list(sources),
            restore_rule=rule,
            preview_expires_at=preview["expires_at"],
        )
        result = self.transfers._start(job, self._import)
        self.previews.pop(identifier)
        return result

    async def _import(self, job: dict) -> None:
        async with self.operations.manager._pm_lock:
            if time.time() > job["preview_expires_at"]:
                raise ValueError("管家数据预检已过期，请重新上传。")
            plan = await self._plan(job["manager_data"])
            if plan["check"] != job["manager_data_check"]:
                raise ValueError("插件、来源关联或导出规则已变化，请重新上传预检。")
            async with self.sources.record_lock:
                before = {
                    "sources": self.sources._store(),
                    "rule": self.transfers.rule(),
                    "sources_exists": self.sources.path.exists(),
                    "rule_exists": self.transfers.rule_path.exists(),
                }
                store = copy.deepcopy(before["sources"])
                for identifier in job["sources"]:
                    incoming = job["manager_data"]["sources"][int(identifier)]
                    item = plan["plugins"][int(identifier)]
                    record = store["plugins"][item["directory"]]
                    added = self._new_bindings(record, incoming)
                    record["bindings"].extend(
                        {
                            **copy.deepcopy(binding),
                            "verification": "imported",
                            "checked_at": None,
                            "imported_active": binding["id"]
                            == incoming["active_binding_id"],
                        }
                        for binding in added
                    )
                    self.sources._event(record, "manager_data_imported", job["owner"])
                rule = {
                    "rule_version": 1,
                    "updated_at": time.time(),
                    "selections": plan["rule"]["selections"],
                }
                await _file_work(
                    write_json,
                    safe_path(
                        self.transfers.job_directory(job["id"]),
                        "before-manager-data.json",
                    ),
                    before,
                )
                self.transfers.in_critical = True
                try:
                    try:
                        if job["sources"]:
                            await _file_work(write_json, self.sources.path, store)
                        if job["restore_rule"]:
                            await _file_work(write_json, self.transfers.rule_path, rule)
                    except BaseException:
                        try:
                            for path, key in (
                                (self.sources.path, "sources"),
                                (self.transfers.rule_path, "rule"),
                            ):
                                if not (
                                    job["sources"]
                                    if key == "sources"
                                    else job["restore_rule"]
                                ):
                                    continue
                                if before[key + "_exists"]:
                                    await _file_work(write_json, path, before[key])
                                else:
                                    await _file_work(path.unlink, missing_ok=True)
                        except Exception as exc:
                            job["status"] = "rollback_failed"
                            raise ValueError(
                                "管家数据恢复失败，原数据未能完整还原；请检查任务中的 before-manager-data.json。"
                            ) from exc
                        raise
                finally:
                    self.transfers.in_critical = False
                job["results"] = [
                    {
                        "directory": plan["plugins"][int(identifier)]["directory"],
                        "message": "来源关联已合并，等待校验。",
                    }
                    for identifier in job["sources"]
                ]
                if job["restore_rule"]:
                    job["results"].append(
                        {
                            "directory": "导出规则",
                            "message": f"已恢复 {len(rule['selections'])} 个插件的规则。",
                        }
                    )
