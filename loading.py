"""Explicitly load one unregistered local plugin through AstrBot's lifecycle."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .bytecode import clear_plugin_bytecode
from .redaction import redact
from .transfer_bundle import component, safe_path

logger = logging.getLogger("astrbot")


class PluginLoadService:
    def __init__(self, sources, transfers) -> None:
        self.sources = sources
        self.transfers = transfers
        self.operations = transfers.operations

    def _source(self, directory: str) -> dict:
        reserved_root = Path(self.operations.manager.reserved_plugin_path).absolute()
        if safe_path(reserved_root, directory).exists():
            raise ValueError("插件目录与 AstrBot 保留插件冲突，无法加载。")
        return self.transfers._source(directory)

    async def _pending(self, directory: str, revision: str) -> dict:
        component(directory, module=True)
        record = await self.sources.detail(directory)
        local = record["local"]
        if record["revision"] != revision:
            raise ValueError("插件信息或状态已变化，请刷新列表后重新确认加载。")
        if local["status"] != "pending" or local["reserved"]:
            raise ValueError("只有待加载的普通插件可以执行此操作。")
        if local["problem"] or record["identity_changed"]:
            raise ValueError(local["problem"] or "插件目录身份冲突，请先检查插件。")
        source = await asyncio.to_thread(self._source, directory)
        if any(
            (source["metadata"].get(field) or "") != (local[field] or "")
            for field in ("name", "author", "version", "display_name")
        ):
            raise ValueError("插件文件中的信息已变化，请刷新列表后重新确认加载。")
        if source["target"] is not None:
            raise ValueError("插件已注册或加载失败，请刷新列表并在官方 WebUI 处理。")
        metadata = source["metadata"]
        if any(
            item.name == metadata["name"] or item.root_dir_name == directory
            for item in self.operations.context.get_all_stars()
        ) or any(
            item.get("name") == metadata["name"]
            for item in self.operations.manager.failed_plugin_dict.values()
        ):
            raise ValueError("插件名或目录与已有插件冲突，无法加载。")
        return source

    async def start(
        self, owner: str, directory: str, revision: str, acknowledged: bool
    ) -> dict:
        self.operations.ensure_writable()
        if acknowledged is not True:
            raise ValueError("请确认加载并启用插件。")
        if not isinstance(revision, str) or not revision:
            raise ValueError("缺少有效的插件状态，请刷新列表。")
        self.transfers._ensure_idle()
        async with self.transfers.operation_lock:
            self.transfers._ensure_idle()
            await self._pending(directory, revision)
            job = self.transfers._new_job(owner, "plugin_load")
            job["load_plan"] = {"directory": directory, "revision": revision}
            return self.transfers._start(job, self._load)

    async def _load(self, job: dict) -> None:
        directory = job["load_plan"]["directory"]
        manager = self.operations.manager
        async with manager._pm_lock:
            source = await self._pending(directory, job["load_plan"]["revision"])
            module_path = source["module_path"]
            preferences = self.transfers.preferences
            inactive = await preferences.global_get("inactivated_plugins", [])
            was_inactive = module_path in inactive
            self.transfers.in_critical = True
            try:
                self.transfers._phase(job, "正在加载并启用：" + directory)
                await asyncio.to_thread(
                    clear_plugin_bytecode, str(source["code"].parent), directory
                )
                if was_inactive:
                    await preferences.global_put(
                        "inactivated_plugins",
                        [item for item in inactive if item != module_path],
                    )
                success, error = await manager.load(specified_dir_name=directory)
                if not success:
                    raise ValueError(error or "AstrBot 未能加载目标插件。")
                registered = [
                    item
                    for item in self.operations.context.get_all_stars()
                    if item.root_dir_name == directory
                ]
                if (
                    len(registered) != 1
                    or registered[0].plugin_id != source["metadata"]["plugin_id"]
                    or registered[0].module_path != module_path
                    or registered[0].version != source["metadata"]["version"]
                    or not registered[0].activated
                    or registered[0].star_cls is None
                ):
                    raise ValueError("AstrBot 未注册并初始化预期插件，请在官方 WebUI 检查。")
            except Exception as exc:
                for item in list(self.operations.context.get_all_stars()):
                    if item.root_dir_name == directory:
                        try:
                            await manager._terminate_plugin(item)
                            await manager._unbind_plugin(item.name, item.module_path)
                        except Exception:
                            logger.exception("Failed to clean up loaded plugin %s", directory)
                            job["warnings"].append("未能清理加载后的运行记录，请在官方 WebUI 检查。")
                if was_inactive:
                    current_inactive = await preferences.global_get(
                        "inactivated_plugins", []
                    )
                    if module_path not in current_inactive:
                        await preferences.global_put(
                            "inactivated_plugins", [*current_inactive, module_path]
                        )
                message = "加载未完成：" + redact(str(exc)) + "；请在官方 WebUI 核对插件状态。"
                job["results"].append(
                    {"directory": directory, "status": "failed", "message": message}
                )
                raise ValueError(message) from exc
            finally:
                self.transfers.in_critical = False
        job["results"].append(
            {"directory": directory, "status": "enabled", "message": "已加载并启用"}
        )
        for action, warning in (
            (self.operations.web.sync_skills_after_plugin_change, "技能同步失败"),
            (self.sources.inventory, "来源记录刷新失败"),
        ):
            try:
                await action()
            except Exception:
                logger.exception("Post-load refresh failed for %s: %s", directory, warning)
                job["warnings"].append("插件已加载并启用，但" + warning + "，请刷新页面或检查日志。")
