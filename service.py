"""Resolve exact plugin identities and execute guarded lifecycle operations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from astrbot.api.star import Context
from astrbot.core import sp
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.star import StarMetadata
from astrbot.core.star.star_handler import star_handlers_registry
from astrbot.dashboard.services.plugin_service import PluginService

from .bytecode import clear_plugin_bytecode
from .catalog import command_entries, tool_entries
from .redaction import redact

PLUGIN_NAME_PREFIX = "astrbot_plugin_"
CATALOG_PAGE_SIZE = 20


@dataclass(frozen=True)
class PluginTarget:
    identifier: str
    name: str
    plugin: StarMetadata | None
    failure: dict | None
    state: tuple

    @property
    def identity(self) -> object:
        return self.plugin if self.plugin is not None else self.failure

    @property
    def reserved(self) -> bool:
        return (
            self.plugin.reserved
            if self.plugin is not None
            else bool(self.failure.get("reserved", False))
        )

    @property
    def directory(self) -> str:
        return self.plugin.root_dir_name if self.plugin is not None else self.name

    def same_as(self, other: PluginTarget) -> bool:
        """Compare the captured instance and state, not just its name.

        Args:
            other: A fresh resolution of the requested target.

        Returns:
            Whether both resolutions describe the unchanged target.
        """
        return (
            self.name == other.name
            and self.identity is other.identity
            and self.state == other.state
        )


class PluginOperations:
    def __init__(
        self, context: Context, owner_module: str, *, allow_prefix_omission: bool
    ) -> None:
        self.context = context
        self.owner_module = owner_module
        self.allow_prefix_omission = allow_prefix_omission
        self.manager = context._star_manager
        if self.manager is None:
            raise RuntimeError("插件管理器不可用，无法初始化插件管理命令。")
        required = (
            "_pm_lock",
            "plugin_store_path",
            "reserved_plugin_path",
            "plugin_config_path",
            "failed_plugin_dict",
            "_terminate_plugin",
            "_unbind_plugin",
            "load",
            "install_plugin",
            "uninstall_plugin",
            "uninstall_failed_plugin",
            "turn_off_plugin",
        )
        missing = [name for name in required if not hasattr(self.manager, name)]
        if missing:
            raise RuntimeError(
                "当前 AstrBot 缺少必要的插件管理接口：" + "、".join(missing)
            )
        self.web = PluginService(None, self.manager)

    def ensure_writable(self) -> None:
        """Reject state-changing operations in demo mode."""
        self.web._ensure_not_demo()

    def command_conflicts(self) -> list[str]:
        """Find active commands and aliases overlapping the owned namespace.

        Returns:
            Sorted command names and their owning module paths.
        """
        owners = {item.module_path: item for item in self.context.get_all_stars()}
        registrations = []
        for handler in star_handlers_registry:
            owner = owners.get(handler.handler_module_path)
            if not handler.enabled or (owner is not None and not owner.activated):
                continue
            for command_filter in handler.event_filters:
                if isinstance(command_filter, CommandFilter | CommandGroupFilter):
                    registrations.append(
                        (handler, command_filter.get_complete_command_names())
                    )
        own_names = [
            name
            for handler, names in registrations
            if handler.handler_module_path == self.owner_module
            for name in names
        ]
        conflicts = set()
        for handler, names in registrations:
            if handler.handler_module_path == self.owner_module:
                continue
            for name in names:
                if any(
                    name == own
                    or name.startswith(own + " ")
                    or own.startswith(name + " ")
                    for own in own_names
                ):
                    conflicts.add(f"{name}（{handler.handler_module_path}）")
        return sorted(conflicts)

    def resolve(self, identifier: str) -> PluginTarget:
        """Resolve exact names and the explicitly enabled prefix omission.

        Args:
            identifier: A metadata name or directory, optionally without its prefix.

        Returns:
            A snapshot bound to a registered instance or failed-plugin record.

        Raises:
            ValueError: The target is missing, ambiguous or incomplete.
            RuntimeError: The core exposes an invalid failed-plugin record.
        """
        identifiers = {identifier}
        if self.allow_prefix_omission and not identifier.startswith(PLUGIN_NAME_PREFIX):
            identifiers.add(PLUGIN_NAME_PREFIX + identifier)
        matches = []
        for plugin in self.context.get_all_stars():
            if plugin.name in identifiers or plugin.root_dir_name in identifiers:
                if not plugin.name:
                    raise ValueError("插件元数据缺少名称，请在 WebUI 中检查。")
                state = (
                    plugin.name,
                    plugin.root_dir_name,
                    plugin.module_path,
                    plugin.activated,
                    plugin.reserved,
                    id(plugin.star_cls),
                )
                matches.append(
                    PluginTarget(identifier, plugin.name, plugin, None, state)
                )
        for directory, record in self.manager.failed_plugin_dict.items():
            if directory not in identifiers:
                continue
            if not isinstance(record, dict):
                raise RuntimeError("加载失败插件记录格式无效，必须是字典。")
            state = tuple((key, repr(value)) for key, value in sorted(record.items()))
            matches.append(PluginTarget(identifier, directory, None, record, state))
        if not matches:
            raise ValueError(f"未找到插件 {identifier}。")
        if len(matches) != 1:
            choices = "、".join(
                f"{item.name}（目录：{item.directory}）" for item in matches
            )
            raise ValueError(
                f"插件标识 {identifier} 存在歧义：{choices}。请使用无歧义的名称或目录名。"
            )
        return matches[0]

    def revalidate(self, target: PluginTarget) -> None:
        """Reject an identity or state change since the initial resolution.

        Args:
            target: The captured target of a pending operation.

        Raises:
            ValueError: The target disappeared or changed while waiting.
        """
        try:
            current = self.resolve(target.identifier)
        except ValueError as exc:
            raise ValueError("插件状态已变化，未执行操作，请重新发送命令。") from exc
        if not target.same_as(current):
            raise ValueError("插件状态已变化，未执行操作，请重新发送命令。")

    def check_operation(self, target: PluginTarget, action: str) -> None:
        """Validate the operation-specific state and protection rules.

        Args:
            target: An exact plugin identity.
            action: The requested mutation subcommand.

        Raises:
            ValueError: The requested operation is unsafe or unsupported.
        """
        if action == "remove":
            if target.reserved:
                raise ValueError("该插件是 AstrBot 保留插件，无法卸载。")
        elif target.plugin is None:
            raise ValueError(
                "该插件加载失败，请通过 WebUI 恢复；聊天命令仅支持卸载加载失败插件。"
            )
        plugin = target.plugin
        if plugin is not None:
            if plugin.module_path == self.owner_module and action in {
                "disable",
                "remove",
            }:
                raise ValueError("不能通过插件管家停用或卸载自身，请使用 WebUI。")
            if (
                not plugin.module_path
                or not plugin.root_dir_name
                or (plugin.activated and plugin.star_cls is None)
            ):
                raise ValueError("插件加载状态不完整，请在 WebUI 中检查。")
            if action == "reload" and not plugin.activated:
                raise ValueError("该插件已停用，请先启用插件，再执行重载命令。")

    def list_plugins(self) -> str:
        """List all registered and failed plugins.

        Returns:
            Plain text containing state, names and directory identifiers.
        """
        entries = []
        for plugin in self.context.get_all_stars():
            status = "✅" if plugin.activated else "⏸️"
            reserved = " [保留插件]" if plugin.reserved else ""
            entries.append(
                (
                    plugin.name or plugin.root_dir_name or "无名称",
                    f"{status}{reserved} {plugin.name}\n  目录：{plugin.root_dir_name}；版本：{plugin.version or '未声明'}\n  {plugin.desc or '无说明'}",
                )
            )
        for directory in self.manager.failed_plugin_dict:
            entries.append(
                (
                    directory,
                    f"❌ {directory}\n  请通过 WebUI 查看错误或恢复；可使用 remove 卸载。",
                )
            )
        entries.sort(key=lambda item: item[0])
        if not entries:
            return "没有已注册或加载失败的插件。"
        return (
            f"插件列表（共 {len(entries)} 个）\n"
            "状态：✅ 已启用 · ⏸️ 已停用 · ❌ 加载失败\n\n"
            + "\n\n".join(text for _, text in entries)
            + "\n\n使用 /pm info <插件名或目录名> 查看详情。"
        )

    def list_catalog(self, kind: str, page: int) -> str:
        """Page through registered commands or model tools without invoking them."""
        if kind not in {"cmds", "tools"}:
            raise ValueError("未知目录类型。")
        if page < 1:
            raise ValueError("页码必须为正整数。")
        plugins = self.context.get_all_stars()
        entries = (
            command_entries(plugins)
            if kind == "cmds"
            else tool_entries(self.context.get_llm_tool_manager(), plugins)
        )
        title = "命令目录" if kind == "cmds" else "模型工具目录"
        pages = max(1, (len(entries) + CATALOG_PAGE_SIZE - 1) // CATALOG_PAGE_SIZE)
        if page > pages:
            raise ValueError(f"页码超出范围，共 {pages} 页。")
        selected = entries[(page - 1) * CATALOG_PAGE_SIZE : page * CATALOG_PAGE_SIZE]
        parts = [
            f"{title}｜共 {len(entries)} 项｜第 {page}/{pages} 页",
            "状态来自全局注册信息；会话权限、其他过滤条件及实际请求仍需另行判定。",
            *(entry.render() for entry in selected),
        ]
        if not selected:
            parts.append("当前没有已注册的条目。")
        if page < pages:
            parts.append(f"下一页：/pm {kind} {page + 1}")
        return "\n\n".join(parts)

    def plugin_info(self, identifier: str) -> str:
        """Describe a resolved plugin and the commands and tools it owns."""
        target = self.resolve(identifier)
        if target.plugin is None:
            return f"插件目录：{target.name}\n状态：❌ 加载失败\n请通过 WebUI 查看错误详情或恢复；可使用 /pm remove {target.name} 卸载。"
        plugin = target.plugin
        plugins = self.context.get_all_stars()
        commands = [
            entry for entry in command_entries(plugins) if entry.owner is plugin
        ]
        tools = [
            entry
            for entry in tool_entries(self.context.get_llm_tool_manager(), plugins)
            if entry.owner is plugin
        ]
        parts = [
            f"插件：{plugin.name}",
            f"目录：{plugin.root_dir_name}",
            f"状态：{'✅ 已启用' if plugin.activated else '⏸️ 已停用'}",
            f"版本：{plugin.version or '未声明'}",
            f"作者：{plugin.author or '未声明'}",
            f"说明：{plugin.desc or '无说明'}",
            f"仓库：{plugin.repo or '未声明'}",
            "",
            "已注册指令：",
        ]
        parts.extend(entry.render(compact=True) for entry in commands)
        if not commands:
            parts.append("无已注册指令。")
        parts.extend(("", "模型工具："))
        parts.extend(entry.render(compact=True) for entry in tools)
        if not tools:
            parts.append(
                "当前没有已注册的模型工具。"
                if plugin.activated
                else "插件已停用；当前没有可用的模型工具注册信息。"
            )
        parts.append(
            "\n触发前缀以 AstrBot 配置为准；工具会话可用性按请求决定；更多说明请查看插件仓库 README。"
        )
        return redact("\n".join(parts))

    def validated_directory(self, target: PluginTarget) -> Path:
        """Verify that destructive operations address a direct storage child.

        Args:
            target: The exact registered or failed plugin identity.

        Returns:
            The absolute plugin directory, without following a linked root.

        Raises:
            ValueError: The directory is missing from metadata or escapes storage.
        """
        store = Path(
            self.manager.reserved_plugin_path
            if target.reserved
            else self.manager.plugin_store_path
        ).resolve()
        directory = target.directory
        if not directory:
            raise ValueError("插件元数据缺少目录名，拒绝操作。")
        candidate = store / directory
        if (
            candidate.name != directory
            or candidate.parent != store
            or candidate.resolve() != candidate
        ):
            raise ValueError("插件目录存在路径越界或链接，拒绝操作。")
        return candidate

    async def install(self, repository: str) -> str:
        """Install through the WebUI service without bypassing version checks.

        Args:
            repository: The explicitly supplied repository address.

        Returns:
            A completion message identifying the registered plugin.

        Raises:
            RuntimeError: The service did not register a complete plugin.
        """
        self.ensure_writable()
        info, _ = await self.web.install_plugin(
            {"url": repository, "ignore_version_check": False}
        )
        name = info.get("name")
        if not name:
            raise RuntimeError("安装服务未返回已注册插件名称。")
        target = self.resolve(name)
        if target.plugin is None or (
            target.plugin.activated and target.plugin.star_cls is None
        ):
            raise RuntimeError("安装后插件未完成注册和初始化。")
        return f"插件 {target.name} 已安装（{'已启用' if target.plugin.activated else '已停用'}）。"

    async def disable(self, target: PluginTarget) -> str:
        """Disable using the core service and verify the resulting state.

        Args:
            target: The captured plugin instance.

        Returns:
            A completion message only when the target is actually disabled.

        Raises:
            RuntimeError: The requested state was not reached.
        """
        self.ensure_writable()
        self.revalidate(target)
        self.check_operation(target, "disable")
        self.validated_directory(target)
        await self.web.set_plugin_enabled({"name": target.name}, enabled=False)
        current = self.resolve(target.identifier)
        if current.plugin is not target.plugin or current.plugin.activated:
            raise RuntimeError("插件停用后状态与目标不一致。")
        return f"插件 {target.name} 已停用。"

    async def reload(self, target: PluginTarget, *, enable: bool = False) -> str:
        """Reload or enable exactly one verified module under the core lock.

        Args:
            target: The captured registered plugin and its lifecycle state.
            enable: Whether to remove this module's disabled preference first.

        Returns:
            A completion message after registering a fresh active instance.

        Raises:
            ValueError: The target changed while waiting for the manager lock.
            RuntimeError: Cache cleanup, termination or targeted loading failed.
        """
        self.ensure_writable()
        action = "enable" if enable else "reload"
        self.check_operation(target, action)
        module_path = target.plugin.module_path
        previous_instance = target.plugin.star_cls
        async with self.manager._pm_lock:
            self.revalidate(target)
            self.check_operation(target, action)
            directory = self.validated_directory(target)
            try:
                await asyncio.to_thread(
                    clear_plugin_bytecode, str(directory.parent), directory.name
                )
                await self.manager._terminate_plugin(target.plugin)
                if enable:
                    inactive = await sp.global_get("inactivated_plugins", [])
                    await sp.global_put(
                        "inactivated_plugins",
                        [item for item in inactive if item != module_path],
                    )
                await self.manager._unbind_plugin(target.name, module_path)
                success, error = await self.manager.load(
                    specified_module_path=module_path
                )
                if not success:
                    raise RuntimeError(error or "插件定向加载失败。")
                registered = [
                    item
                    for item in self.context.get_all_stars()
                    if item.module_path == module_path
                ]
                if len(registered) != 1:
                    raise RuntimeError("主程序未唯一注册目标模块。")
                current = registered[0]
                if (
                    not current.name
                    or not current.activated
                    or current.star_cls is None
                    or current.star_cls is previous_instance
                ):
                    raise RuntimeError("主程序未注册并初始化新的插件实例。")
            except ValueError as exc:
                raise RuntimeError("插件生命周期操作失败。") from exc
        await self.web.sync_skills_after_plugin_change()
        return f"插件 {current.name} 已{'启用' if enable else '重载'}。"

    async def uninstall(self, target: PluginTarget, delete_all: bool) -> str:
        """Uninstall through the WebUI service and report incomplete cleanup.

        Args:
            target: The instance bound to the administrator's confirmation.
            delete_all: Whether to delete configuration, data and plugin KV state.

        Returns:
            A complete or explicitly partial outcome, never assumed success.

        Raises:
            ValueError: Identity, protection or filesystem checks failed.
        """
        self.ensure_writable()
        self.revalidate(target)
        self.check_operation(target, "remove")
        directory = self.validated_directory(target)
        artifacts = []
        plugin_id = (
            target.plugin.plugin_id
            if target.plugin is not None
            else target.failure.get("plugin_id")
        )
        if delete_all:
            config_store = await asyncio.to_thread(
                Path(self.manager.plugin_config_path).resolve
            )
            data_store = (
                await asyncio.to_thread(Path(self.manager.plugin_store_path).resolve)
            ).parent
            artifacts = [
                config_store / f"{target.directory}_config.json",
                data_store / "plugin_data" / target.directory,
                data_store / "plugins_data" / target.directory,
            ]
            if any(candidate.resolve() != candidate for candidate in artifacts):
                raise ValueError("插件配置或数据路径存在链接，拒绝彻底删除。")
        payload = {"delete_config": delete_all, "delete_data": delete_all}
        if target.plugin is None:
            await self.web.uninstall_failed_plugin({"dir_name": target.name, **payload})
        else:
            await self.web.uninstall_plugin({"name": target.name, **payload})
        remaining = []
        if directory.exists():
            remaining.append("插件目录")
        if target.directory in self.manager.failed_plugin_dict or any(
            item.root_dir_name == target.directory
            for item in self.context.get_all_stars()
        ):
            remaining.append("插件注册记录")
        if any(candidate.exists() for candidate in artifacts):
            remaining.append("配置或持久化数据文件")
        if delete_all and plugin_id and await sp.range_get_async("plugin", plugin_id):
            remaining.append("插件 KV 数据")
        if remaining:
            return f"插件 {target.name} 的卸载服务已执行，但未完全清理：{'、'.join(remaining)}。请在 WebUI 和日志中检查，不要视为彻底删除成功。"
        scope = "配置和数据已删除。" if delete_all else "配置和数据保留。"
        return f"插件 {target.name} 已卸载；{scope}"
