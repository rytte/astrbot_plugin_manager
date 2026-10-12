"""Persisted rules and serialized, recoverable plugin transfer jobs."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import platform
import shutil
import time
import uuid
import zipfile
from pathlib import Path

from astrbot import __version__ as ASTRBOT_VERSION

from . import service as operation_module
from .bytecode import clear_plugin_bytecode
from .export_names import export_filename, export_name
from .manager_data_bundle import validate_manager_data, validate_selections
from .recovery import RecoveryService
from .redaction import redact
from .transfer_bundle import (
    FORMAT,
    FORMAT_VERSION,
    MAX_ARCHIVE,
    MAX_CONTENT,
    MAX_FILE,
    MAX_FILES,
    check_bundle_format,
    check_config,
    check_environment,
    check_source_snapshot,
    component,
    copy_tree,
    decode_value,
    digest,
    encode_value,
    exact_keys,
    is_manager_plugin,
    load_json,
    pack,
    read_metadata,
    remove_path,
    safe_path,
    stamp,
    tree_directories,
    tree_files,
    unpack,
    write_json,
)

logger = logging.getLogger("astrbot")
PREVIEW_TTL = 3600
SPACE_MARGIN = 64 * 1024 * 1024


class TransferService:
    """Own private files and one transfer task sharing the chat operation lock."""

    def __init__(self, operations, operation_lock: asyncio.Lock) -> None:
        self.operations = operations
        self.manager = operations.manager
        self.preferences = operation_module.sp
        self.operation_lock = operation_lock
        self.code_root = Path(self.manager.plugin_store_path).absolute()
        self.config_root = Path(self.manager.plugin_config_path).absolute()
        self.data_root = safe_path(self.code_root.parent, "plugin_data")
        self.root = safe_path(self.data_root, "astrbot_plugin_manager/transfers")
        self.jobs_root = safe_path(self.root, "jobs")
        self.rule_path = safe_path(self.root, "rule.json")
        self.jobs = {}
        self.deleting_jobs = set()
        self.task = None
        self.active_job = None
        self.in_critical = False
        self.closing = False
        self.recovery = RecoveryService(self, PREVIEW_TTL)

    async def initialize(self) -> None:
        """Load retained jobs and mark unfinished work as interrupted."""
        await asyncio.to_thread(self._initialize_files)

    def _initialize_files(self) -> None:
        for root in (
            self.code_root,
            self.config_root,
            self.data_root,
            self.root,
            self.jobs_root,
        ):
            if root.resolve() != root:
                raise ValueError("插件迁移存储路径含链接，拒绝初始化。")
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        for directory in self.jobs_root.iterdir():
            if len(directory.name) != 32 or any(
                char not in "0123456789abcdef" for char in directory.name
            ):
                raise ValueError("任务存储包含未知目录，请检查后再启动。")
            path = safe_path(self.jobs_root, directory.name + "/job.json")
            job = load_json(path)
            if job["id"] != directory.name:
                raise ValueError("任务存储标识不一致。")
            if job["status"] in {"preparing", "queued", "running"}:
                job.update(
                    status="interrupted",
                    phase="进程中断",
                    error="任务被中断；请检查目标插件和恢复资料，不自动继续覆盖。",
                )
                write_json(path, job)
            self.jobs[job["id"]] = job
        if self.rule_path.exists():
            rule = load_json(self.rule_path)
            exact_keys(rule, {"rule_version", "updated_at", "selections"})
            if type(rule["rule_version"]) is not int or rule["rule_version"] != 1:
                raise ValueError("导出规则版本不受支持。")
            validate_selections(rule["selections"], identities=True)

    @staticmethod
    def _expired(job: dict) -> bool:
        """Recognize files already cleaned by older releases; age never expires files."""
        return bool(job.get("expired"))

    def job_directory(self, identifier: str) -> Path:
        """Resolve only server-issued UUID job directories."""
        if (
            not isinstance(identifier, str)
            or len(identifier) != 32
            or any(char not in "0123456789abcdef" for char in identifier)
        ):
            raise ValueError("任务标识无效。")
        return safe_path(self.jobs_root, identifier)

    def _save(self, job: dict) -> None:
        write_json(safe_path(self.job_directory(job["id"]), "job.json"), job)

    def _new_job(self, owner: str, kind: str) -> dict:
        self._ensure_idle()
        identifier = uuid.uuid4().hex
        directory = self.job_directory(identifier)
        directory.mkdir(mode=0o700)
        job = {
            "id": identifier,
            "owner": owner,
            "kind": kind,
            "status": "preparing" if kind == "import" else "queued",
            "phase": "上传与预检" if kind == "import" else "排队",
            "created_at": time.time(),
            "updated_at": time.time(),
            "results": [],
            "warnings": [],
            "error": "",
            "expired": False,
            "backup_ready": False,
        }
        self.jobs[identifier] = job
        self._save(job)
        return job

    def _phase(self, job: dict, phase: str) -> None:
        job.update(phase=phase, updated_at=time.time())
        self._save(job)

    def get_job(self, identifier: str, owner: str) -> dict:
        """Bind job details and downloads to the authenticated creator."""
        self.job_directory(identifier)
        job = self.jobs.get(identifier)
        if job is None or job["owner"] != owner:
            raise ValueError("任务不存在或无权访问。")
        if identifier in self.deleting_jobs:
            raise ValueError("任务正在删除，请稍后重试。")
        return job

    async def delete_job(self, identifier: str, owner: str, acknowledged: bool) -> dict:
        """Delete one owned inactive job and all files under its verified directory."""
        self.operations.ensure_writable()
        if acknowledged is not True:
            raise ValueError("请确认删除任务记录及全部相关文件。")
        self.get_job(identifier, owner)
        self._ensure_idle()
        async with self.operation_lock:
            self._ensure_idle()
            job = self.get_job(identifier, owner)
            if job["status"] in {"preparing", "queued", "running"}:
                raise ValueError("任务正在上传、预检或执行，结束后才能删除。")
            self.deleting_jobs.add(identifier)
            deletion = asyncio.create_task(
                asyncio.to_thread(self._delete_job_files, job)
            )
            try:
                try:
                    await asyncio.shield(deletion)
                except asyncio.CancelledError:
                    await deletion
                    self.jobs.pop(identifier, None)
                    raise
                self.jobs.pop(identifier, None)
            finally:
                self.deleting_jobs.discard(identifier)
            return {"id": identifier, "deleted": True}

    def _delete_job_files(self, job: dict) -> None:
        self.job_directory(job["id"])
        try:
            remove_path(self.jobs_root, job["id"])
        except OSError:
            # A partial filesystem deletion must leave a readable task record.
            directory = self.job_directory(job["id"])
            directory.mkdir(exist_ok=True)
            self._save(job)
            raise ValueError(
                "部分任务文件未能删除，记录已保留，请检查文件权限后重试。"
            ) from None

    @staticmethod
    def public_job(job: dict) -> dict:
        """Return progress without private plans, configuration or KV content."""
        fields = {
            "id",
            "kind",
            "status",
            "phase",
            "created_at",
            "updated_at",
            "results",
            "warnings",
            "error",
            "expired",
            "backup_ready",
            "reload_required",
            "source_id",
            "name",
        }
        result = {
            key: copy.deepcopy(value) for key, value in job.items() if key in fields
        }
        result["expired"] = TransferService._expired(job)
        if job["kind"] in {"export", "manifest_export"}:
            result["download_filename"] = export_filename(
                job,
                {
                    "export": "package",
                    "manifest_export": "manifest",
                }[job["kind"]],
            )
        if job["kind"] == "rollback" and job["status"] == "preview":
            result.update(
                preview_expires_at=job["created_at"] + PREVIEW_TTL,
                preview_expired=time.time() > job["created_at"] + PREVIEW_TTL,
            )
        if (
            job["kind"] == "manifest_install"
            and not job.get("selected")
            and job.get("manifest", {}).get("schema_version") == 1
        ):
            result["preview_task"] = True
            if job["status"] == "preview":
                deadline = job.get(
                    "preview_expires_at", job["created_at"] + PREVIEW_TTL
                )
                result.update(
                    preview_expires_at=deadline, preview_expired=time.time() > deadline
                )
        return result

    def history(self, owner: str) -> list[dict]:
        return [
            self.public_job(job)
            for job in sorted(
                self.jobs.values(), key=lambda item: item["created_at"], reverse=True
            )
            if job["owner"] == owner
        ]

    def rule(self) -> dict:
        if self.rule_path.exists():
            return load_json(self.rule_path)
        return {"rule_version": 1, "updated_at": None, "selections": []}

    def _source(self, directory: str, *, require_runtime_identity=False) -> dict:
        component(directory, module=True)
        code = safe_path(self.code_root, directory)
        metadata = read_metadata(code)
        targets = [
            item
            for item in self.operations.context.get_all_stars()
            if item.root_dir_name == directory
        ]
        if len(targets) > 1:
            raise ValueError("插件目录归属不唯一。")
        target = (
            self.operations.resolve(directory)
            if targets or directory in self.manager.failed_plugin_dict
            else None
        )
        if target and target.reserved:
            raise ValueError("保留插件不参与插件包迁移。")
        if (
            target
            and target.plugin
            and (
                target.name != metadata["name"]
                or (
                    require_runtime_identity
                    and target.plugin.plugin_id != metadata["plugin_id"]
                )
            )
        ):
            raise ValueError(
                "磁盘元数据与运行时插件名或数据标识不一致，请先重载或检查插件。"
            )
        kv_plugin_id = (
            getattr(target.plugin.star_cls, "plugin_id", target.plugin.plugin_id)
            if target and target.plugin
            else metadata["plugin_id"]
        )
        return {
            "directory": directory,
            "metadata": metadata,
            "code": code,
            "config_path": safe_path(self.config_root, directory + "_config.json"),
            "data_path": safe_path(self.data_root, component(metadata["name"])),
            "target": target,
            "kv_plugin_id": kv_plugin_id,
            "module_path": target.plugin.module_path
            if target and target.plugin
            else f"data.plugins.{directory}.main",
        }

    def _is_self(self, source: dict) -> bool:
        target = source.get("target")
        return is_manager_plugin(
            {**source["metadata"], "directory": source["directory"]}
        ) or bool(
            target
            and target.plugin
            and target.plugin.module_path == self.operations.owner_module
        )

    def _inspect(self, directory: str, *, statistics: bool = False) -> dict:
        entry = {
            "directory": directory,
            "name": directory,
            "display_name": "",
            "version": "",
            "status": "failed",
            "reserved": False,
            "blocked": "",
            "metadata_changed": False,
            "metadata_notice": "",
            "data_available": False,
            "config_available": False,
            "code_size": None,
            "config_size": None,
            "data_size": None,
            "kv_count": None,
            "statistics_status": "pending",
            "ranges": [],
        }
        # A backup validation error must not invent a runtime loading failure.
        registered = [
            plugin
            for plugin in self.operations.context.get_all_stars()
            if plugin.root_dir_name == directory
        ]
        if len(registered) == 1:
            plugin = registered[0]
            entry.update(
                name=plugin.name,
                display_name=plugin.display_name or "",
                version=plugin.version or "",
                plugin_id=plugin.plugin_id,
                status="enabled" if plugin.activated else "disabled",
            )
        try:
            source = self._source(directory)
            metadata, target = source["metadata"], source["target"]
            entry.update(
                name=metadata["name"],
                display_name=metadata.get("display_name") or "",
                version=metadata["version"],
                plugin_id=metadata["plugin_id"],
                data_plugin_id=source["kv_plugin_id"],
                status=("enabled" if target.plugin.activated else "disabled")
                if target and target.plugin
                else ("failed" if target else "pending"),
                config_available=source["config_path"].is_file(),
                data_available=True,
                manager_data=self._is_self(source),
            )
            if target and target.plugin and target.plugin.author != metadata["author"]:
                entry["metadata_changed"] = True
                entry["metadata_notice"] = "插件信息已变更：作者。"
            if entry["manager_data"]:
                entry["ranges"].append(
                    "仅导出全部已安装插件的来源关联和已保存的导出规则。"
                )
            elif source["data_path"].exists():
                entry["ranges"].append(f"plugin_data/{metadata['name']}")
            if not entry["manager_data"]:
                entry["ranges"].append("专属 KV：" + source["kv_plugin_id"])
                if source["kv_plugin_id"] != metadata["plugin_id"]:
                    entry["ranges"].append(
                        "运行中的键值数据仍使用原作者标识；更新管家信息不会迁移数据。"
                    )
            if statistics:
                entry["code_size"] = sum(
                    path.stat().st_size
                    for path in tree_files(source["code"], code=True)
                )
                entry["config_size"] = (
                    source["config_path"].stat().st_size
                    if entry["config_available"]
                    else 0
                )
                entry["data_size"] = (
                    sum(path.stat().st_size for path in tree_files(source["data_path"]))
                    if source["data_path"].exists() and not entry["manager_data"]
                    else 0
                )
                entry["kv_count"] = 0
                entry["statistics_status"] = "ready"
        except (ValueError, OSError) as exc:
            entry["blocked"] = redact(str(exc))
            entry["statistics_status"] = "unavailable"
        return entry

    async def inventory(self, *, statistics: bool = False) -> list[dict]:
        """List identities first; inspect file trees and KV only when requested."""
        directories = {
            item.root_dir_name
            for item in self.operations.context.get_all_stars()
            if item.root_dir_name and not item.reserved
        }
        directories.update(self.manager.failed_plugin_dict)
        directories.update(
            await asyncio.to_thread(
                lambda: [
                    item.name
                    for item in self.code_root.iterdir()
                    if item.is_dir() and not item.name.startswith(".")
                ]
            )
        )
        result = await asyncio.to_thread(
            lambda: [
                self._inspect(directory, statistics=statistics)
                for directory in sorted(directories)
            ]
        )
        for entry in result:
            if statistics and entry.get("manager_data") and not entry["blocked"]:
                value, _ = await self.manager_data.snapshot()
                entry["data_size"] = len(
                    json.dumps(value, ensure_ascii=False).encode("utf-8")
                )
                continue
            if statistics and entry["data_available"] and not entry["blocked"]:
                records = await self.preferences.range_get_async(
                    "plugin", entry["data_plugin_id"]
                )
                entry["kv_count"] = len(records)
        return result

    async def save_rule(self, selections: list[dict]) -> dict:
        self.operations.ensure_writable()
        validate_selections(selections)
        self._ensure_idle()
        async with self.operation_lock:
            self._ensure_idle()
            self.operations.ensure_writable()
            return await self._save_rule(selections)

    async def _save_rule(self, selections: list[dict]) -> dict:
        rule = await self._selection_rule(selections)
        await asyncio.to_thread(write_json, self.rule_path, rule)
        return rule

    async def _selection_rule(self, selections: list[dict]) -> dict:
        """Capture identities for a selection without changing the saved rule."""
        validate_selections(selections)
        saved = []
        for selection in copy.deepcopy(selections):
            source = await asyncio.to_thread(self._source, selection["directory"])
            self._check_components(source, selection)
            saved.append(
                {
                    **selection,
                    "name": source["metadata"]["name"],
                    "plugin_id": source["metadata"]["plugin_id"],
                }
            )
        return {"rule_version": 1, "updated_at": time.time(), "selections": saved}

    def _check_components(self, source: dict, selection: dict) -> None:
        if selection["config"] and not source["config_path"].is_file():
            raise ValueError("已选择配置，但插件没有已保存配置。")
        if selection["data"] and not self._is_self(source):
            target = source.get("target")
            if target and target.plugin and target.plugin.activated:
                try:
                    self.operations.check_operation(target, "disable")
                except ValueError as exc:
                    reason = (
                        "插件标记为启用，但没有已初始化的运行实例。"
                        if target.plugin.star_cls is None
                        else str(exc)
                    )
                    raise ValueError(
                        f"插件 {source['directory']} 无法安全暂停以处理标准数据："
                        f"{reason}请在 WebUI 中检查，或取消勾选该插件的标准数据后重试。"
                    ) from exc
            if (
                target
                and target.plugin
                and target.plugin.activated
                and source["kv_plugin_id"] != source["metadata"]["plugin_id"]
            ):
                raise ValueError(
                    "运行中的键值数据仍使用原作者标识。请先停用该插件再备份标准数据，"
                    "避免备份后的自动重载改变数据归属；代码和配置可直接备份。"
                )
            required = {"flush", "range_get_async", "clear_async", "put_async"}
            if any(not hasattr(self.preferences, field) for field in required):
                raise ValueError("当前 AstrBot 缺少专属 KV 快照或恢复接口。")

    async def export_preview(self, selections: list[dict] | None = None) -> dict:
        rule = (
            self.rule()
            if selections is None
            else await self._selection_rule(selections)
        )
        if not rule["selections"]:
            raise ValueError("导出选择没有插件，请先选择插件。")
        validate_selections(rule["selections"], identities=True)
        sources = await self._rule_sources(rule)
        return {
            "plugins": [
                {
                    "directory": source["directory"],
                    "name": source["metadata"]["name"],
                    "display_name": source["metadata"].get("display_name") or "",
                    "version": source["metadata"]["version"],
                    "data": selection["data"],
                    "config": selection["config"],
                    "manager_data": self._is_self(source),
                    "pause_required": bool(
                        selection["data"]
                        and not self._is_self(source)
                        and source["target"]
                        and source["target"].plugin
                        and source["target"].plugin.activated
                    ),
                }
                for source, selection in zip(sources, rule["selections"])
            ],
            "rule_updated_at": rule["updated_at"],
            "selections": rule["selections"],
        }

    async def _rule_sources(self, rule: dict) -> list[dict]:
        sources = []
        for selection in rule["selections"]:
            source = await asyncio.to_thread(self._source, selection["directory"])
            if (
                source["metadata"]["name"] != selection["name"]
                or source["metadata"]["plugin_id"] != selection["plugin_id"]
            ):
                raise ValueError("导出选择中的插件身份变化，请重新选择并预检。")
            self._check_components(source, selection)
            sources.append(source)
        return sources

    def _ensure_idle(self) -> None:
        if self.closing or self.deleting_jobs or (self.task and not self.task.done()):
            raise ValueError("已有插件管理任务运行或管家正在关闭，请稍后重试。")

    def _start(self, job: dict, operation, *, preview: bool = False) -> dict:
        self._ensure_idle()
        if self.jobs.get(job["id"]) is not job:
            raise ValueError("任务记录已删除，请重新创建任务。")
        job["status"] = "queued"
        self._save(job)
        self.active_job = job["id"]
        self.task = asyncio.create_task(self._run(job, operation, preview=preview))
        return self.public_job(job)

    async def _run(self, job: dict, operation, *, preview: bool = False) -> None:
        try:
            async with self.operation_lock:
                if self.closing:
                    raise ValueError("插件管家正在关闭。")
                self.operations.ensure_writable()
                job["status"] = "running"
                self._phase(job, "预检")
                await operation(job)
                job["status"] = (
                    "preview"
                    if preview
                    else ("completed_with_warning" if job["warnings"] else "completed")
                )
                self._phase(
                    job, "清单预检完成，等待选择插件及安装来源" if preview else "完成"
                )
        except asyncio.CancelledError:
            job.update(
                status="interrupted",
                error="任务被插件关闭中断；请核对任务结果和已完成的插件，不自动续跑。",
            )
            self._phase(job, "中断")
        except Exception as exc:
            if job["status"] != "rollback_failed":
                job["status"] = "failed"
            job["error"] = (
                redact(str(exc))
                if isinstance(exc, ValueError)
                else "任务失败，请检查日志和恢复资料。"
            )
            logger.error(
                "Plugin transfer %s failed (%s): %s",
                job["id"],
                type(exc).__name__,
                job["error"],
            )
            if job["kind"] in {"import", "rollback"}:
                processed = {result["directory"] for result in job["results"]}
                for selection in job["restore"]:
                    if selection["directory"] not in processed:
                        job["results"].append(
                            {
                                "directory": selection["directory"],
                                "status": "not_executed",
                                "message": "前置检查或之前的插件恢复失败，本插件未执行。",
                            }
                        )
            self._phase(job, "失败")

    async def start_export(
        self,
        owner: str,
        rule_updated_at,
        acknowledged: bool,
        *,
        selections: list[dict] | None = None,
        name: str = "",
    ) -> dict:
        self.operations.ensure_writable()
        name = export_name(name)
        if acknowledged is not True:
            raise ValueError("请确认敏感内容和暂停插件风险。")
        if self.task and not self.task.done():
            raise ValueError("已有导入导出任务运行。")
        # Keep the legacy saved-rule path; page exports use their own snapshot.
        rule = (
            self.rule()
            if selections is None
            else {
                "rule_version": 1,
                "updated_at": None,
                "selections": copy.deepcopy(selections),
            }
        )
        if not rule["selections"]:
            raise ValueError("导出选择没有插件，请先选择插件。")
        if selections is None and rule["updated_at"] != rule_updated_at:
            raise ValueError("导出规则已变化，请重新预检。")
        validate_selections(rule["selections"], identities=True)
        await self._rule_sources(rule)
        job = self._new_job(owner, "export")
        job["name"] = name or f"astrbot-package-{job['id'][:8]}"
        job["rule"] = rule
        return self._start(job, self._export)

    async def _pause(self, source: dict) -> bool:
        target = source["target"]
        if not target or not target.plugin or not target.plugin.activated:
            return False
        self.operations.revalidate(target)
        self.operations.check_operation(target, "disable")
        if not hasattr(self.manager, "_iter_plugin_llm_tools"):
            raise ValueError("当前 AstrBot 缺少安全暂停插件工具的接口。")
        inactive = await self.preferences.global_get("inactivated_plugins", [])
        if source["module_path"] in inactive:
            raise ValueError("插件启停配置与运行状态不一致，请先检查。")
        source["needs_resume"] = True
        await self.manager._terminate_plugin(target.plugin)
        for tool in self.manager._iter_plugin_llm_tools(source["module_path"]):
            tool.active = False
        await self.preferences.global_put(
            "inactivated_plugins", [*inactive, source["module_path"]]
        )
        target.plugin.activated = False
        return True

    async def _resume(self, source: dict) -> None:
        module = source["module_path"]
        previous_instance = source["target"].plugin.star_cls
        await asyncio.to_thread(
            clear_plugin_bytecode, str(self.code_root), source["directory"]
        )
        inactive = await self.preferences.global_get("inactivated_plugins", [])
        await self.preferences.global_put(
            "inactivated_plugins", [item for item in inactive if item != module]
        )
        try:
            if any(
                plugin.module_path == module
                for plugin in self.operations.context.get_all_stars()
            ):
                await self.manager._unbind_plugin(source["metadata"]["name"], module)
            success, _ = await self.manager.load(specified_module_path=module)
            if not success:
                raise ValueError("插件恢复启用失败，请通过 WebUI 恢复。")
            registered = [
                plugin
                for plugin in self.operations.context.get_all_stars()
                if plugin.module_path == module
            ]
            if len(registered) != 1:
                raise ValueError("恢复后插件未唯一注册，请通过 WebUI 检查。")
            current = registered[0]
            if (
                current.name != source["metadata"]["name"]
                or current.plugin_id != source["metadata"]["plugin_id"]
                or current.root_dir_name != source["directory"]
                or not current.activated
                or current.star_cls is None
                or current.star_cls is previous_instance
            ):
                raise ValueError("恢复后插件未正确初始化，请通过 WebUI 检查。")
        except Exception:
            for current in self.operations.context.get_all_stars():
                if current.module_path == module:
                    current.activated = False
                    try:
                        await self.manager._terminate_plugin(current)
                    except Exception:
                        logger.error(
                            "Failed to terminate a plugin after transfer resume failure."
                        )
            for tool in self.manager._iter_plugin_llm_tools(module):
                tool.active = False
            inactive = await self.preferences.global_get("inactivated_plugins", [])
            if module not in inactive:
                await self.preferences.global_put(
                    "inactivated_plugins", [*inactive, module]
                )
            raise

    async def _read_kv(self, plugin_id: str) -> dict:
        await self.preferences.flush()
        records = await self.preferences.range_get_async("plugin", plugin_id)
        result = {}
        for record in records:
            if (
                not isinstance(record.key, str)
                or record.key in result
                or not isinstance(record.value, dict)
                or "val" not in record.value
            ):
                raise ValueError("插件 KV 注册数据不完整或重复。")
            result[record.key] = encode_value(record.value["val"])
        return result

    async def _write_kv(self, plugin_id: str, values: dict) -> None:
        await self.preferences.clear_async("plugin", plugin_id)
        for key, value in values.items():
            await self.preferences.put_async(
                "plugin", plugin_id, key, decode_value(value)
            )
        await self.preferences.flush()

    @staticmethod
    def _path_size(path: Path, *, code: bool = False) -> int:
        if path.is_dir():
            return sum(item.stat().st_size for item in tree_files(path, code=code))
        return path.stat().st_size if path.exists() else 0

    async def _source_size(self, source: dict, selection: dict, *, export: bool) -> int:
        size = 0
        for kind, path in (
            ("code", source["code"]),
            ("config", source["config_path"]),
            ("data", source["data_path"]),
        ):
            if selection[kind] and not (kind == "data" and self._is_self(source)):
                size += await asyncio.to_thread(
                    self._path_size, path, code=export and kind == "code"
                )
        if selection["data"]:
            if self._is_self(source):
                values = (
                    (await self.manager_data.snapshot())[0]
                    if export
                    else self.manager_data.capture()
                )
            else:
                values = await self._read_kv(source["kv_plugin_id"])
            size += len(
                json.dumps(values, ensure_ascii=False, indent=2).encode("utf-8")
            )
        return size

    async def _require_space(self, size: int) -> None:
        required = size + SPACE_MARGIN
        available = await asyncio.to_thread(shutil.disk_usage, self.root)
        if available.free < required:
            raise ValueError(
                f"磁盘可用空间不足，当前任务需至少 {required / 1024 / 1024:.1f} MiB 可用空间。"
            )

    @staticmethod
    async def _copy_snapshot_tree(
        source: Path, destination: Path, context: str, *, code: bool = False
    ) -> None:
        try:
            await asyncio.to_thread(copy_tree, source, destination, code=code)
        except ValueError as exc:
            raise ValueError(f"{context}快照失败：{exc}") from exc

    async def _snapshot(
        self, source: dict, selection: dict, stage: Path, was_enabled: bool
    ) -> dict:
        directory = source["directory"]
        context = f"插件 {source['metadata']['name']}（目录 {directory}）"
        prefix = safe_path(stage, f"payload/{directory}")
        prefix.mkdir(parents=True)
        await self._copy_snapshot_tree(
            source["code"],
            prefix / "code",
            f"{context}的代码目录（plugins/{directory}/）",
            code=True,
        )
        metadata = read_metadata(prefix / "code")
        if any(
            metadata[field] != source["metadata"][field]
            for field in ("name", "author", "version", "plugin_id")
        ):
            raise ValueError("快照期间插件身份变化。")
        if selection["config"]:
            before = digest(source["config_path"])
            await asyncio.to_thread(
                shutil.copyfile, source["config_path"], prefix / "config.json"
            )
            load_json(prefix / "config.json")
            if before != digest(source["config_path"]) or before != digest(
                prefix / "config.json"
            ):
                raise ValueError("快照期间配置变化。")
        file_data = bool(
            selection["data"]
            and not self._is_self(source)
            and source["data_path"].exists()
        )
        if selection["data"] and self._is_self(source):
            value, warnings = await self.manager_data.snapshot()
            await asyncio.to_thread(write_json, prefix / "manager-data.json", value)
            self.jobs[self.active_job]["warnings"].extend(warnings)
        elif selection["data"]:
            if file_data:
                await self._copy_snapshot_tree(
                    source["data_path"],
                    prefix / "data",
                    f"{context}的标准数据目录"
                    f"（plugin_data/{source['metadata']['name']}/）",
                )
            values = await self._read_kv(source["kv_plugin_id"])
            await asyncio.to_thread(write_json, prefix / "kv.json", values)
        return {
            "name": metadata["name"],
            "directory": directory,
            "display_name": metadata.get("display_name") or "",
            "author": metadata["author"],
            "version": metadata["version"],
            "plugin_id": metadata["plugin_id"],
            "was_enabled": was_enabled,
            "config": selection["config"],
            "data": selection["data"],
            "file_data": file_data,
        }

    async def _export(self, job: dict) -> None:
        directory = self.job_directory(job["id"])
        stage = safe_path(directory, "stage")
        stage.mkdir()
        sources = []
        manifest_plugins = []
        async with self.manager._pm_lock:
            self.in_critical = True
            try:
                sources = await self._rule_sources(job["rule"])
                size = 0
                for source, selection in zip(sources, job["rule"]["selections"]):
                    size += await self._source_size(
                        source,
                        {
                            "code": True,
                            "config": selection["config"],
                            "data": selection["data"],
                        },
                        export=True,
                    )
                await self._require_space(size * 2)
                for source, selection in zip(sources, job["rule"]["selections"]):
                    source["was_enabled"] = bool(
                        source["target"]
                        and source["target"].plugin
                        and source["target"].plugin.activated
                    )
                    if selection["data"] and not self._is_self(source):
                        self._phase(job, "暂停并创建数据快照：" + source["directory"])
                        await self._pause(source)
                for source, selection in zip(sources, job["rule"]["selections"]):
                    self._phase(job, "创建快照：" + source["directory"])
                    manifest_plugins.append(
                        await self._snapshot(
                            source, selection, stage, source["was_enabled"]
                        )
                    )
                    (
                        source_data,
                        warnings,
                    ) = await self.operations.sources.export_sources(
                        source["directory"], source["metadata"]
                    )
                    await asyncio.to_thread(
                        write_json,
                        safe_path(stage, f"payload/{source['directory']}/sources.json"),
                        source_data,
                    )
                    job["warnings"].extend(warnings)
                    job["results"].append(
                        {"directory": source["directory"], "status": "snapshot"}
                    )
                    self._save(job)
            finally:
                for source in reversed(sources):
                    if not source.get("needs_resume"):
                        continue
                    try:
                        await self._resume(source)
                    except Exception:
                        job["warnings"].append(
                            source["directory"] + " 恢复启用失败，请通过 WebUI 恢复。"
                        )
                self.in_critical = False
        self._phase(job, "压缩插件包")
        manifest = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "created_at": job["created_at"],
            "astrbot_version": ASTRBOT_VERSION,
            "platform": platform.system(),
            "plugins": manifest_plugins,
        }
        await asyncio.to_thread(pack, stage, directory / "package.zip", manifest)
        await asyncio.to_thread(remove_path, directory, "stage")

    def new_upload(self, owner: str) -> dict:
        self.operations.ensure_writable()
        if self.closing or (self.task and not self.task.done()):
            raise ValueError("已有导入导出任务运行或管家正在关闭。")
        return self._new_job(owner, "import")

    async def prepare_import(self, job: dict) -> dict:
        directory = self.job_directory(job["id"])
        try:
            manifest = await asyncio.to_thread(
                unpack, directory / "upload.zip", directory / "checked"
            )
            plan = []
            checks = {}
            for plugin in manifest["plugins"]:
                self._protect_import(plugin)
                existing = await asyncio.to_thread(self._existing, plugin)
                checks[plugin["directory"]] = await asyncio.to_thread(
                    self._fingerprint, plugin["directory"]
                )
                code = safe_path(
                    directory / "checked", f"payload/{plugin['directory']}/code"
                )
                problem = ""
                try:
                    if not is_manager_plugin(plugin):
                        await asyncio.to_thread(
                            check_environment, code, ASTRBOT_VERSION
                        )
                except ValueError as exc:
                    problem = redact(str(exc))
                plan.append(
                    {
                        **plugin,
                        "exists": existing is not None,
                        "problem": problem,
                        "manager_data": is_manager_plugin(plugin),
                    }
                )
            if any(
                is_manager_plugin(plugin) and plugin["data"]
                for plugin in manifest["plugins"]
            ):
                await self.operations.sources.inventory()
                job["manager_data_check"] = self.manager_data.state()
            job.update(status="preview", manifest=manifest, checks=checks, plan=plan)
            if manifest["platform"] != platform.system():
                job["warnings"].append(
                    "来源系统不同，数据内的绝对路径和外部资源不会自动迁移。"
                )
            self._phase(job, "预检完成，等待选择与确认")
            return {
                "job": self.public_job(job),
                "plugins": plan,
                "astrbot_version": manifest["astrbot_version"],
                "expires_at": job["created_at"] + PREVIEW_TTL,
            }
        except Exception:
            for key in ("manifest", "checks", "plan"):
                job.pop(key, None)
            try:
                for name in ("upload.zip", "checked"):
                    await asyncio.to_thread(remove_path, directory, name)
            finally:
                job.update(
                    status="failed",
                    error="上传包未通过预检，请检查包格式、身份和安全限制。",
                )
                self._save(job)
            raise

    def _protect_import(self, plugin: dict) -> None:
        if is_manager_plugin(plugin):
            source = self._existing(plugin)
            if not source or source["module_path"] != self.operations.owner_module:
                raise ValueError("仅能向当前运行的插件管家恢复来源关联和导出规则。")
            return
        for registered in self.operations.context.get_all_stars():
            if (
                registered.reserved
                or registered.module_path == self.operations.owner_module
            ):
                if (
                    plugin["name"].casefold() == (registered.name or "").casefold()
                    or plugin["directory"].casefold()
                    == (registered.root_dir_name or "").casefold()
                ):
                    raise ValueError("不能覆盖保留插件或插件管家。")

    def _existing(self, plugin: dict):
        code = safe_path(self.code_root, plugin["directory"])
        for registered in self.operations.context.get_all_stars():
            if (
                registered.name
                and registered.name.casefold() == plugin["name"].casefold()
                and registered.root_dir_name != plugin["directory"]
            ):
                raise ValueError("同名插件已存在于其他目录，不自动改名或替换。")
        for directory in self.code_root.iterdir():
            if (
                directory.name.casefold() == plugin["directory"].casefold()
                or directory.name.startswith(".")
                or not directory.is_dir()
            ):
                continue
            directory = safe_path(self.code_root, directory.name)
            if not safe_path(directory, "metadata.yaml").is_file():
                continue
            try:
                metadata = read_metadata(directory)
            except (ValueError, OSError) as exc:
                raise ValueError(
                    "已有插件元数据无效，无法完整排除身份冲突，请先检查："
                    + directory.name
                ) from exc
            if metadata["name"].casefold() == plugin["name"].casefold():
                raise ValueError("同名插件已存在于其他目录，不自动改名或替换。")
        if not code.exists():
            return None
        source = self._source(plugin["directory"], require_runtime_identity=True)
        if (
            source["metadata"]["name"] != plugin["name"]
            or source["metadata"]["plugin_id"] != plugin["plugin_id"]
        ):
            raise ValueError("目标目录已有不同身份的插件，拒绝覆盖。")
        return source

    def _fingerprint(self, directory: str) -> str:
        code = safe_path(self.code_root, directory)
        if not code.exists():
            return "absent"
        source = self._source(directory)
        target = source["target"]
        value = {
            "code": stamp(code),
            "config": digest(source["config_path"])
            if source["config_path"].exists()
            else None,
            "state": repr(target.state) if target else "pending",
        }
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def _check_restoration(self, job: dict, plugin: dict, selection: dict) -> None:
        self._protect_import(plugin)
        source = self._existing(plugin)
        if is_manager_plugin(plugin):
            if selection["code"] or selection["config"]:
                raise ValueError(
                    "管家仅允许恢复来源关联和导出规则，不能在线覆盖代码或配置。"
                )
            if not selection["data"] or not plugin["data"]:
                raise ValueError("插件包未携带管家的来源关联和导出规则。")
            return
        if source is None and not selection["code"]:
            raise ValueError("新插件必须选择恢复代码。")
        if (
            selection["config"]
            and not plugin["config"]
            or selection["data"]
            and not plugin["data"]
        ):
            raise ValueError("选择了包中未携带的组件。")
        code = (
            safe_path(
                self.job_directory(job["id"]) / "checked",
                f"payload/{plugin['directory']}/code",
            )
            if selection["code"]
            else source["code"]
        )
        check_environment(code, ASTRBOT_VERSION)
        if selection["config"]:
            config = load_json(
                safe_path(
                    self.job_directory(job["id"]) / "checked",
                    f"payload/{plugin['directory']}/config.json",
                )
            )
            schema = load_json(safe_path(code, "_conf_schema.json"))
            check_config(config, schema)
        if selection["data"]:
            self._check_components(
                source
                or {
                    "metadata": plugin,
                    "directory": plugin["directory"],
                    "target": None,
                },
                {"config": False, "data": True},
            )

    async def start_import(
        self, identifier: str, owner: str, selections, acknowledged: bool
    ) -> dict:
        self.operations.ensure_writable()
        job = self.get_job(identifier, owner)
        if (
            job["kind"] != "import"
            or job["status"] != "preview"
            or time.time() > job["created_at"] + PREVIEW_TTL
        ):
            raise ValueError("导入预检已过期或已使用，请重新上传。")
        if acknowledged is not True:
            raise ValueError("请确认来源可信、覆盖风险和敏感内容。")
        check_bundle_format(job["manifest"])
        for plugin in job["manifest"]["plugins"]:
            await asyncio.to_thread(
                check_source_snapshot,
                self.job_directory(job["id"]) / "checked",
                plugin,
                job["manifest"]["files"],
            )
        if not isinstance(selections, list) or not selections:
            raise ValueError("请选择需要恢复的插件和组件。")
        incoming = {
            plugin["directory"]: plugin for plugin in job["manifest"]["plugins"]
        }
        seen = set()
        for selection in selections:
            exact_keys(selection, {"directory", "code", "config", "data"})
            directory = selection["directory"]
            if (
                directory not in incoming
                or directory in seen
                or any(
                    type(selection[key]) is not bool
                    for key in ("code", "config", "data")
                )
                or not any(selection[key] for key in ("code", "config", "data"))
            ):
                raise ValueError("恢复选择无效、重复或没有组件。")
            seen.add(directory)
            await asyncio.to_thread(
                self._check_restoration, job, incoming[directory], selection
            )
            if job["checks"][directory] != await asyncio.to_thread(
                self._fingerprint, directory
            ):
                raise ValueError("目标插件状态或文件已变化，请重新上传预检。")
        await self._check_manager_restore(job, selections)
        job["restore"] = sorted(
            copy.deepcopy(selections),
            key=lambda item: is_manager_plugin(incoming[item["directory"]]),
        )
        return self._start(job, self._import)

    async def _check_manager_restore(self, job: dict, selections: list[dict]) -> None:
        incoming = {
            plugin["directory"]: plugin for plugin in job["manifest"]["plugins"]
        }
        managers = [
            incoming[item["directory"]]
            for item in selections
            if is_manager_plugin(incoming[item["directory"]])
        ]
        if not managers:
            return
        await self.operations.sources.inventory()
        if self.manager_data.state() != job["manager_data_check"]:
            raise ValueError("来源关联或导出规则在预检后变化，请重新上传预检。")
        replacements = {}
        for selection in selections:
            plugin = incoming[selection["directory"]]
            if is_manager_plugin(plugin):
                continue
            existing = await asyncio.to_thread(self._existing, plugin)
            prefix = safe_path(
                self.job_directory(job["id"]) / "checked",
                "payload/" + plugin["directory"],
            )
            replacements[plugin["directory"]] = {
                "directory": plugin["directory"],
                "metadata": plugin if selection["code"] else existing["metadata"],
                "target": None,
                "kv_plugin_id": plugin["plugin_id"],
                "config_path": prefix / "config.json"
                if selection["config"]
                else safe_path(self.config_root, plugin["directory"] + "_config.json"),
            }
        for plugin in managers:
            plan = await self.manager_data.plan(
                self._manager_payload(job, plugin), replacements
            )
            if not plan["rule"]["ready"]:
                raise ValueError("管家导出规则无法恢复：" + plan["rule"]["problem"])

    def _manager_payload(self, job: dict, plugin: dict) -> dict:
        return validate_manager_data(
            load_json(
                safe_path(
                    self.job_directory(job["id"]) / "checked",
                    f"payload/{plugin['directory']}/manager-data.json",
                )
            )
        )

    def _verify_checked(self, job: dict) -> None:
        root = safe_path(self.job_directory(job["id"]), "checked")
        manifest = job["manifest"]
        check_bundle_format(manifest)
        for plugin in manifest["plugins"]:
            check_source_snapshot(root, plugin, manifest["files"])
        actual = {path.relative_to(root).as_posix() for path in tree_files(root)} - {
            "manifest.json"
        }
        if actual != set(manifest["files"]):
            raise ValueError("暂存包内容发生变化，请重新上传。")
        directories = {
            path.relative_to(root).as_posix() for path in tree_directories(root)
        }
        if directories != set(manifest["directories"]):
            raise ValueError("暂存包目录发生变化，请重新上传。")
        for relative, record in manifest["files"].items():
            path = safe_path(root, relative)
            if (
                digest(path) != record["sha256"]
                or path.stat().st_size != record["size"]
            ):
                raise ValueError("暂存包校验失败，请重新上传。")

    def _paths(self, plugin: dict) -> dict:
        return {
            "code": (self.code_root, plugin["directory"]),
            "config": (self.config_root, plugin["directory"] + "_config.json"),
            "data": (self.data_root, component(plugin["name"])),
        }

    async def _backup(self, job: dict, plugin: dict, selection: dict, source) -> dict:
        before = safe_path(
            self.job_directory(job["id"]), "before/" + plugin["directory"]
        )
        before.mkdir(parents=True)
        record = {
            "directory": plugin["directory"],
            "name": plugin["name"],
            "plugin_id": plugin["plugin_id"],
            "module_path": source["module_path"]
            if source
            else f"data.plugins.{plugin['directory']}.main",
            "was_enabled": bool(source and source["was_enabled"]),
            "was_inactive": source["was_inactive"]
            if source
            else f"data.plugins.{plugin['directory']}.main"
            in await self.preferences.global_get("inactivated_plugins", []),
            "components": {},
        }
        if selection["code"] and hasattr(self.operations, "sources"):
            record["source_state"] = await self.operations.sources.source_snapshot(
                plugin["directory"], plugin["name"]
            )
        if is_manager_plugin(plugin):
            record["components"] = {"data": True}
            value = self.manager_data.capture()
            self.manager_data.validate_backup(value)
            await asyncio.to_thread(write_json, before / "manager-data.json", value)
        for kind, (root, relative) in (
            () if is_manager_plugin(plugin) else self._paths(plugin).items()
        ):
            if not selection[kind]:
                continue
            path = safe_path(root, relative)
            record["components"][kind] = path.exists()
            if path.is_dir():
                await asyncio.to_thread(copy_tree, path, before / kind)
            elif path.is_file():
                await asyncio.to_thread(shutil.copyfile, path, before / kind)
        if selection["data"] and not is_manager_plugin(plugin):
            await asyncio.to_thread(
                write_json, before / "kv.json", await self._read_kv(plugin["plugin_id"])
            )
        journal_path = safe_path(self.job_directory(job["id"]), "rollback.json")
        journal = (
            load_json(journal_path)
            if journal_path.exists()
            else {
                "format": FORMAT + "-rollback",
                "format_version": 1,
                "plugins": [],
                "job_id": job["id"],
                "created_at": job["created_at"],
            }
        )
        journal["plugins"].append(record)
        await asyncio.to_thread(write_json, journal_path, journal)
        await asyncio.to_thread(self._pack_rollback, job)
        job["backup_ready"] = True
        self._save(job)
        return record

    def _pack_rollback(self, job: dict) -> None:
        directory = self.job_directory(job["id"])
        temporary = safe_path(directory, "rollback.tmp")
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.write(directory / "rollback.json", "rollback.json")
            for path in tree_directories(directory / "before"):
                archive.write(
                    path,
                    "before/" + path.relative_to(directory / "before").as_posix() + "/",
                )
            for path in tree_files(directory / "before"):
                archive.write(
                    path, "before/" + path.relative_to(directory / "before").as_posix()
                )
        if temporary.stat().st_size > MAX_ARCHIVE:
            temporary.unlink()
            raise ValueError("恢复资料包超过限制，尚未覆盖当前插件。")
        temporary.replace(directory / "rollback.zip")
        os.chmod(directory / "rollback.zip", 0o600)

    @staticmethod
    def _replace(root: Path, relative: str, source: Path | None) -> None:
        destination = safe_path(root, relative)
        if source is None:
            remove_path(root, relative)
            return
        temporary_name = ".pm-stage-" + uuid.uuid4().hex
        old_name = ".pm-before-" + uuid.uuid4().hex
        temporary = safe_path(root, temporary_name)
        old = safe_path(root, old_name)
        installed = False
        try:
            if source.is_dir():
                copy_tree(source, temporary)
            else:
                shutil.copyfile(source, temporary)
            if destination.exists():
                destination.replace(old)
            try:
                temporary.replace(destination)
                installed = True
            except Exception:
                if old.exists():
                    old.replace(destination)
                raise
        finally:
            remove_path(root, temporary_name)
            if installed:
                remove_path(root, old_name)

    async def _restore_backup(self, job: dict, plugin: dict, record: dict) -> None:
        before = safe_path(
            self.job_directory(job["id"]), "before/" + plugin["directory"]
        )
        if is_manager_plugin(plugin):
            await self.manager_data.restore(load_json(before / "manager-data.json"))
            return
        for kind, existed in record["components"].items():
            root, relative = self._paths(plugin)[kind]
            await asyncio.to_thread(
                self._replace, root, relative, before / kind if existed else None
            )
        if "data" in record["components"]:
            await self._write_kv(plugin["plugin_id"], load_json(before / "kv.json"))
        inactive = await self.preferences.global_get("inactivated_plugins", [])
        module = record["module_path"]
        inactive = [item for item in inactive if item != module]
        if record["was_inactive"]:
            inactive.append(module)
        await self.preferences.global_put("inactivated_plugins", inactive)

        if "source_state" in record:
            await self.operations.sources.restore_sources(record["source_state"])

    async def _import(self, job: dict) -> None:
        incoming = {
            plugin["directory"]: plugin for plugin in job["manifest"]["plugins"]
        }
        async with self.manager._pm_lock:
            self.in_critical = True
            try:
                if time.time() > job["created_at"] + PREVIEW_TTL:
                    raise ValueError("导入预检在排队期间过期，请重新上传。")
                await asyncio.to_thread(self._verify_checked, job)
                await self._check_manager_restore(job, job["restore"])
                space = 0
                for selection in job["restore"]:
                    directory = selection["directory"]
                    if job["checks"][directory] != await asyncio.to_thread(
                        self._fingerprint, directory
                    ):
                        raise ValueError("目标状态在排队期间发生变化，请重新预检。")
                    await asyncio.to_thread(
                        self._check_restoration, job, incoming[directory], selection
                    )
                    source = await asyncio.to_thread(
                        self._existing, incoming[directory]
                    )
                    if is_manager_plugin(incoming[directory]):
                        space += 3 * len(
                            json.dumps(
                                self.manager_data.capture(), ensure_ascii=False
                            ).encode("utf-8")
                        )
                        space += len(
                            json.dumps(
                                self._manager_payload(job, incoming[directory]),
                                ensure_ascii=False,
                            ).encode("utf-8")
                        )
                        continue
                    paths = self._paths(incoming[directory])
                    previous = source or {
                        "directory": directory,
                        "metadata": incoming[directory],
                        "code": safe_path(*paths["code"]),
                        "config_path": safe_path(*paths["config"]),
                        "data_path": safe_path(*paths["data"]),
                        "kv_plugin_id": incoming[directory]["plugin_id"],
                    }
                    space += 3 * await self._source_size(
                        previous, selection, export=False
                    )
                    prefix = safe_path(
                        self.job_directory(job["id"]) / "checked",
                        "payload/" + directory,
                    )
                    for kind, filename in (
                        ("code", "code"),
                        ("config", "config.json"),
                        ("data", "data"),
                    ):
                        if selection[kind]:
                            space += await asyncio.to_thread(
                                self._path_size, prefix / filename
                            )
                    if selection["data"]:
                        space += (prefix / "kv.json").stat().st_size
                await self._require_space(space)
                for selection in job["restore"]:
                    plugin = incoming[selection["directory"]]
                    source = await asyncio.to_thread(self._existing, plugin)
                    paused = False
                    backup = None
                    self._phase(job, "备份并恢复：" + plugin["directory"])
                    try:
                        if source:
                            source["was_enabled"] = bool(
                                source["target"]
                                and source["target"].plugin
                                and source["target"].plugin.activated
                            )
                            source["was_inactive"] = source[
                                "module_path"
                            ] in await self.preferences.global_get(
                                "inactivated_plugins", []
                            )
                            if not is_manager_plugin(plugin):
                                paused = await self._pause(source)
                        backup = await self._backup(job, plugin, selection, source)
                        if is_manager_plugin(plugin):
                            job["warnings"].extend(
                                await self.manager_data.apply(
                                    self._manager_payload(job, plugin), job["owner"]
                                )
                            )
                            job["results"].append(
                                {
                                    "directory": plugin["directory"],
                                    "status": "restored",
                                    "message": "来源关联已合并，导出规则已恢复；管家保持运行。",
                                }
                            )
                            self._save(job)
                            continue
                        prefix = safe_path(
                            self.job_directory(job["id"]) / "checked",
                            "payload/" + plugin["directory"],
                        )
                        paths = self._paths(plugin)
                        for kind, (root, relative) in paths.items():
                            if not selection[kind]:
                                continue
                            staged = (
                                prefix
                                / {
                                    "code": "code",
                                    "config": "config.json",
                                    "data": "data",
                                }[kind]
                            )
                            if kind == "data" and not plugin["file_data"]:
                                staged = None
                            elif kind == "data" and not staged.exists():
                                staged.mkdir()
                            await asyncio.to_thread(
                                self._replace, root, relative, staged
                            )
                        if selection["data"]:
                            await self._write_kv(
                                plugin["plugin_id"], load_json(prefix / "kv.json")
                            )
                        module = (
                            source["module_path"]
                            if source
                            else f"data.plugins.{plugin['directory']}.main"
                        )
                        inactive = await self.preferences.global_get(
                            "inactivated_plugins", []
                        )
                        if module not in inactive:
                            await self.preferences.global_put(
                                "inactivated_plugins", [*inactive, module]
                            )
                        if source and source["target"] and source["target"].plugin:
                            await self.manager._unbind_plugin(
                                source["metadata"]["name"], module
                            )
                        self.manager.failed_plugin_dict.pop(plugin["directory"], None)
                        if selection["code"] and hasattr(self.operations, "sources"):
                            incoming_sources = await asyncio.to_thread(
                                load_json, prefix / "sources.json"
                            )
                            await self.operations.sources.note_bundle(
                                plugin["directory"], job["owner"], incoming_sources
                            )
                        job["results"].append(
                            {
                                "directory": plugin["directory"],
                                "status": "restored",
                                "message": "已恢复到磁盘并保持停用；可点击「待加载」确认加载启用，或重启 AstrBot 后在 WebUI 手动启用。",
                            }
                        )
                        job["reload_required"] = True
                        self._save(job)
                    except Exception:
                        try:
                            if backup:
                                await self._restore_backup(job, plugin, backup)
                            if paused or (source and source.get("needs_resume")):
                                await self._resume(source)
                            job["results"].append(
                                {
                                    "directory": plugin["directory"],
                                    "status": "failed",
                                    "message": "恢复失败，已恢复本机备份；后续插件未执行。",
                                }
                            )
                        except Exception:
                            job["status"] = "rollback_failed"
                            job["results"].append(
                                {
                                    "directory": plugin["directory"],
                                    "status": "rollback_failed",
                                    "message": "自动恢复失败，请停止 AstrBot 并使用恢复资料手工检查。",
                                }
                            )
                        self._save(job)
                        raise
            finally:
                self.in_critical = False
        if job.get("reload_required"):
            try:
                await self.operations.web.sync_skills_after_plugin_change()
            except Exception:
                job["warnings"].append(
                    "插件文件已恢复，但技能同步失败，请重启 AstrBot 后检查。"
                )
        for name in ("upload.zip", "checked"):
            await asyncio.to_thread(remove_path, self.job_directory(job["id"]), name)

    def download(self, identifier: str, owner: str, kind: str) -> Path:
        job = self.get_job(identifier, owner)
        if self._expired(job):
            raise ValueError("任务文件已被旧版清理，请重新导出。")
        if kind == "package":
            if job["kind"] != "export" or job["status"] not in {
                "completed",
                "completed_with_warning",
            }:
                raise ValueError("导出包尚未完成。")
        elif kind == "rollback":
            if not job.get("backup_ready"):
                raise ValueError("任务没有可下载的恢复资料。")
            if job["status"] in {"running", "queued"}:
                raise ValueError("请等待任务结束后下载恢复资料。")
        else:
            raise ValueError("下载类型无效。")
        path = safe_path(self.job_directory(identifier), kind + ".zip")
        if not path.is_file():
            raise ValueError("任务文件不存在。")
        return path

    async def close(self) -> None:
        """Cancel waiting work, but finish any entered filesystem critical section."""
        self.closing = True
        if self.task and not self.task.done():
            if not self.in_critical:
                self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                job = self.jobs[self.active_job]
                job.update(
                    status="interrupted",
                    error="等待中的任务被插件关闭中断，正式目录未修改。",
                )
                self._phase(job, "中断")

    @staticmethod
    def limits() -> dict:
        return {
            "archive_bytes": MAX_ARCHIVE,
            "unpacked_bytes": MAX_CONTENT,
            "file_bytes": MAX_FILE,
            "file_count": MAX_FILES,
            "retention_days": None,
            "preview_seconds": PREVIEW_TTL,
        }
