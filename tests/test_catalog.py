"""Verify plugin info catalogs, ownership and read-only query behavior."""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import mcp.types
import pytest
from astrbot.core.agent.agent import Agent
from astrbot.core.agent.handoff import HandoffTool
from astrbot.core.agent.mcp_client import MCPTool
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.provider.func_tool_manager import FunctionToolManager
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
from astrbot.core.star.star import StarMetadata
from astrbot.core.star.star_handler import EventType, StarHandlerMetadata


def output(event):
    return "\n".join(event.sent)


def add_command(env, plugin, name, module=None):
    async def action(self, event, count: int):
        raise AssertionError("Catalogs must not invoke commands")

    handler = StarHandlerMetadata(
        EventType.AdapterMessageEvent,
        name,
        name,
        module or plugin.module_path,
        action,
        [],
        desc="Example",
    )
    handler.event_filters = [CommandFilter(name, None, handler)]
    env.registry.append(handler)
    return handler


async def test_groups_inherit_custom_admin_filter_aliases_and_disabled_state(env):
    target = env.add_target(reserved=True)
    parent = add_command(env, target, "parent")
    group = CommandGroupFilter("manage", alias={"m"})
    group.custom_filter_list = [PermissionTypeFilter(PermissionType.ADMIN)]
    parent.event_filters = [group]
    parent.enabled = False
    child = add_command(env, target, "child")
    child.event_filters = [
        CommandFilter("echo", {"say"}, child, group.get_complete_command_names())
    ]
    entry = next(
        entry
        for entry in env.catalog.command_entries(env.context.get_all_stars())
        if entry.name == "manage echo"
    )
    assert entry.owner is target and entry.source == "内置"
    assert "已停用" in entry.status
    assert (
        "管理员" in entry.details
        and "m say" in entry.details
        and "count" in entry.details
    )
    event = env.event(f"/pm info {target.name}")
    await env.scheduler.execute(event)
    assert "manage echo（管理员；已停用" in output(event)
    assert "m say" in output(event) and not env.model_calls


async def test_plugin_info_filters_by_identity_instead_of_display_name(env):
    first = env.add_target(name="shared", directory="first")
    second = env.add_target(name="shared", directory="second")
    add_command(env, first, "first_command", module="data.plugins.first.commands")
    add_command(env, second, "second_command")
    env.tools.func_list = [
        FunctionTool(
            name="first_tool",
            description="first",
            parameters={},
            handler_module_path="data.plugins.first.tools",
        ),
        FunctionTool(
            name="second_tool",
            description="second",
            parameters={},
            handler_module_path=second.module_path,
        ),
    ]
    event = env.event("/pm info first")
    await env.scheduler.execute(event)
    assert "first_command" in output(event) and "first_tool" in output(event)
    assert "second_command" not in output(event) and "second_tool" not in output(event)
    assert not env.model_calls


async def test_info_keeps_agent_children_with_their_true_owners(env):
    parent_plugin = env.add_target(name="parent", directory="parent")
    child_plugin = env.add_target(name="child", directory="child")
    borrowed = FunctionTool(
        name="borrowed_tool",
        description="borrowed",
        parameters={},
        handler_module_path=child_plugin.module_path,
    )
    owned = FunctionTool(
        name="owned_tool",
        description="owned",
        parameters={},
        handler_module_path=parent_plugin.module_path,
    )
    client = SimpleNamespace(call_tool_with_reconnect=AsyncMock())
    remote = MCPTool(
        mcp.types.Tool(name="remote_tool", inputSchema={}), client, "remote_server"
    )
    handoff = HandoffTool(
        Agent(name="helper", tools=[borrowed, owned, remote]),
        handler_module_path=parent_plugin.module_path,
    )
    env.tools.func_list = [handoff]
    parent = env.event("/pm info parent")
    await env.scheduler.execute(parent)
    assert "transfer_to_helper" in output(parent) and "owned_tool" in output(parent)
    assert "borrowed_tool" not in output(parent) and "remote_tool" not in output(parent)
    child = env.event("/pm info child")
    await env.scheduler.execute(child)
    assert "borrowed_tool" in output(child) and "所属 Agent 入口" in output(child)
    assert "owned_tool" not in output(child)
    client.call_tool_with_reconnect.assert_not_awaited()
    assert not env.model_calls


@pytest.mark.parametrize(
    "plugin_active,tool_active,disabled",
    [
        (True, True, False),
        (False, True, True),
        (True, False, True),
        (False, False, True),
    ],
)
async def test_info_combines_plugin_and_tool_state_with_compact_parameters(
    env, plugin_active, tool_active, disabled
):
    target = env.add_target(name="astrbot_plugin_example", active=plugin_active)
    tool = FunctionTool(
        name="lookup",
        description="Look up a result.\nDetailed description.",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        handler_module_path=target.module_path,
        active=tool_active,
    )
    env.tools.func_list = [tool]
    event = env.event("/pm info example")
    await env.scheduler.execute(event)
    assert "模型工具：" in output(event) and "lookup" in output(event)
    assert "参数：query" in output(event) and '"properties"' not in output(event)
    assert "Detailed description" not in output(event)
    assert f"lookup（工具；{'已停用' if disabled else '全局启用'}）" in output(event)
    assert tool.active is tool_active and target.activated is plugin_active
    assert not env.model_calls


@pytest.mark.parametrize("active", [True, False])
async def test_info_without_tool_metadata_is_explicit(env, active):
    target = env.add_target(active=active)
    event = env.event(f"/pm info {target.name}")
    await env.scheduler.execute(event)
    assert "模型工具：" in output(event)
    assert (
        "当前没有已注册的模型工具" if active else "没有可用的模型工具注册信息"
    ) in output(event)


async def test_info_sanitizes_metadata_without_changing_or_executing_tools(env):
    target = env.add_target()
    target.desc = "Plugin api_key=plugin-secret"
    callback = AsyncMock(side_effect=AssertionError("Unexpected tool call"))
    tool = FunctionTool(
        name="lookup",
        description='Visible password="tool-secret"',
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": '{"password":"parameter-secret"}',
                }
            },
        },
        handler=callback,
        handler_module_path=target.module_path,
    )
    env.tools.func_list = [tool]
    original = copy.deepcopy(tool.parameters)
    event = env.event()
    await env.invoke(event, f"info {target.name}")
    assert "lookup" in output(event) and "Visible" in output(event)
    assert "[已隐藏]" in output(event)
    assert all(
        secret not in output(event)
        for secret in ("plugin-secret", "tool-secret", "parameter-secret")
    )
    assert tool.parameters == original and tool.active is True
    callback.assert_not_awaited()
    env.preferences.global_put.assert_not_awaited()
    env.manager.load.assert_not_awaited()
    assert not env.model_calls


async def test_info_reflects_live_tool_state_without_caching(env):
    target = env.add_target()
    tool = FunctionTool(
        name="lookup",
        description="desc",
        parameters={},
        handler_module_path=target.module_path,
    )
    env.tools.func_list = [tool]
    first = env.event()
    await env.invoke(first, f"info {target.name}")
    assert "lookup（工具；全局启用）" in output(first)
    tool.active = False
    second = env.event()
    await env.invoke(second, f"info {target.name}")
    assert "lookup（工具；已停用）" in output(second)
    assert "lookup（工具；全局启用）" not in output(second)


@pytest.mark.parametrize("reader", ["command_entries", "tool_entries"])
async def test_info_failures_do_not_claim_partial_state_changes(env, monkeypatch, reader):
    target = env.add_target()
    monkeypatch.setattr(
        env.service_module,
        reader,
        Mock(side_effect=RuntimeError("broken registry")),
    )
    event = env.event()
    await env.invoke(event, f"info {target.name}")
    assert "查询失败，请检查 AstrBot 日志" in output(event)
    assert "部分变更" not in output(event)
    env.preferences.global_put.assert_not_awaited()
    assert event.is_stopped() and not env.model_calls


async def test_real_builtin_and_mcp_tools_are_listed_without_connections(env):
    manager = FunctionToolManager()
    client = SimpleNamespace(call_tool_with_reconnect=AsyncMock())
    remote = MCPTool(
        mcp.types.Tool(
            name="search.demo", description="MCP search", inputSchema={"type": "object"}
        ),
        client,
        "demo_server",
    )
    manager.func_list.append(remote)
    entries = env.catalog.tool_entries(manager, env.context.get_all_stars())
    assert any(entry.source == "内置" for entry in entries)
    entry = next(entry for entry in entries if entry.name == remote.name)
    assert (
        entry.source == "MCP" and entry.plugin == "demo_server" and entry.owner is None
    )
    assert manager.func_list == [remote]
    client.call_tool_with_reconnect.assert_not_awaited()


@pytest.mark.parametrize(
    "module,expected",
    [
        ("data.plugins.foo.main", "foo"),
        ("data.plugins.foo.main.nested", "foo"),
        ("data.plugins.foo.tools", "foo"),
        ("data.plugins.foo_extra.tools", "foo_extra"),
        ("data.plugins.foobar.tools", None),
        ("data.plugins.foo.tools.agent.actions", "nested"),
        (None, None),
    ],
)
def test_module_ownership_uses_the_longest_package_boundary(env, module, expected):
    plugins = tuple(
        StarMetadata(name=name, module_path=path)
        for name, path in [
            ("foo", "data.plugins.foo.main"),
            ("foo_extra", "data.plugins.foo_extra.main"),
            ("nested", "data.plugins.foo.tools.agent.main"),
        ]
    )
    owner = env.catalog.owner_for_module(module, plugins)
    assert (owner.name if owner else None) == expected


def test_ambiguous_module_ownership_is_not_guessed(env):
    plugins = tuple(
        StarMetadata(name=name, module_path=f"data.plugins.shared.{name}")
        for name in ("first", "second")
    )
    assert env.catalog.owner_for_module("data.plugins.shared.tools", plugins) is None
    assert (
        env.catalog.owner_for_module("data.plugins.shared.first", plugins) is plugins[0]
    )
