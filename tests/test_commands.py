"""Verify command routing, permission checks, real names and namespace ownership."""

import copy
from unittest.mock import AsyncMock

import pytest
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.star import StarMetadata
from astrbot.core.star.star_handler import EventType, StarHandlerMetadata
from astrbot.dashboard.services.plugin_service import PluginService


def output(event):
    return "\n".join(event.sent)


async def test_pm_is_the_only_registered_entry_point(env):
    command_filters = [
        command_filter
        for handler in env.registry
        if handler.handler_module_path == env.main.__name__
        for command_filter in handler.event_filters
        if isinstance(command_filter, CommandFilter | CommandGroupFilter)
    ]
    assert len(command_filters) == 1
    assert set(command_filters[0].get_complete_command_names()) == {"pm"}


@pytest.mark.parametrize(
    "command",
    [
        "/pm",
        "/pm help",
        "/pm list",
        "/pm info demo",
        "/pm add https://example.com/repo",
        "/pm enable demo",
        "/pm disable demo",
        "/pm reload demo",
        "/pm remove demo",
    ],
)
async def test_all_commands_require_admin_in_real_pipeline(env, command):
    event = env.event(command, user="member", admin=False)
    await env.scheduler.execute(event)
    assert "权限" in output(event)
    env.manager.load.assert_not_awaited()
    env.manager.uninstall_plugin.assert_not_awaited()


@pytest.mark.parametrize(
    "arguments",
    [
        "",
        "list",
        "help",
        "info bad",
        "add bad",
        "enable bad",
        "disable bad",
        "reload bad",
        "remove bad",
    ],
)
async def test_direct_calls_cannot_bypass_role_restrictions(env, arguments):
    for event in (env.event(user="member", admin=False), env.event()):
        if event.is_admin():
            event.set_extra("_api_key_allow_admin_role", False)
        await env.invoke(event, arguments)
        assert "仅限" in output(event)
        assert event.is_stopped()


@pytest.mark.parametrize("command", ["/pm", "/pm help"])
async def test_bare_entry_shows_help_without_calling_model(env, command):
    event = env.event(command)
    await env.scheduler.execute(event)
    assert "/pm remove" in output(event)
    assert "/plugin" not in output(event)
    assert "插件管家" in output(event)
    assert "/pm list" in output(event).splitlines()
    assert "/pm help" in output(event).splitlines()
    assert "/pm info <插件名或目录名>" in output(event).splitlines()
    commands = {
        line.split()[1]
        for line in output(event).splitlines()
        if line.startswith("/pm ")
    }
    assert commands == {
        "list", "help", "info", "add", "enable", "disable", "reload", "remove"
    }
    assert event.is_stopped() and not env.model_calls


@pytest.mark.parametrize(
    "arguments",
    [
        "ls",
        "get https://github.com/owner/repo",
        "on demo_directory",
        "off demo_directory",
    ],
)
async def test_old_subcommand_names_are_rejected_without_side_effects(
    env, monkeypatch, arguments
):
    target = env.add_target()
    install = AsyncMock()
    monkeypatch.setattr(PluginService, "install_plugin", install)
    event = env.event(f"/pm {arguments}")
    await env.scheduler.execute(event)
    assert "未知子命令" in output(event)
    assert target.name not in output(event)
    install.assert_not_awaited()
    env.manager.load.assert_not_awaited()
    env.manager.turn_on_plugin.assert_not_awaited()
    env.manager.turn_off_plugin.assert_not_awaited()
    env.manager.uninstall_plugin.assert_not_awaited()
    env.preferences.global_put.assert_not_awaited()
    assert event.is_stopped() and not env.model_calls


@pytest.mark.parametrize(
    "arguments",
    [
        "install demo",
        "uninstall demo",
        "list 0",
        "list 1",
        "list 2",
        "list no",
        "list 1 extra",
        "info",
        "info demo extra",
        "add",
        "add url extra",
        "enable",
        "disable demo --force",
        "reload",
        "reload demo confirm",
        "remove demo --delete-config",
        "remove demo confirm --all",
    ],
)
async def test_invalid_and_unpromised_syntax_is_rejected(env, arguments):
    event = env.event()
    await env.invoke(event, arguments)
    assert any(word in output(event) for word in ("用法", "未知"))
    env.manager.load.assert_not_awaited()
    env.manager.uninstall_plugin.assert_not_awaited()


async def test_list_includes_all_disabled_and_failed_plugins_without_pagination(env):
    env.add_target(active=False)
    env.manager.failed_plugin_dict["broken_directory"] = {"reserved": False}
    for index in range(22):
        env.add_target(f"plugin_{index:02}", f"directory_{index:02}")
    event = env.event("/pm list")
    await env.scheduler.execute(event)
    listing = output(event)
    assert listing.startswith(
        "插件列表（共 25 个）\n"
        "状态：✅ 已启用 · ⏸️ 已停用 · ❌ 加载失败\n\n"
    )
    assert "✅ plugin_00" in listing.splitlines()
    assert "⏸️ demo_plugin" in listing.splitlines()
    assert "❌ broken_directory" in listing.splitlines()
    for status in ("已启用", "已停用", "加载失败"):
        assert listing.count(status) == 1
    for target in env.context.get_all_stars():
        assert f" {target.name}\n" in listing
        assert f"目录：{target.root_dir_name}" in listing
    assert "页" not in listing
    assert "/pm info <插件名或目录名>" in listing
    assert sum(bool(message) for message in event.sent) == 1
    assert event.is_stopped() and not env.model_calls


@pytest.mark.parametrize(
    "active,status", [(True, "✅"), (False, "⏸️")]
)
async def test_list_places_reserved_marker_after_status_emoji(env, active, status):
    env.add_target(name="astrbot", active=active, reserved=True)
    event = env.event("/pm list")
    await env.scheduler.execute(event)
    assert f"{status} [保留插件] astrbot" in output(event).splitlines()
    assert event.is_stopped() and not env.model_calls


async def test_list_reports_an_empty_registry(env):
    env.owners.clear()
    assert env.plugin.service.list_plugins() == "没有已注册或加载失败的插件。"


@pytest.mark.parametrize("identifier", ["demo_plugin", "demo_directory"])
async def test_help_with_a_plugin_name_is_rejected_with_info_usage(env, identifier):
    target = env.add_target()
    event = env.event(f"/pm help {identifier}")
    await env.scheduler.execute(event)
    assert "用法" in output(event)
    assert "/pm info <插件名或目录名>" in output(event)
    assert f"插件：{target.name}" not in output(event)
    assert event.is_stopped() and not env.model_calls


@pytest.mark.parametrize("identifier", ["demo_plugin", "demo_directory"])
async def test_info_uses_real_full_command_names_aliases_and_disabled_state(
    env, identifier
):
    target = env.add_target()
    handler = copy.copy(next(iter(env.registry)))
    handler.handler_module_path = target.module_path
    handler.handler_name = "child"
    handler.enabled = False
    command_filter = CommandFilter("renamed", None, handler, ["parent"])
    command_filter.alias = {"short"}
    handler.event_filters = [command_filter]
    env.registry.append(handler)
    event = env.event(f"/pm info {identifier}")
    await env.scheduler.execute(event)
    assert "parent renamed" in output(event)
    assert "parent short" in output(event)
    assert "已停用" in output(event)
    assert "Tester" in output(event)
    assert event.is_stopped() and not env.model_calls


@pytest.mark.parametrize(
    "active,status", [(True, "✅ 已启用"), (False, "⏸️ 已停用")]
)
async def test_info_reports_plugin_status_with_emoji(env, active, status):
    target = env.add_target(active=active)
    event = env.event(f"/pm info {target.name}")
    await env.scheduler.execute(event)
    assert f"状态：{status}\n" in output(event)
    assert event.is_stopped() and not env.model_calls


async def test_info_reports_failed_plugins_and_their_removal_command(env):
    env.manager.failed_plugin_dict["broken_directory"] = {"reserved": False}
    event = env.event("/pm info broken_directory")
    await env.scheduler.execute(event)
    assert "状态：❌ 加载失败\n" in output(event)
    assert "/pm remove broken_directory" in output(event)
    assert event.is_stopped() and not env.model_calls


@pytest.mark.parametrize("action", ["info", "reload"])
async def test_ambiguous_identifiers_are_rejected_instead_of_first_match(env, action):
    env.add_target(name="collision", directory="first")
    env.add_target(name="second", directory="collision")
    event = env.event()
    await env.invoke(event, f"{action} collision")
    assert "歧义" in output(event) and "first" in output(event)
    env.manager.load.assert_not_awaited()


@pytest.mark.parametrize("kind", ["command", "group", "alias", "child"])
async def test_conflicting_active_namespace_fails_initialization_and_runtime(env, kind):
    def foreign_handler(event):
        raise AssertionError("A conflicting handler must not run")

    handler = StarHandlerMetadata(
        EventType.AdapterMessageEvent,
        "foreign",
        "foreign",
        "foreign.main",
        foreign_handler,
        [],
    )
    if kind == "group":
        handler.event_filters = [CommandGroupFilter("pm")]
    else:
        command_filter = CommandFilter(
            "other"
            if kind == "alias"
            else "pm reload"
            if kind == "child"
            else "pm",
            None,
            handler,
        )
        if kind == "alias":
            command_filter.alias = {"pm"}
        handler.event_filters = [command_filter]
    env.registry.append(handler)
    env.owners["foreign.main"] = StarMetadata(name="foreign", activated=True)
    with pytest.raises(RuntimeError, match="命令冲突"):
        await env.plugin.initialize()
    event = env.event()
    await env.invoke(event, "list")
    assert "命令冲突" in output(event)


@pytest.mark.parametrize("command", ["plugin", "plugin reload", "pm_tools"])
async def test_unrelated_commands_do_not_conflict_with_pm(env, command):
    handler = copy.copy(next(iter(env.registry)))
    handler.handler_module_path = "foreign.main"
    handler.event_filters = [CommandFilter(command, None, handler)]
    env.registry.append(handler)
    env.owners["foreign.main"] = StarMetadata(
        name="foreign", module_path="foreign.main", activated=True
    )
    await env.plugin.initialize()
    assert env.plugin.service.command_conflicts() == []
    event = env.event("/pm list")
    await env.scheduler.execute(event)
    assert "插件列表（共 2 个）" in output(event)
    assert event.is_stopped() and not env.model_calls


@pytest.mark.parametrize(
    "disabled_handler,disabled_plugin", [(True, False), (False, True)]
)
async def test_disabled_foreign_commands_do_not_block_namespace(
    env, disabled_handler, disabled_plugin
):
    handler = copy.copy(next(iter(env.registry)))
    handler.handler_module_path = "foreign.main"
    handler.enabled = not disabled_handler
    env.registry.append(handler)
    env.owners["foreign.main"] = StarMetadata(
        name="foreign", module_path="foreign.main", activated=not disabled_plugin
    )
    await env.plugin.initialize()


async def test_install_uses_webui_service_and_retains_version_validation(
    env, monkeypatch
):
    target = env.add_target()
    install = AsyncMock(return_value=({"name": target.name}, "安装成功"))
    monkeypatch.setattr(PluginService, "install_plugin", install)
    event = env.event("/pm add https://github.com/owner/repo")
    await env.scheduler.execute(event)
    install.assert_awaited_once_with(
        {"url": "https://github.com/owner/repo", "ignore_version_check": False}
    )
    assert "已安装" in output(event)
    assert event.is_stopped() and not env.model_calls


@pytest.mark.parametrize(
    "arguments",
    ["add url", "enable demo", "disable demo", "reload demo", "remove demo"],
)
async def test_demo_mode_forbids_every_mutation(env, monkeypatch, arguments):
    monkeypatch.setattr("astrbot.dashboard.services.plugin_service.DEMO_MODE", True)
    event = env.event()
    await env.invoke(event, arguments)
    assert "demo mode" in output(event)
    env.manager.load.assert_not_awaited()
    env.manager.uninstall_plugin.assert_not_awaited()


async def test_missing_manager_and_missing_core_interfaces_fail_fast(env):
    env.context._star_manager = None
    with pytest.raises(RuntimeError, match="不可用"):
        env.main.Main(env.context, env.plugin_config)
    env.context._star_manager = object()
    with pytest.raises(RuntimeError, match="必要"):
        env.main.Main(env.context, env.plugin_config)
