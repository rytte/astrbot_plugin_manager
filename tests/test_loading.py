"""Pending local loads require confirmation and an unchanged, unique target."""

import asyncio
from pathlib import Path

import pytest


@pytest.fixture
async def pending_plugin(transfer_env):
    env = transfer_env
    target = env.make_transfer_plugin(active=False)
    env.owners.pop(target.module_path)
    record = await env.plugin.sources.detail(target.root_dir_name)
    assert record["local"]["status"] == "pending"
    return env, target, record


@pytest.mark.parametrize("acknowledged", [False, "true", 1, None])
async def test_load_requires_explicit_confirmation(pending_plugin, acknowledged):
    env, target, record = pending_plugin
    with pytest.raises(ValueError, match="请确认"):
        await env.plugin.loading.start(
            "admin", target.root_dir_name, record["revision"], acknowledged
        )
    env.manager.load.assert_not_awaited()
    assert not env.plugin.transfers.jobs


@pytest.mark.parametrize("state", ["enabled", "disabled", "failed", "conflict", "stale"])
async def test_load_rejects_changed_or_conflicting_targets(pending_plugin, state):
    env, target, record = pending_plugin
    if state in {"enabled", "disabled"}:
        target.activated = state == "enabled"
        env.owners[target.module_path] = target
    elif state == "failed":
        env.manager.failed_plugin_dict[target.root_dir_name] = {"name": target.name}
    elif state == "conflict":
        env.add_target(name=target.name, directory="other_directory")
    revision = record["revision"] if state != "stale" else "stale"
    with pytest.raises(ValueError):
        await env.plugin.loading.start("admin", target.root_dir_name, revision, True)
    env.manager.load.assert_not_awaited()
    assert not env.plugin.transfers.jobs


async def test_load_rejects_traversal_and_reserved_directory(pending_plugin):
    env, target, record = pending_plugin
    with pytest.raises(ValueError):
        await env.plugin.loading.start("admin", "../escape", record["revision"], True)
    reserved = env.plugin.transfers.manager.reserved_plugin_path
    (Path(reserved) / target.root_dir_name).mkdir()
    with pytest.raises(ValueError, match="保留插件"):
        await env.plugin.loading.start(
            "admin", target.root_dir_name, record["revision"], True
        )
    env.manager.load.assert_not_awaited()
    assert not env.plugin.transfers.jobs


async def test_load_rechecks_after_waiting_for_core_lock(pending_plugin):
    env, target, record = pending_plugin
    await env.manager._pm_lock.acquire()
    try:
        started = await env.plugin.loading.start(
            "admin", target.root_dir_name, record["revision"], True
        )
        await asyncio.sleep(0)
        env.owners[target.module_path] = target
    finally:
        env.manager._pm_lock.release()
    await env.plugin.transfers.task
    job = env.plugin.transfers.get_job(started["id"], "admin")
    assert job["status"] == "failed"
    assert "状态已变化" in job["error"]
    env.manager.load.assert_not_awaited()


async def test_load_rejects_metadata_changed_between_reads(pending_plugin, monkeypatch):
    env, target, record = pending_plugin
    loading = env.plugin.loading
    original = loading._source

    def changed_source(directory):
        source = original(directory)
        source["metadata"]["version"] = "2.0.0"
        return source

    monkeypatch.setattr(loading, "_source", changed_source)
    with pytest.raises(ValueError, match="文件中的信息已变化"):
        await loading.start("admin", target.root_dir_name, record["revision"], True)
    env.manager.load.assert_not_awaited()
    assert not env.plugin.transfers.jobs


@pytest.mark.parametrize("outcome", ["error", "exception", "not_registered", "wrong_identity"])
async def test_failed_load_restores_disabled_preference(pending_plugin, outcome):
    env, target, record = pending_plugin
    env.prefs["inactivated_plugins"] = [target.module_path, "data.plugins.other.main"]

    async def load(*, specified_dir_name):
        assert specified_dir_name == target.root_dir_name
        assert env.manager._pm_lock.locked()
        assert target.module_path not in env.prefs["inactivated_plugins"]
        if outcome == "exception":
            raise RuntimeError("dependency unavailable")
        if outcome == "error":
            env.manager.failed_plugin_dict[specified_dir_name] = {"name": target.name}
            return False, "依赖不可用"
        if outcome == "wrong_identity":
            target.author = "Other"
            target.activated = True
            target.star_cls = object()
            env.owners[target.module_path] = target
        return True, None

    env.manager.load.side_effect = load
    started = await env.plugin.loading.start(
        "admin", target.root_dir_name, record["revision"], True
    )
    await env.plugin.transfers.task
    job = env.plugin.transfers.get_job(started["id"], "admin")
    assert job["status"] == "failed"
    assert "加载未完成" in job["error"]
    assert job["results"][0]["status"] == "failed"
    assert set(env.prefs["inactivated_plugins"]) == {
        target.module_path,
        "data.plugins.other.main",
    }
    assert not env.plugin.transfers.in_critical
    assert target.module_path not in env.owners
    env.sync.assert_not_awaited()


async def test_load_is_serialized_and_sync_failure_does_not_invent_load_failure(
    pending_plugin
):
    env, target, record = pending_plugin

    async def load(*, specified_dir_name):
        assert specified_dir_name == target.root_dir_name
        assert env.manager._pm_lock.locked()
        assert env.plugin._operations.locked()
        target.activated = True
        target.star_cls = object()
        env.owners[target.module_path] = target
        return True, None

    env.manager.load.side_effect = load
    env.sync.side_effect = RuntimeError("skills unavailable")
    started = await env.plugin.loading.start(
        "admin", target.root_dir_name, record["revision"], True
    )
    with pytest.raises(ValueError, match="任务"):
        await env.plugin.loading.start(
            "admin", target.root_dir_name, record["revision"], True
        )
    await env.plugin.transfers.task
    job = env.plugin.transfers.get_job(started["id"], "admin")
    assert job["status"] == "completed_with_warning"
    assert job["results"][0]["status"] == "enabled"
    assert "技能同步失败" in job["warnings"][0]
    assert target.activated
    env.manager.load.assert_awaited_once_with(specified_dir_name=target.root_dir_name)
