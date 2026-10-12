"""Verify configured prefix omission, ambiguity rejection and guarded operations."""

import asyncio
from pathlib import Path

import pytest


def output(event):
    return "\n".join(event.sent)


async def test_schema_enables_prefix_omission_by_default(env):
    assert env.plugin_config["allow_prefix_omission"] is True
    assert env.plugin.service.allow_prefix_omission is True


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"allow_prefix_omission": "false"},
        {"allow_prefix_omission": 1},
        {"allow_prefix_omission": None},
        {"allow_prefix_omission": []},
        {"allow_prefix_omission": True, "unexpected": True},
    ],
)
async def test_invalid_or_unknown_configuration_is_rejected(env, config):
    with pytest.raises(ValueError, match="allow_prefix_omission|未知"):
        env.main.Main(env.context, config)


@pytest.mark.parametrize(
    "env",
    [{"allow_prefix_omission": True}, {"allow_prefix_omission": False}],
    indirect=True,
)
@pytest.mark.parametrize("match_by", ["name", "directory"])
async def test_omission_setting_applies_to_names_and_directories(env, match_by):
    name = "astrbot_plugin_demo" if match_by == "name" else "original_name"
    directory = "original_directory" if match_by == "name" else "astrbot_plugin_demo"
    target = env.add_target(name, directory)
    assert env.plugin.service.resolve("astrbot_plugin_demo").plugin is target
    if env.plugin_config["allow_prefix_omission"]:
        resolved = env.plugin.service.resolve("demo")
        assert resolved.plugin is target
        assert resolved.identifier == "demo"
        env.plugin.service.revalidate(resolved)
    else:
        with pytest.raises(ValueError, match="未找到插件 demo"):
            env.plugin.service.resolve("demo")


async def test_short_and_prefixed_matches_for_one_plugin_are_not_ambiguous(env):
    target = env.add_target("astrbot_plugin_demo", "demo")
    assert env.plugin.service.resolve("demo").plugin is target


async def test_complete_prefix_is_not_expanded_again(env):
    target = env.add_target(
        "astrbot_plugin_astrbot_plugin_demo", "double_prefix_directory"
    )
    with pytest.raises(ValueError, match="未找到"):
        env.plugin.service.resolve("astrbot_plugin_demo")
    assert env.plugin.service.resolve(target.name).plugin is target


@pytest.mark.parametrize("identifier", ["dem", "Demo", "DEMO", "astrbot_demo"])
async def test_prefix_omission_does_not_add_fuzzy_or_case_insensitive_matching(
    env, identifier
):
    env.add_target("astrbot_plugin_demo", "astrbot_plugin_demo")
    with pytest.raises(ValueError, match="未找到"):
        env.plugin.service.resolve(identifier)


@pytest.mark.parametrize("identifier", ["broken", "astrbot_plugin_broken"])
async def test_failed_plugin_directory_resolves_to_its_complete_name(env, identifier):
    record = {"reserved": False}
    env.manager.failed_plugin_dict["astrbot_plugin_broken"] = record
    target = env.plugin.service.resolve(identifier)
    assert target.name == "astrbot_plugin_broken"
    assert target.directory == "astrbot_plugin_broken"
    assert target.failure is record
    env.plugin.service.revalidate(target)


async def test_expanded_failed_plugin_record_is_validated(env):
    env.manager.failed_plugin_dict["astrbot_plugin_broken"] = "invalid"
    with pytest.raises(RuntimeError, match="记录格式无效"):
        env.plugin.service.resolve("broken")


@pytest.mark.parametrize("failed_directory", ["demo", "astrbot_plugin_demo"])
async def test_loaded_and_failed_plugin_matches_are_not_silently_prioritized(
    env, failed_directory
):
    env.add_target("astrbot_plugin_demo", "loaded_directory")
    env.manager.failed_plugin_dict[failed_directory] = {"reserved": False}
    with pytest.raises(ValueError, match="歧义"):
        env.plugin.service.resolve("demo")


@pytest.mark.parametrize(
    "action", ["info", "update", "enable", "disable", "reload", "remove"]
)
async def test_all_named_commands_accept_prefix_omission_in_real_pipeline(env, action):
    target = env.add_target(
        "astrbot_plugin_demo", "astrbot_plugin_demo", active=action != "enable"
    )
    event = env.event(f"/pm {action} demo")
    await env.scheduler.execute(event)
    expected = {
        "info": f"插件名：{target.name}",
        "update": "没有当前更新源",
        "enable": "已启用",
        "disable": "已停用",
        "reload": "已重载",
        "remove": "确认卸载",
    }
    assert expected[action] in output(event)
    assert event.is_stopped() and not env.model_calls
    if action == "remove":
        env.manager.uninstall_plugin.assert_not_awaited()
        confirmed = env.event("/pm remove demo confirm")
        await env.scheduler.execute(confirmed)
        env.manager.uninstall_plugin.assert_awaited_once_with(
            target.name, delete_config=False, delete_data=False
        )
        assert "已卸载" in output(confirmed)


@pytest.mark.parametrize("env", [{"allow_prefix_omission": False}], indirect=True)
@pytest.mark.parametrize(
    "action", ["info", "update", "enable", "disable", "reload", "remove"]
)
async def test_disabled_setting_rejects_omission_without_side_effects(env, action):
    env.add_target("astrbot_plugin_demo", "astrbot_plugin_demo")
    event = env.event(f"/pm {action} demo")
    await env.scheduler.execute(event)
    assert "未找到插件 demo" in output(event)
    env.manager.load.assert_not_awaited()
    env.manager.turn_off_plugin.assert_not_awaited()
    env.manager.uninstall_plugin.assert_not_awaited()
    env.preferences.global_put.assert_not_awaited()
    assert not env.plugin._removals
    assert event.is_stopped() and not env.model_calls


@pytest.mark.parametrize(
    "action", ["info", "update", "enable", "disable", "reload", "remove"]
)
async def test_exact_and_expanded_names_for_different_plugins_are_ambiguous(
    env, action
):
    plain = env.add_target("demo", "plain_directory")
    prefixed = env.add_target("astrbot_plugin_demo", "prefixed_directory")
    event = env.event(f"/pm {action} demo")
    await env.scheduler.execute(event)
    assert "歧义" in output(event)
    assert plain.root_dir_name in output(event)
    assert prefixed.root_dir_name in output(event)
    env.manager.load.assert_not_awaited()
    env.manager.turn_off_plugin.assert_not_awaited()
    env.manager.uninstall_plugin.assert_not_awaited()
    assert not env.plugin._removals


@pytest.mark.parametrize("env", [{"allow_prefix_omission": False}], indirect=True)
async def test_disabled_setting_does_not_create_expanded_name_conflicts(env):
    plain = env.add_target("demo", "plain_directory")
    env.add_target("astrbot_plugin_demo", "prefixed_directory")
    assert env.plugin.service.resolve("demo").plugin is plain


@pytest.mark.parametrize("action", ["update", "disable", "remove"])
async def test_omission_does_not_bypass_self_protection(env, action):
    event = env.event(f"/pm {action} manager")
    await env.scheduler.execute(event)
    assert "自身" in output(event)
    env.manager.turn_off_plugin.assert_not_awaited()
    env.manager.uninstall_plugin.assert_not_awaited()


@pytest.mark.parametrize(
    "request_identifier,confirmation_identifier",
    [("demo", "astrbot_plugin_demo"), ("astrbot_plugin_demo", "demo")],
)
async def test_removal_confirmation_binds_the_plugin_not_the_omitted_prefix(
    env, request_identifier, confirmation_identifier
):
    target = env.add_target("astrbot_plugin_demo", "astrbot_plugin_demo")
    request = env.event(f"/pm remove {request_identifier}")
    await env.scheduler.execute(request)
    assert f"/pm remove {request_identifier} confirm" in output(request)
    confirmed = env.event(f"/pm remove {confirmation_identifier} confirm")
    await env.scheduler.execute(confirmed)
    env.manager.uninstall_plugin.assert_awaited_once_with(
        target.name, delete_config=False, delete_data=False
    )
    assert "已卸载" in output(confirmed)


async def test_failed_plugin_removal_uses_the_complete_directory(env):
    directory = Path(env.manager.plugin_store_path) / "astrbot_plugin_broken"
    directory.mkdir()
    env.manager.failed_plugin_dict[directory.name] = {"reserved": False}
    await env.scheduler.execute(env.event("/pm remove broken"))
    confirmed = env.event("/pm remove broken confirm")
    await env.scheduler.execute(confirmed)
    env.manager.uninstall_failed_plugin.assert_awaited_once_with(
        directory.name, delete_config=False, delete_data=False
    )
    assert "已卸载" in output(confirmed)
    assert not directory.exists()


@pytest.mark.parametrize("action", ["enable", "reload"])
async def test_omitted_name_is_revalidated_after_waiting_for_the_manager_lock(
    env, action
):
    env.add_target(
        "astrbot_plugin_demo", "astrbot_plugin_demo", active=action != "enable"
    )
    event = env.event()
    await env.manager._pm_lock.acquire()
    task = asyncio.create_task(env.invoke(event, f"{action} demo"))
    try:
        for attempt in range(50):
            await asyncio.sleep(0)
            if event.sent:
                break
        assert event.sent
        env.add_target("demo", "new_directory")
    finally:
        env.manager._pm_lock.release()
    await task
    assert "状态已变化" in output(event)
    env.manager._terminate_plugin.assert_not_awaited()
    env.manager.load.assert_not_awaited()
    env.preferences.global_put.assert_not_awaited()
