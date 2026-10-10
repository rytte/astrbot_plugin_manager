"""Verify bounded confirmations, deletion scope and partial cleanup outcomes."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


def output(event):
    return "\n".join(event.sent)


@pytest.mark.parametrize("delete_all", [False, True])
async def test_pm_removal_confirmation_works_in_real_pipeline(env, delete_all):
    target = env.add_target()
    option = " --all" if delete_all else ""
    request = env.event(f"/pm remove demo_directory{option}")
    await env.scheduler.execute(request)
    confirmation_command = f"/pm remove demo_directory{option} confirm"
    assert confirmation_command in output(request)
    assert "/plugin" not in output(request)
    env.manager.uninstall_plugin.assert_not_awaited()
    confirmed = env.event(confirmation_command)
    await env.scheduler.execute(confirmed)
    env.manager.uninstall_plugin.assert_awaited_once_with(
        target.name, delete_config=delete_all, delete_data=delete_all
    )
    assert "已卸载" in output(confirmed)
    assert confirmed.is_stopped() and not env.model_calls


async def test_remove_requires_private_confirmation_and_preserves_data_by_default(env):
    target = env.add_target()
    config = (
        Path(env.manager.plugin_config_path) / f"{target.root_dir_name}_config.json"
    )
    config.write_text("{}", encoding="utf-8")
    data = env.root / "plugin_data" / target.root_dir_name
    data.mkdir(parents=True)
    group = env.event(group="group")
    await env.invoke(group, "remove demo_plugin")
    assert "私聊" in output(group)
    env.manager.uninstall_plugin.assert_not_awaited()
    request = env.event()
    await env.invoke(request, "remove demo_directory")
    assert "60 秒" in output(request) and "保留配置和数据" in output(request)
    env.manager.uninstall_plugin.assert_not_awaited()
    confirmed = env.event()
    await env.invoke(confirmed, "remove demo_plugin confirm")
    env.manager.uninstall_plugin.assert_awaited_once_with(
        target.name, delete_config=False, delete_data=False
    )
    assert "已卸载" in output(confirmed)
    assert config.exists() and data.exists()


async def test_remove_all_deletes_files_configuration_data_and_checks_kv(env):
    target = env.add_target()
    config = (
        Path(env.manager.plugin_config_path) / f"{target.root_dir_name}_config.json"
    )
    config.write_text("{}", encoding="utf-8")
    for dirname in ("plugin_data", "plugins_data"):
        (env.root / dirname / target.root_dir_name).mkdir(parents=True)
    await env.invoke(env.event(), "remove demo_plugin --all")
    confirmed = env.event()
    await env.invoke(confirmed, "remove demo_plugin --all confirm")
    env.manager.uninstall_plugin.assert_awaited_once_with(
        target.name, delete_config=True, delete_data=True
    )
    assert "配置和数据已删除" in output(confirmed)
    env.preferences.range_get_async.assert_awaited_once_with("plugin", target.plugin_id)
    assert not config.exists()


@pytest.mark.parametrize("delete_all", [False, True])
async def test_failed_plugin_is_removed_by_directory_through_webui_service(
    env, delete_all
):
    name = "broken_directory"
    (Path(env.manager.plugin_store_path) / name).mkdir()
    env.manager.failed_plugin_dict[name] = {"reserved": False}
    option = " --all" if delete_all else ""
    await env.invoke(env.event(), f"remove {name}{option}")
    confirmed = env.event()
    await env.invoke(confirmed, f"remove {name}{option} confirm")
    env.manager.uninstall_failed_plugin.assert_awaited_once_with(
        name, delete_config=delete_all, delete_data=delete_all
    )
    env.manager.uninstall_plugin.assert_not_awaited()
    assert "已卸载" in output(confirmed)


@pytest.mark.parametrize(
    "change", ["scope", "target", "instance", "state", "session", "sender", "expiry"]
)
async def test_confirmation_is_bound_to_unchanged_target_scope_identity_and_session(
    env, change
):
    target = env.add_target()
    env.add_target(name="second", directory="second_directory")
    initial = env.event()
    await env.invoke(initial, "remove demo_plugin")
    confirmed = env.event()
    arguments = "remove demo_plugin confirm"
    if change == "scope":
        arguments = "remove demo_plugin --all confirm"
    elif change == "target":
        arguments = "remove second confirm"
    elif change == "instance":
        target.star_cls = object()
    elif change == "state":
        target.activated = False
    elif change == "session":
        confirmed = env.event(platform="other_platform")
    elif change == "sender":
        confirmed = env.event(user="another_admin")
    else:
        key = (initial.unified_msg_origin, initial.get_sender_id())
        env.plugin._removals[key] = replace(env.plugin._removals[key], expires=0)
    await env.invoke(confirmed, arguments)
    assert "已过期或插件状态已变化" in output(confirmed)
    env.manager.uninstall_plugin.assert_not_awaited()


@pytest.mark.parametrize("failed", [False, True])
async def test_reserved_plugins_cannot_be_uninstalled(env, failed):
    if failed:
        env.manager.failed_plugin_dict["demo_plugin"] = {"reserved": True}
    else:
        env.add_target(reserved=True)
    event = env.event()
    await env.invoke(event, "remove demo_plugin")
    assert "保留插件" in output(event)
    assert not env.plugin._removals


async def test_uninstall_service_failure_does_not_claim_success_or_allow_replay(env):
    env.add_target()
    env.manager.uninstall_plugin = AsyncMock(side_effect=RuntimeError("failure"))
    await env.invoke(env.event(), "remove demo_plugin")
    first = env.event()
    await env.invoke(first, "remove demo_plugin confirm")
    assert "失败" in output(first) and "已卸载" not in output(first)
    second = env.event()
    await env.invoke(second, "remove demo_plugin confirm")
    assert "已过期" in output(second)
    assert env.manager.uninstall_plugin.await_count == 1


@pytest.mark.parametrize("remaining", ["directory", "configuration", "kv"])
async def test_partial_cleanup_is_reported_instead_of_complete_success(env, remaining):
    target = env.add_target()
    if remaining == "directory":
        env.manager.uninstall_plugin = AsyncMock()
    elif remaining == "configuration":
        candidate = (
            Path(env.manager.plugin_config_path) / f"{target.root_dir_name}_config.json"
        )
        candidate.write_text("{}", encoding="utf-8")
        original = env.manager.uninstall_plugin.side_effect

        async def partial_uninstall(name, **kwargs):
            await original(name, delete_config=False, delete_data=True)

        env.manager.uninstall_plugin.side_effect = partial_uninstall
    else:
        env.preferences.range_get_async.return_value = [object()]
    await env.invoke(env.event(), "remove demo_plugin --all")
    confirmed = env.event()
    await env.invoke(confirmed, "remove demo_plugin --all confirm")
    assert "未完全清理" in output(confirmed)
    assert "已卸载" not in output(confirmed)


async def test_invalid_failed_record_is_not_treated_as_legacy_metadata(env):
    env.manager.failed_plugin_dict["broken_directory"] = "old record"
    with pytest.raises(RuntimeError, match="必须是字典"):
        env.plugin.service.resolve("broken_directory")
