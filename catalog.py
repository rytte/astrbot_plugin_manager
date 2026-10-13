"""Inspect runtime command and tool registrations without executing them."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from functools import partial

from astrbot.core.agent.handoff import HandoffTool
from astrbot.core.agent.mcp_client import MCPTool
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.provider.func_tool_manager import FunctionToolManager
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
from astrbot.core.star.star import StarMetadata
from astrbot.core.star.star_handler import EventType, star_handlers_registry

from .redaction import redact


@dataclass(frozen=True)
class Entry:
    """A registration snapshot with explicit ownership and a display summary."""

    kind: str
    name: str
    plugin: str
    source: str
    status: str
    details: str
    owner: StarMetadata | None
    summary: str

    def render(self) -> str:
        """Render a sanitized summary without evaluating runtime permissions."""
        return redact(self.summary)


def owner_for_module(
    module: str | None, plugins: tuple[StarMetadata, ...]
) -> StarMetadata | None:
    """Resolve entry modules and sibling modules by the longest package boundary."""
    if not module:
        return None
    exact = [plugin for plugin in plugins if plugin.module_path == module]
    if exact:
        return exact[0] if len(exact) == 1 else None
    matches = []
    for plugin in plugins:
        path = plugin.module_path
        if not path:
            continue
        package = path.rpartition(".")[0]
        prefixes = [path]
        if package and package not in {"astrbot.builtin_stars", "data.plugins"}:
            prefixes.append(package)
        lengths = [
            len(prefix)
            for prefix in prefixes
            if module == prefix or module.startswith(prefix + ".")
        ]
        if lengths:
            matches.append((max(lengths), plugin))
    if not matches:
        return None
    longest = max(length for length, _ in matches)
    owners = [plugin for length, plugin in matches if length == longest]
    return owners[0] if len(owners) == 1 else None


def command_entries(plugins: Iterable[StarMetadata]) -> list[Entry]:
    """Collect commands, aliases and inherited group restrictions."""
    owners = tuple(plugins)
    handlers = [
        handler
        for handler in star_handlers_registry
        if handler.event_type == EventType.AdapterMessageEvent
    ]
    groups = [
        (handler, command_filter)
        for handler in handlers
        for command_filter in handler.event_filters
        if isinstance(command_filter, CommandGroupFilter)
    ]
    entries = []
    for handler in handlers:
        owner = owner_for_module(handler.handler_module_path, owners)
        for command_filter in handler.event_filters:
            if not isinstance(command_filter, CommandFilter | CommandGroupFilter):
                continue
            names = command_filter.get_complete_command_names()
            canonical = names[0]
            ancestors = [
                (parent, group)
                for parent, group in groups
                if parent.handler_module_path == handler.handler_module_path
                and any(
                    canonical.startswith(name + " ")
                    for name in group.get_complete_command_names()
                )
            ]
            filters = [*handler.event_filters, *command_filter.custom_filter_list]
            for parent, group in ancestors:
                filters.extend(parent.event_filters)
                filters.extend(group.custom_filter_list)
            admin = any(
                isinstance(item, PermissionTypeFilter)
                and item.permission_type == PermissionType.ADMIN
                for item in filters
            )
            enabled = bool(owner and owner.activated and handler.enabled)
            enabled = enabled and all(parent.enabled for parent, _ in ancestors)
            is_group = isinstance(command_filter, CommandGroupFilter)
            usage = "指令组" if is_group else command_filter.print_types()
            description = (
                handler.desc.strip().splitlines()[0]
                if handler.desc and handler.desc.strip()
                else "无说明"
            )
            permission = "管理员" if admin else "按插件权限过滤器判断"
            aliases = sorted(set(names[1:]))
            details = [
                f"权限：{permission}（其他过滤条件仍生效）",
                f"用法：{canonical}" + (f" {usage}" if usage else ""),
                f"说明：{description}",
            ]
            if aliases:
                details.append("别名：" + "、".join(aliases))
            status = "全局启用" if enabled else ("已停用" if owner else "来源未加载")
            summary_state = "；" + status if not enabled else ""
            summary_aliases = "；别名：" + "、".join(aliases) if aliases else ""
            entries.append(
                Entry(
                    kind="命令组" if is_group else "命令",
                    name=canonical,
                    plugin=owner.name
                    if owner and owner.name
                    else handler.handler_module_path,
                    source="内置" if owner and owner.reserved else "插件",
                    status=status,
                    details="\n".join(details),
                    owner=owner,
                    summary=f"- {canonical}（{permission}{summary_state}{summary_aliases}）：{description}",
                )
            )
    return sorted(entries, key=lambda item: (item.plugin, item.name))


def tool_entries(
    manager: FunctionToolManager, plugins: Iterable[StarMetadata]
) -> list[Entry]:
    """Collect plugin, builtin, MCP and nested Agent tools by object identity."""
    owners = tuple(plugins)
    builtins = manager.iter_builtin_tools()
    builtin_ids = {id(tool) for tool in builtins}
    tools = {}
    parents = defaultdict(set)
    unresolved = defaultdict(set)
    pending = [*manager.func_list, *builtins]
    for tool in pending:
        if id(tool) in tools:
            continue
        tools[id(tool)] = tool
        if isinstance(tool, HandoffTool):
            for child in tool.agent.tools or []:
                if isinstance(child, str):
                    reference = child
                    child = manager.get_func(reference)
                    if child is None:
                        unresolved[id(tool)].add(reference)
                        continue
                if not isinstance(child, FunctionTool):
                    raise ValueError(
                        f"Agent {tool.agent.name} 含有无效的工具注册信息。"
                    )
                parents[id(child)].add(f"{tool.agent.name}（{tool.name}）")
                pending.append(child)

    entries = []
    for tool in tools.values():
        owner = None
        if id(tool) in builtin_ids:
            source, plugin = "内置", "AstrBot"
        elif isinstance(tool, MCPTool):
            source, plugin = "MCP", tool.mcp_server_name
        else:
            module = tool.handler_module_path
            if not module and tool.handler is not None:
                callback = tool.handler
                while isinstance(callback, partial):
                    callback = callback.func
                module = getattr(callback, "__module__", None)
            owner = owner_for_module(module, owners)
            source = (
                "内置" if owner and owner.reserved else ("插件" if owner else "未识别")
            )
            plugin = owner.name if owner and owner.name else (module or "未识别来源")
        active = tool.active and (owner is None or owner.activated)
        description = tool.description or "无说明"
        parameters = json.dumps(tool.parameters, ensure_ascii=False)
        parameter_names = (
            "、".join(sorted(tool.parameters.get("properties", {}))) or "未声明命名参数"
        )
        details = []
        if isinstance(tool, HandoffTool):
            details.append(f"Agent：{tool.agent.name}")
            if tool.agent.tools is None:
                details.append("Agent 工具范围：按请求决定（未指定工具列表）")
            if unresolved[id(tool)]:
                details.append(
                    "工具引用信息不可用（按请求决定）："
                    + "、".join(sorted(unresolved[id(tool)]))
                )
        if parents[id(tool)]:
            details.append("所属 Agent 入口：" + "、".join(sorted(parents[id(tool)])))
        kind = "Agent 入口" if isinstance(tool, HandoffTool) else "工具"
        status = "全局启用；会话可用性按请求决定" if active else "已停用"
        summary = [
            f"- {tool.name}（{kind}；{'全局启用' if active else '已停用'}）：{description.strip().splitlines()[0] if description.strip() else '无说明'}",
            f"  参数：{parameter_names}",
            *("  " + detail for detail in details),
        ]
        details.extend((f"说明：{description}", f"参数：{parameters}"))
        entries.append(
            Entry(
                kind=kind,
                name=tool.name,
                plugin=plugin,
                source=source,
                status=status,
                details="\n".join(details),
                owner=owner,
                summary="\n".join(summary),
            )
        )
    return sorted(entries, key=lambda item: (item.source, item.plugin, item.name))
