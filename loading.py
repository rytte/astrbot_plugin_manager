"""Explicitly load one unregistered local plugin through AstrBot's lifecycle."""

from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext
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
        self.task = None
        self.closing = False
        self.in_critical = False

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

    async def load(self, directory: str, revision: str, acknowledged: bool) -> dict:
        self.operations.ensure_writable()
        if acknowledged is not True:
            raise ValueError("请确认加载并启用插件。")
        if not isinstance(revision, str) or not revision:
            raise ValueError("缺少有效的插件状态，请刷新列表。")
        if self.closing:
            raise ValueError("插件管家正在关闭。")
        self.transfers._ensure_idle()
        async with self.transfers.operation_lock:
            self.transfers._ensure_idle()
            await self._pending(directory, revision)
            if self.closing:
                raise ValueError("插件管家正在关闭。")
            self.transfers._ensure_idle()
            self.transfers.loading_plugin = True
            task = self.task = asyncio.create_task(self._run(directory, revision))
            task.add_done_callback(self._finished)
        # Finish plugin initialization even if the HTTP caller disconnects.
        return await asyncio.shield(task)

    def _finished(self, task: asyncio.Task) -> None:
        self.transfers.loading_plugin = False
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.warning("Pending plugin load failed: %s", redact(str(error)))

    async def _run(self, directory: str, revision: str) -> dict:
        async with self.transfers.operation_lock:
            if self.closing or self.transfers.closing:
                raise ValueError("插件管家正在关闭。")
            self.operations.ensure_writable()
            return await self._load(directory, revision)

    async def load_restored(self, directory: str, *, enabled: bool) -> dict:
        """Load a restored plugin while the transfer and core locks are held."""
        record = await self.sources.detail(directory)
        return await self._load(
            directory, record["revision"], locked=True, enabled=enabled
        )

    async def _load(
        self, directory: str, revision: str, *, locked=False, enabled=True
    ) -> dict:
        manager = self.operations.manager
        warnings = []
        async with nullcontext() if locked else manager._pm_lock:
            source = await self._pending(directory, revision)
            module_path = source["module_path"]
            preferences = self.transfers.preferences
            inactive = await preferences.global_get("inactivated_plugins", [])
            was_inactive = module_path in inactive
            self.in_critical = True
            try:
                await asyncio.to_thread(
                    clear_plugin_bytecode, str(source["code"].parent), directory
                )
                if enabled and was_inactive:
                    await preferences.global_put(
                        "inactivated_plugins",
                        [item for item in inactive if item != module_path],
                    )
                elif not enabled and not was_inactive:
                    await preferences.global_put(
                        "inactivated_plugins", [*inactive, module_path]
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
                    or registered[0].activated != enabled
                    or (enabled and registered[0].star_cls is None)
                ):
                    raise ValueError(
                        "AstrBot 未注册并初始化预期插件，请在官方 WebUI 检查。"
                    )
            except Exception as exc:
                for item in list(self.operations.context.get_all_stars()):
                    if item.root_dir_name == directory:
                        try:
                            await manager._terminate_plugin(item)
                            await manager._unbind_plugin(item.name, item.module_path)
                        except Exception:
                            logger.exception(
                                "Failed to clean up loaded plugin %s", directory
                            )
                            warnings.append(
                                "未能清理加载后的运行记录，请在官方 WebUI 检查。"
                            )
                current_inactive = await preferences.global_get(
                    "inactivated_plugins", []
                )
                current_inactive = [
                    item for item in current_inactive if item != module_path
                ]
                if was_inactive:
                    current_inactive.append(module_path)
                await preferences.global_put("inactivated_plugins", current_inactive)
                message = (
                    "加载未完成："
                    + redact(str(exc))
                    + "；请在官方 WebUI 核对插件状态。"
                )
                if warnings:
                    message += " " + " ".join(warnings)
                raise ValueError(message) from exc
            finally:
                self.in_critical = False
        for action, warning in (
            ()
            if locked
            else (
                (self.operations.web.sync_skills_after_plugin_change, "技能同步失败"),
                (self.sources.inventory, "来源记录刷新失败"),
            )
        ):
            try:
                await action()
            except Exception:
                logger.exception(
                    "Post-load refresh failed for %s: %s", directory, warning
                )
                warnings.append(
                    "插件已加载并启用，但" + warning + "，请刷新页面或检查日志。"
                )
        return {
            "directory": directory,
            "status": "enabled" if enabled else "disabled",
            "message": "已加载并启用" if enabled else "已加载，保持停用",
            "warnings": warnings,
        }

    async def close(self) -> None:
        """Cancel waiting loads, but allow plugin initialization to finish."""
        self.closing = True
        if self.task and not self.task.done():
            if not self.in_critical:
                self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
