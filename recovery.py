"""Preview and restore retained, instance-local pre-import backups."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
import zipfile
import zlib

from astrbot import __version__ as ASTRBOT_VERSION

from .bytecode import clear_plugin_bytecode
from .redaction import redact
from .transfer_bundle import (
    FORMAT,
    MAX_ARCHIVE,
    MAX_CONTENT,
    MAX_FILES,
    check_config,
    check_environment,
    component,
    decode_value,
    digest,
    exact_keys,
    is_manager_plugin,
    load_json,
    read_metadata,
    safe_path,
    tree_directories,
    tree_files,
)

COMPONENTS = ("code", "config", "data")
FINISHED = {
    "completed",
    "completed_with_warning",
    "failed",
    "interrupted",
    "rollback_failed",
}


class RecoveryService:
    def __init__(self, transfers, preview_seconds: int) -> None:
        self.transfers = transfers
        self.preview_seconds = preview_seconds

    def _source(self, owner: str, identifier: str) -> dict:
        job = self.transfers.get_job(identifier, owner)
        if job["kind"] != "import" or job["status"] not in FINISHED:
            raise ValueError("只能还原已结束的插件包导入任务。")
        if not job.get("backup_ready") or self.transfers._expired(job):
            raise ValueError("此任务没有可用的恢复资料。")
        return job

    def _load(self, source: dict) -> tuple[list[dict], str]:
        """Validate the existing journal and its files against the retained ZIP."""
        try:
            return self._load_files(source)
        except (
            KeyError,
            TypeError,
            zipfile.BadZipFile,
            EOFError,
            RuntimeError,
            zlib.error,
        ) as exc:
            raise ValueError("恢复资料损坏或记录不完整，无法通过页面还原。") from exc

    def _load_files(self, source: dict) -> tuple[list[dict], str]:
        root = self.transfers.job_directory(source["id"])
        journal_path = safe_path(root, "rollback.json")
        archive_path = safe_path(root, "rollback.zip")
        if not journal_path.is_file() or not archive_path.is_file():
            raise ValueError("恢复资料缺失，请检查该任务的文件。")
        if journal_path.stat().st_nlink != 1 or archive_path.stat().st_nlink != 1:
            raise ValueError("恢复资料包含硬链接，拒绝还原。")
        journal = load_json(journal_path)
        exact_keys(
            journal, {"format", "format_version", "plugins", "job_id", "created_at"}
        )
        if (
            journal["format"] != FORMAT + "-rollback"
            or type(journal["format_version"]) is not int
            or journal["format_version"] != 1
            or journal["job_id"] != source["id"]
            or not isinstance(journal["plugins"], list)
            or not 1 <= len(journal["plugins"]) <= 256
        ):
            raise ValueError("恢复资料格式或任务标识无效。")
        incoming = {item["directory"]: item for item in source["manifest"]["plugins"]}
        selections = {item["directory"]: item for item in source["restore"]}
        seen = set()
        allowed = set()
        plugins = []
        for record in journal["plugins"]:
            if not isinstance(record, dict):
                raise ValueError("恢复资料中的插件记录格式无效。")
            exact_keys(
                record,
                {
                    "directory",
                    "name",
                    "plugin_id",
                    "module_path",
                    "was_enabled",
                    "was_inactive",
                    "components",
                }
                | ({"source_state"} if "source_state" in record else set()),
            )
            directory = component(record["directory"])
            component(record["name"])
            if (
                directory in seen
                or directory not in incoming
                or directory not in selections
            ):
                raise ValueError("恢复资料包含重复或不属于该导入任务的插件。")
            seen.add(directory)
            plugin = incoming[directory]
            if (
                record["name"] != plugin["name"]
                or record["plugin_id"] != plugin["plugin_id"]
            ):
                raise ValueError("恢复资料的插件身份与导入任务不一致。")
            module = record["module_path"]
            if not isinstance(module, str) or (
                module != self.transfers.operations.owner_module
                if is_manager_plugin(plugin)
                else not module.startswith(f"data.plugins.{directory}.")
            ):
                raise ValueError("恢复资料中的插件运行标识无效。")
            for part in module.split("."):
                component(part, module=True)
            if any(
                type(record[key]) is not bool for key in ("was_enabled", "was_inactive")
            ):
                raise ValueError("恢复资料中的启停状态无效。")
            expected = {key for key in COMPONENTS if selections[directory][key]}
            if (
                not isinstance(record["components"], dict)
                or set(record["components"]) != expected
                or any(
                    type(value) is not bool for value in record["components"].values()
                )
            ):
                raise ValueError("恢复资料中的组件范围不完整。")
            before = safe_path(root, "before/" + directory)
            if not before.is_dir():
                raise ValueError("恢复资料缺少插件备份目录。")
            allowed.add("before/" + directory)
            manager_data = is_manager_plugin(plugin)
            if manager_data:
                if record["components"] != {"data": True} or "source_state" in record:
                    raise ValueError("管家恢复资料仅允许来源关联和导出规则。")
                self.transfers.manager_data.validate_backup(
                    load_json(before / "manager-data.json")
                )
                allowed.add("before/" + directory + "/manager-data.json")
            for key, existed in record["components"].items():
                if manager_data:
                    continue
                path = safe_path(before, key)
                if existed:
                    if (key == "config" and not path.is_file()) or (
                        key != "config" and not path.is_dir()
                    ):
                        raise ValueError(
                            "恢复资料缺少原有组件：" + directory + "/" + key
                        )
                    allowed.add("before/" + directory + "/" + key)
                elif path.exists():
                    raise ValueError("恢复资料与组件缺失标记不一致。")
            if record["components"].get("code"):
                metadata = read_metadata(before / "code")
                if (
                    metadata["name"] != record["name"]
                    or metadata["plugin_id"] != record["plugin_id"]
                ):
                    raise ValueError("备份代码的插件身份不一致。")
                plugin = {**plugin, **metadata, "directory": directory}
            if "code" in record["components"]:
                snapshot = record.get("source_state")
                if not isinstance(snapshot, dict):
                    raise ValueError("恢复资料缺少来源快照，无法通过页面完整还原代码。")
                exact_keys(snapshot, {"directory", "name", "record", "core"})
                if (
                    snapshot["directory"] != directory
                    or snapshot["name"] != record["name"]
                    or not isinstance(snapshot["core"], dict)
                    or not set(snapshot["core"]) <= {directory, record["name"]}
                    or (
                        snapshot["record"] is not None
                        and not isinstance(snapshot["record"], dict)
                    )
                ):
                    raise ValueError("恢复资料中的来源快照无效。")
            if "data" in record["components"] and not manager_data:
                values = load_json(safe_path(before, "kv.json"))
                if not isinstance(values, dict):
                    raise ValueError("恢复资料中的键值数据格式无效。")
                for value in values.values():
                    decode_value(value)
                allowed.add("before/" + directory + "/kv.json")
            plugins.append({"plugin": plugin, "record": record})
        before_root = safe_path(root, "before")
        files = tree_files(before_root)
        folders = tree_directories(before_root)
        for path in [*files, *folders]:
            relative = "before/" + path.relative_to(before_root).as_posix()
            if not any(
                relative == prefix
                or (
                    prefix not in {"before/" + directory for directory in seen}
                    and relative.startswith(prefix + "/")
                )
                for prefix in allowed
            ):
                raise ValueError("恢复资料包含范围外的文件或目录。")
        hashes = {"rollback.json": digest(journal_path)}
        hashes.update(
            {
                "before/" + path.relative_to(before_root).as_posix(): digest(path)
                for path in files
            }
        )
        directories = {
            "before/" + path.relative_to(before_root).as_posix() for path in folders
        }
        if archive_path.stat().st_size > MAX_ARCHIVE:
            raise ValueError("恢复资料包超过大小限制。")
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if (
                len(entries) > MAX_FILES
                or sum(entry.file_size for entry in entries) > MAX_CONTENT
                or len({entry.filename for entry in entries}) != len(entries)
            ):
                raise ValueError("恢复资料包的大小、数量或路径无效。")
            if {entry.filename for entry in entries if not entry.is_dir()} != set(
                hashes
            ) or {
                entry.filename.rstrip("/") for entry in entries if entry.is_dir()
            } != directories:
                raise ValueError("恢复资料包与备份目录不一致。")
            for entry in entries:
                safe_path(root, entry.filename.rstrip("/"))
                if entry.is_dir():
                    continue
                checksum = hashlib.sha256()
                with archive.open(entry) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        checksum.update(chunk)
                if checksum.hexdigest() != hashes[entry.filename]:
                    raise ValueError("恢复资料校验失败，文件可能已损坏或被修改。")
        signature = hashlib.sha256(
            json.dumps([hashes, sorted(directories)], sort_keys=True).encode()
        ).hexdigest()
        return plugins, signature

    async def _state(self, plugin: dict, *, include_sources: bool = True) -> str:
        source = await asyncio.to_thread(self.transfers._existing, plugin)
        if is_manager_plugin(plugin):
            self.transfers._protect_import(plugin)
            await self.transfers.operations.sources.inventory()
            return self.transfers.manager_data.state()
        paths = self.transfers._paths(plugin)

        def files():
            result = {}
            for key, (root, relative) in paths.items():
                path = safe_path(root, relative)
                if path.is_dir():
                    result[key] = {
                        "files": {
                            item.relative_to(path).as_posix(): digest(item)
                            for item in tree_files(path)
                        },
                        "directories": [
                            item.relative_to(path).as_posix()
                            for item in tree_directories(path)
                        ],
                    }
                else:
                    if path.exists() and (
                        not path.is_file() or path.stat().st_nlink != 1
                    ):
                        raise ValueError("目标组件包含特殊文件或硬链接。")
                    result[key] = digest(path) if path.exists() else None
            return result

        value = await asyncio.to_thread(files)
        target = source["target"] if source else None
        if not source and any(
            item.root_dir_name == plugin["directory"]
            for item in self.transfers.operations.context.get_all_stars()
        ):
            raise ValueError("插件代码缺失但仍有运行记录，请先重启 AstrBot 核对状态。")
        value["runtime"] = repr(target.state) if target else "pending"
        value["inactive"] = (
            f"data.plugins.{plugin['directory']}.main"
            in await self.transfers.preferences.global_get("inactivated_plugins", [])
        )
        value["kv"] = await self.transfers._read_kv(plugin["plugin_id"])
        if include_sources:
            value["sources"] = await self.transfers.operations.sources.source_snapshot(
                plugin["directory"], plugin["name"]
            )
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def _check(
        self, source_job: dict, plugin: dict, record: dict, selection: dict
    ) -> None:
        self.transfers._protect_import(plugin)
        source = self.transfers._existing(plugin)
        if source and source["module_path"] != record["module_path"]:
            raise ValueError("目标插件运行标识已变化，拒绝还原。")
        if is_manager_plugin(plugin):
            if selection["code"] or selection["config"] or not selection["data"]:
                raise ValueError("管家仅允许还原来源关联和导出规则。")
            return
        before = safe_path(
            self.transfers.job_directory(source_job["id"]),
            "before/" + plugin["directory"],
        )
        deleting_code = selection["code"] and not record["components"]["code"]
        code = (
            before / "code"
            if selection["code"] and record["components"]["code"]
            else source["code"]
            if source
            else None
        )
        if not deleting_code:
            if code is None:
                raise ValueError("当前插件代码不存在，必须同时还原代码。")
            check_environment(code, ASTRBOT_VERSION)
            if selection["config"] and record["components"]["config"]:
                check_config(
                    load_json(before / "config"),
                    load_json(safe_path(code, "_conf_schema.json")),
                )
        if selection["data"]:
            self.transfers._check_components(
                source
                or {
                    "metadata": plugin,
                    "directory": plugin["directory"],
                    "target": None,
                },
                {"config": False, "data": True},
            )

    async def preview(self, owner: str, identifier: str) -> dict:
        transfers = self.transfers
        transfers.operations.ensure_writable()
        transfers._ensure_idle()
        async with transfers.operation_lock, transfers.manager._pm_lock:
            transfers._ensure_idle()
            source = self._source(owner, identifier)
            entries, signature = await asyncio.to_thread(self._load, source)
            plans, checks, content_checks = [], {}, {}
            has_manager = any(is_manager_plugin(entry["plugin"]) for entry in entries)
            for entry in entries:
                plugin, record = entry["plugin"], entry["record"]
                selection = {key: key in record["components"] for key in COMPONENTS}
                problem = ""
                try:
                    await asyncio.to_thread(
                        self._check, source, plugin, record, selection
                    )
                    checks[plugin["directory"]] = await self._state(plugin)
                    if has_manager and not is_manager_plugin(plugin):
                        content_checks[plugin["directory"]] = await self._state(
                            plugin, include_sources=False
                        )
                except ValueError as exc:
                    problem = redact(str(exc))
                plans.append(
                    {
                        "directory": plugin["directory"],
                        "name": plugin["name"],
                        "display_name": plugin.get("display_name", plugin["name"]),
                        "version": plugin.get("version", ""),
                        "components": {
                            key: "replace" if existed else "remove"
                            for key, existed in record["components"].items()
                        },
                        "problem": problem,
                        "manager_data": is_manager_plugin(plugin),
                    }
                )
            job = transfers._new_job(owner, "rollback")
            job.update(
                status="preview",
                source_id=identifier,
                recovery_entries=entries,
                recovery_signature=signature,
                checks=checks,
                content_checks=content_checks,
                plan=plans,
            )
            transfers._phase(job, "还原预检完成，等待选择与确认")
            return self.plan(owner, job["id"])

    def plan(self, owner: str, identifier: str) -> dict:
        job = self.transfers.get_job(identifier, owner)
        if (
            job["kind"] != "rollback"
            or job["status"] != "preview"
            or time.time() > job["created_at"] + self.preview_seconds
        ):
            raise ValueError("还原预检已过期或已执行，请从原导入任务重新预检。")
        return {
            "job": self.transfers.public_job(job),
            "plugins": copy.deepcopy(job["plan"]),
            "expires_at": job["created_at"] + self.preview_seconds,
        }

    async def start(
        self, owner: str, identifier: str, selections, acknowledged: bool
    ) -> dict:
        transfers = self.transfers
        transfers.operations.ensure_writable()
        transfers._ensure_idle()
        async with transfers.operation_lock, transfers.manager._pm_lock:
            transfers._ensure_idle()
            self.plan(owner, identifier)
            if acknowledged is not True:
                raise ValueError("请确认还原范围及覆盖当前修改的风险。")
            job = transfers.get_job(identifier, owner)
            source = self._source(owner, job["source_id"])
            _, signature = await asyncio.to_thread(self._load, source)
            if signature != job["recovery_signature"]:
                raise ValueError("恢复资料在预检后变化，请重新预检。")
            if not isinstance(selections, list) or not selections:
                raise ValueError("请选择需要还原的插件和组件。")
            entries = {
                entry["plugin"]["directory"]: entry for entry in job["recovery_entries"]
            }
            seen = set()
            for selection in selections:
                exact_keys(selection, {"directory", *COMPONENTS})
                directory = selection["directory"]
                if (
                    not isinstance(directory, str)
                    or directory not in entries
                    or directory in seen
                    or any(type(selection[key]) is not bool for key in COMPONENTS)
                    or not any(selection[key] for key in COMPONENTS)
                ):
                    raise ValueError("还原选择无效、重复或没有组件。")
                seen.add(directory)
                plugin, record = (
                    entries[directory]["plugin"],
                    entries[directory]["record"],
                )
                if any(
                    selection[key] and key not in record["components"]
                    for key in COMPONENTS
                ):
                    raise ValueError("选择了恢复资料中未备份的组件。")
                await asyncio.to_thread(self._check, source, plugin, record, selection)
                if job["checks"].get(directory) != await self._state(plugin):
                    raise ValueError(
                        "目标文件、数据、来源或启停状态在预检后变化，请重新预检。"
                    )
            job["restore"] = sorted(
                copy.deepcopy(selections),
                key=lambda item: (
                    not is_manager_plugin(entries[item["directory"]]["plugin"])
                ),
            )
            return transfers._start(job, self._restore)

    async def _restore(self, job: dict) -> None:
        transfers = self.transfers
        async with transfers.manager._pm_lock:
            transfers.in_critical = True
            try:
                if time.time() > job["created_at"] + self.preview_seconds:
                    raise ValueError("还原预检在排队期间过期，请重新预检。")
                source_job = self._source(job["owner"], job["source_id"])
                _, signature = await asyncio.to_thread(self._load, source_job)
                if signature != job["recovery_signature"]:
                    raise ValueError("恢复资料在排队期间变化，请重新预检。")
                entries = {
                    entry["plugin"]["directory"]: entry
                    for entry in job["recovery_entries"]
                }
                space = 0
                for selection in job["restore"]:
                    entry = entries[selection["directory"]]
                    plugin = entry["plugin"]
                    await asyncio.to_thread(
                        self._check, source_job, plugin, entry["record"], selection
                    )
                    if job["checks"][plugin["directory"]] != await self._state(plugin):
                        raise ValueError("目标状态在排队期间变化，请重新预检。")
                    if is_manager_plugin(plugin):
                        space += 3 * len(
                            json.dumps(
                                transfers.manager_data.capture(), ensure_ascii=False
                            ).encode("utf-8")
                        )
                        space += (
                            safe_path(
                                transfers.job_directory(source_job["id"]),
                                "before/" + plugin["directory"] + "/manager-data.json",
                            )
                            .stat()
                            .st_size
                        )
                        continue
                    paths = transfers._paths(plugin)
                    current = {
                        "directory": plugin["directory"],
                        "metadata": plugin,
                        "code": safe_path(*paths["code"]),
                        "config_path": safe_path(*paths["config"]),
                        "data_path": safe_path(*paths["data"]),
                        "kv_plugin_id": plugin["plugin_id"],
                    }
                    space += 3 * await transfers._source_size(
                        current, selection, export=False
                    )
                    before = safe_path(
                        transfers.job_directory(source_job["id"]),
                        "before/" + plugin["directory"],
                    )
                    for key in COMPONENTS:
                        if selection[key]:
                            space += await asyncio.to_thread(
                                transfers._path_size, before / key
                            )
                    if selection["data"]:
                        space += (before / "kv.json").stat().st_size
                await transfers._require_space(space)
                manager_restored = False
                for selection in job["restore"]:
                    entry = entries[selection["directory"]]
                    plugin, original = entry["plugin"], entry["record"]
                    source = await asyncio.to_thread(transfers._existing, plugin)
                    backup, paused = None, False
                    previous_results = len(job["results"])
                    transfers._phase(job, "备份当前内容并还原：" + plugin["directory"])
                    try:
                        # Manager rollback changes shared source records intentionally.
                        # Keep checking files, KV and runtime for the following items.
                        expected = (
                            job["content_checks"] if manager_restored else job["checks"]
                        )
                        if expected[plugin["directory"]] != await self._state(
                            plugin, include_sources=not manager_restored
                        ):
                            raise ValueError("目标内容在执行前变化，请重新预检。")
                        if source:
                            source["was_enabled"] = bool(
                                source["target"]
                                and source["target"].plugin
                                and source["target"].plugin.activated
                            )
                            source["was_inactive"] = source[
                                "module_path"
                            ] in await transfers.preferences.global_get(
                                "inactivated_plugins", []
                            )
                            if not is_manager_plugin(plugin):
                                paused = await transfers._pause(source)
                        backup = await transfers._backup(job, plugin, selection, source)
                        selected_record = {
                            **original,
                            "components": {
                                key: existed
                                for key, existed in original["components"].items()
                                if selection[key]
                            },
                            "was_inactive": True,
                        }
                        if not selection["code"]:
                            selected_record.pop("source_state", None)
                        await transfers._restore_backup(
                            source_job, plugin, selected_record
                        )
                        if is_manager_plugin(plugin):
                            manager_restored = True
                            job["results"].append(
                                {
                                    "directory": plugin["directory"],
                                    "status": "restored",
                                    "message": "已还原来源关联和导出规则；管家保持运行。",
                                }
                            )
                            transfers._save(job)
                            continue
                        if safe_path(transfers.code_root, plugin["directory"]).is_dir():
                            await asyncio.to_thread(
                                clear_plugin_bytecode,
                                str(transfers.code_root),
                                plugin["directory"],
                            )
                        if source and source["target"] and source["target"].plugin:
                            await transfers.manager._unbind_plugin(
                                plugin["name"], source["module_path"]
                            )
                        transfers.manager.failed_plugin_dict.pop(
                            plugin["directory"], None
                        )
                        job["results"].append(
                            {
                                "directory": plugin["directory"],
                                "status": "restored",
                                "message": "已移除新增代码并还原所选组件。"
                                if selection["code"]
                                and not original["components"]["code"]
                                else "已还原所选内容并保持停用；重启 AstrBot 后在 WebUI 手动启用。",
                            }
                        )
                        job["reload_required"] = True
                        transfers._save(job)
                    except Exception:
                        del job["results"][previous_results:]
                        try:
                            if backup:
                                await transfers._restore_backup(job, plugin, backup)
                            if paused or (source and source.get("needs_resume")):
                                await transfers._resume(source)
                            job["results"].append(
                                {
                                    "directory": plugin["directory"],
                                    "status": "failed",
                                    "message": "还原失败，已恢复还原前的备份；后续插件未执行。",
                                }
                            )
                        except Exception:
                            job["status"] = "rollback_failed"
                            job["results"].append(
                                {
                                    "directory": plugin["directory"],
                                    "status": "rollback_failed",
                                    "message": "还原失败且自动恢复失败，请停止 AstrBot 并检查本任务的恢复资料。",
                                }
                            )
                        transfers._save(job)
                        raise
            finally:
                transfers.in_critical = False
        if job.get("reload_required"):
            try:
                await transfers.operations.web.sync_skills_after_plugin_change()
            except Exception:
                job["warnings"].append(
                    "插件已还原，但技能同步失败，请重启 AstrBot 后检查。"
                )
