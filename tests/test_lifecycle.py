"""Verify single-target loading, cache failures and identity races."""

import asyncio
import copy
from unittest.mock import AsyncMock

import pytest


def output(event):
    return "\n".join(event.sent)


@pytest.mark.parametrize("group", [None, "group"])
@pytest.mark.parametrize("identifier", ["demo_plugin", "demo_directory"])
async def test_reload_is_immediate_and_strictly_targeted(env, group, identifier):
    target = env.add_target()
    original = target.star_cls
    event = env.event(f"/pm reload {identifier}", group=group)
    await env.scheduler.execute(event)
    assert env.actions == ["terminate", "unbind", "load"]
    assert "已重载" in output(event)
    env.manager.load.assert_awaited_once_with(specified_module_path=target.module_path)
    env.manager.reload.assert_not_awaited()
    env.sync.assert_awaited_once()
    assert env.owners[target.module_path].star_cls is not original
    assert event.is_stopped() and not env.model_calls


@pytest.mark.parametrize("state", ["missing", "failed", "disabled", "incomplete"])
async def test_reload_rejects_unsafe_states_without_fallback(env, state):
    if state == "failed":
        env.manager.failed_plugin_dict["demo_plugin"] = {"reserved": False}
    elif state != "missing":
        target = env.add_target(active=state != "disabled")
        if state == "incomplete":
            target.star_cls = None
    event = env.event()
    await env.invoke(event, "reload demo_plugin")
    assert "已重载" not in output(event)
    env.manager.reload.assert_not_awaited()
    env.manager.load.assert_not_awaited()


@pytest.mark.parametrize("action", ["reload", "enable"])
async def test_identity_is_rechecked_after_waiting_for_manager_lock(env, action):
    target = env.add_target(active=action != "enable")
    event = env.event()
    await env.manager._pm_lock.acquire()
    task = asyncio.create_task(env.invoke(event, f"{action} demo_plugin"))
    try:
        for _ in range(50):
            await asyncio.sleep(0)
            if event.sent:
                break
        assert event.sent
        env.owners[target.module_path] = copy.copy(target)
    finally:
        env.manager._pm_lock.release()
    await task
    assert "状态已变化" in output(event)
    env.manager._terminate_plugin.assert_not_awaited()
    env.manager.load.assert_not_awaited()
    env.preferences.global_put.assert_not_awaited()


async def test_enable_uses_locked_single_module_load_and_preserves_other_preferences(
    env,
):
    target = env.add_target(active=False)
    env.prefs["inactivated_plugins"] = [target.module_path, "other.main"]
    env.prefs["inactivated_llm_tools"] = ["disabled_tool"]
    event = env.event("/pm enable demo_directory")
    await env.scheduler.execute(event)
    assert "已启用" in output(event)
    assert env.prefs["inactivated_plugins"] == ["other.main"]
    assert env.prefs["inactivated_llm_tools"] == ["disabled_tool"]
    env.manager.turn_on_plugin.assert_not_awaited()
    env.manager.reload.assert_not_awaited()
    env.manager.load.assert_awaited_once_with(specified_module_path=target.module_path)


async def test_enable_already_active_plugin_does_not_reload(env):
    target = env.add_target()
    event = env.event(f"/pm enable {target.name}")
    await env.scheduler.execute(event)
    assert "已处于启用" in output(event)
    env.manager._terminate_plugin.assert_not_awaited()
    env.manager.turn_on_plugin.assert_not_awaited()
    env.manager.load.assert_not_awaited()
    env.preferences.global_put.assert_not_awaited()


async def test_disable_uses_webui_service_and_is_idempotent(env):
    target = env.add_target()
    first = env.event("/pm disable demo_directory")
    await env.scheduler.execute(first)
    env.manager.turn_off_plugin.assert_awaited_once_with(target.name)
    assert "已停用" in output(first)
    env.sync.assert_awaited_once()
    second = env.event("/pm disable demo_plugin")
    await env.scheduler.execute(second)
    assert "已处于停用" in output(second)
    assert env.manager.turn_off_plugin.await_count == 1


async def test_cache_cleanup_failure_does_not_terminate_or_unbind(env, monkeypatch):
    env.add_target()

    def broken_cache(*args):
        raise OSError("Cannot clear bytecode")

    monkeypatch.setattr(env.service_module, "clear_plugin_bytecode", broken_cache)
    event = env.event()
    await env.invoke(event, "reload demo_plugin")
    assert "失败" in output(event) and "WebUI" in output(event)
    assert "已重载" not in output(event)
    env.manager._terminate_plugin.assert_not_awaited()
    env.manager._unbind_plugin.assert_not_awaited()


@pytest.mark.parametrize("step", ["terminate", "unbind", "load", "registration"])
async def test_lifecycle_failure_never_claims_success(env, step):
    env.add_target()
    if step == "terminate":
        env.manager._terminate_plugin.side_effect = ValueError("termination failed")
    elif step == "unbind":
        env.manager._unbind_plugin.side_effect = RuntimeError("unbind failed")
    elif step == "load":
        env.manager.load = AsyncMock(return_value=(False, "loading failed"))
    else:
        env.manager.load = AsyncMock(return_value=(True, None))
    event = env.event()
    await env.invoke(event, "reload demo_plugin")
    assert "失败" in output(event) and "WebUI" in output(event)
    assert "已重载" not in output(event)
    env.sync.assert_not_awaited()


@pytest.mark.parametrize("action", ["disable", "remove"])
async def test_self_disable_and_uninstall_are_rejected(env, action):
    event = env.event()
    await env.invoke(event, f"{action} astrbot_plugin_manager")
    assert "自身" in output(event)
    env.manager.turn_off_plugin.assert_not_awaited()
    env.manager.uninstall_plugin.assert_not_awaited()


async def test_terminate_invalidates_confirmation_state(env):
    env.add_target()
    await env.invoke(env.event(), "remove demo_plugin")
    assert env.plugin._removals
    await env.plugin.terminate()
    assert not env.plugin.ready and not env.plugin._removals
