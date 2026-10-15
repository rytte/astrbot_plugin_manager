"""Verify automatic source discovery, lifecycle hooks and concurrent edits."""

import asyncio
import copy
from unittest.mock import AsyncMock, Mock

import pytest
import yaml


@pytest.fixture
async def auto_env(transfer_env, monkeypatch):
    env = transfer_env
    env.target = env.make_transfer_plugin(active=True)
    env.sources = env.plugin.sources
    env.repo = "https://github.com/test/demo"
    env.identifier = f"{env.target.author}/{env.target.name}"
    env.remote = {
        "name": env.target.name,
        "author": env.target.author,
        "repo": env.repo,
        "version": "2.0.0",
    }
    env.fetch = AsyncMock(return_value=({env.identifier: env.remote}, None))
    env.inspect = AsyncMock(return_value=env.remote)
    env.cache = {}
    env.load_cache = Mock(side_effect=lambda path: copy.deepcopy(env.cache))
    monkeypatch.setattr(env.plugin.service.web, "load_plugin_cache", env.load_cache)
    monkeypatch.setattr(env.plugin.service.web, "get_online_plugins", env.fetch)
    monkeypatch.setattr(
        env.manager, "inspect_plugin_repository", env.inspect, raising=False
    )
    yield env
    await env.sources.close()


def set_repo(env, value):
    env.target.repo = value
    path = env.sources.root / env.target.root_dir_name / "metadata.yaml"
    metadata = yaml.safe_load(path.read_text(encoding="utf-8"))
    metadata["repo"] = value
    path.write_text(yaml.safe_dump(metadata), encoding="utf-8")


async def autobind(env):
    await env.sources.schedule_missing_bindings()
    if env.sources.auto_task:
        await asyncio.wait_for(env.sources.auto_task, 5)
    return await env.sources.detail(env.target.root_dir_name)


@pytest.mark.parametrize("filename", ["metadata.yaml", "metadata.yml"])
async def test_market_autobind_accepts_missing_repo_and_preserves_provenance(
    auto_env, filename
):
    env = auto_env
    path = env.sources.root / env.target.root_dir_name / "metadata.yaml"
    path = path.rename(path.with_name(filename))
    before = await env.sources.detail(env.target.root_dir_name)
    record = await autobind(env)
    assert record["current_source"]["kind"] == "market"
    assert record["current_source"]["market_plugin_id"] == env.identifier
    assert record["current_source"]["verification"] == "identity_matched"
    assert record["provenance"] == before["provenance"]
    assert record["history"][-1]["action"] == "source_auto_bound"
    assert not record["metadata_changed"] and not record["auto_binding_notice"]
    assert env.target.repo == env.repo
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["repo"] == env.repo
    env.fetch.assert_awaited_once_with(custom_registry=None, force_refresh=True)
    env.inspect.assert_not_awaited()
    assert env.target.activated
    env.manager.load.assert_not_awaited()


async def test_market_autobind_checks_optional_repo(auto_env):
    env = auto_env
    set_repo(env, env.repo + ".git")
    record = await autobind(env)
    assert record["current_source"]["kind"] == "market"
    assert env.target.repo == env.repo + ".git"
    env.inspect.assert_not_awaited()


@pytest.mark.parametrize("trigger", ["background", "restore"])
@pytest.mark.parametrize("has_repo", [False, True])
async def test_matching_market_cache_binds_without_network(auto_env, trigger, has_repo):
    env = auto_env
    env.cache = {env.identifier: env.remote}
    if has_repo:
        set_repo(env, env.repo + ".git")
    env.fetch.side_effect = AssertionError("Matched cache must not fetch remotely")
    if trigger == "background":
        record = await autobind(env)
    else:
        async with env.plugin.transfers.operation_lock, env.manager._pm_lock:
            warnings = await env.sources.bind_restored(
                env.target.root_dir_name, "admin"
            )
        assert not warnings
        record = await env.sources.detail(env.target.root_dir_name)
    assert record["current_source"]["kind"] == "market"
    assert record["current_source"]["market_plugin_id"] == env.identifier
    assert record["current_source"]["verification"] == "identity_matched"
    assert not record["auto_binding_notice"]
    assert (
        env.prefs["plugin_install_sources"][env.target.root_dir_name][
            "market_plugin_id"
        ]
        == env.identifier
    )
    env.load_cache.assert_called_once()
    env.fetch.assert_not_awaited()
    env.inspect.assert_not_awaited()


@pytest.mark.parametrize(
    "case",
    ["missing", "name", "author", "repo", "ambiguous", "invalid_repo"],
)
async def test_market_cache_miss_or_mismatch_refreshes_once(auto_env, case):
    env = auto_env
    set_repo(env, env.repo)
    metadata = dict(env.remote)
    identifier = env.identifier
    if case == "name":
        metadata["name"] = "other_plugin"
    elif case == "author":
        metadata["author"] = "Other"
    elif case == "repo":
        metadata["repo"] = "https://github.com/other/demo"
    elif case == "invalid_repo":
        metadata["repo"] = "not-a-repository"
    if case != "missing":
        env.cache = {identifier: metadata}
    if case == "ambiguous":
        env.cache["Other/demo_plugin"] = env.remote
    record = await autobind(env)
    assert record["current_source"]["kind"] == "market"
    assert record["current_source"]["market_plugin_id"] == env.identifier
    assert record["current_source"]["repo_url"] == env.repo
    env.fetch.assert_awaited_once_with(custom_registry=None, force_refresh=True)
    env.inspect.assert_not_awaited()


async def test_cache_miss_rejects_network_failure_even_with_matching_fallback(auto_env):
    env = auto_env
    env.fetch.return_value = ({env.identifier: env.remote}, "stale cache")
    record = await autobind(env)
    assert record["current_source"] is None
    assert "市场联网校验失败" in record["auto_binding_notice"]
    env.fetch.assert_awaited_once_with(custom_registry=None, force_refresh=True)
    env.preferences.global_put.assert_not_awaited()
    env.inspect.assert_not_awaited()


@pytest.mark.parametrize("failure", ["notice", "exception"])
async def test_batch_network_failure_does_not_block_cached_matches(auto_env, failure):
    env = auto_env
    missing = [
        env.make_transfer_plugin(name="first_plugin", directory="aaa"),
        env.make_transfer_plugin(name="second_plugin", directory="bbb"),
    ]
    env.cache = {env.identifier: env.remote}
    if failure == "notice":
        env.fetch.return_value = ({}, "stale cache")
    else:
        env.fetch.side_effect = RuntimeError("Network unavailable")
    record = await autobind(env)
    assert record["current_source"]["market_plugin_id"] == env.identifier
    assert not record["auto_binding_notice"]
    for target in missing:
        unbound = await env.sources.detail(target.root_dir_name)
        assert unbound["current_source"] is None
        assert unbound["auto_binding_notice"]
    env.fetch.assert_awaited_once_with(custom_registry=None, force_refresh=True)
    env.inspect.assert_not_awaited()


async def test_unmatched_market_autobinds_verified_repository(auto_env):
    env = auto_env
    set_repo(env, env.repo)
    env.fetch.return_value = ({}, None)
    record = await autobind(env)
    assert record["current_source"]["kind"] == "repository"
    assert record["current_source"]["repo_url"] == env.repo
    assert record["current_source"]["verification"] == "identity_matched"
    core = env.prefs["plugin_install_sources"][env.target.root_dir_name]
    assert core["install_method"] == "repository" and core["repo"] == env.repo
    env.inspect.assert_awaited_once_with(env.repo)


@pytest.mark.parametrize(
    "case,reason",
    [
        ("missing", "未声明仓库地址"),
        ("ambiguous", "多个匹配"),
        ("conflict", "仓库地址与本机不一致"),
        ("offline", "联网校验失败"),
        ("repository_identity", "作者或插件名与本机不一致"),
    ],
)
async def test_autobind_failure_retains_unbound_source_and_reason(
    auto_env, case, reason
):
    env = auto_env
    if case == "missing":
        env.fetch.return_value = ({}, None)
    elif case == "ambiguous":
        env.fetch.return_value = (
            {env.identifier: env.remote, "Other/demo_plugin": env.remote},
            None,
        )
    else:
        set_repo(env, env.repo)
        if case == "conflict":
            env.fetch.return_value = (
                {
                    env.identifier: {
                        **env.remote,
                        "repo": "https://github.com/other/demo",
                    }
                },
                None,
            )
        elif case == "offline":
            env.fetch.return_value = ({}, "stale cache")
        elif case == "repository_identity":
            env.fetch.return_value = ({}, None)
            env.inspect.return_value = {**env.remote, "author": "Other"}
    record = await autobind(env)
    assert record["current_source"] is None
    assert reason in record["auto_binding_notice"]
    assert (
        reason
        in env.sources._store()["plugins"][env.target.root_dir_name][
            "auto_binding_notice"
        ]
    )
    env.preferences.global_put.assert_not_awaited()
    if case != "repository_identity":
        env.inspect.assert_not_awaited()


@pytest.mark.parametrize("method", ["market", "repository"])
async def test_existing_source_is_never_replaced(auto_env, method):
    env = auto_env
    env.prefs["plugin_install_sources"] = {
        env.target.root_dir_name: {
            "name": env.target.name,
            "install_method": method,
            "market_plugin_id": env.identifier,
            "repo": "https://github.com/selected/other",
            "registry_url": "https://custom.example/registry.json",
        }
    }
    original = copy.deepcopy(env.prefs)
    await autobind(env)
    assert env.prefs == original
    env.fetch.assert_not_awaited()
    env.inspect.assert_not_awaited()


async def test_startup_batches_loaded_and_disabled_plugins_into_one_market_query(
    auto_env,
):
    env = auto_env
    other = env.make_transfer_plugin(
        name="other_plugin", directory="other", active=False
    )
    env.fetch.return_value[0][f"{other.author}/{other.name}"] = {
        **env.remote,
        "name": other.name,
        "repo": "https://github.com/test/other",
    }
    await env.sources.initialize()
    await asyncio.wait_for(env.sources.auto_task, 5)
    assert env.prefs["plugin_install_sources"][env.target.root_dir_name]
    assert env.prefs["plugin_install_sources"][other.root_dir_name]
    assert not other.activated
    env.fetch.assert_awaited_once()
    env.manager.load.assert_not_awaited()


async def test_unloaded_and_reserved_plugins_are_not_autobound(auto_env):
    env = auto_env
    env.owners.pop(env.target.module_path)
    env.add_target(name="builtin", directory="builtin", reserved=True)
    record = await autobind(env)
    assert record["current_source"] is None
    env.fetch.assert_not_awaited()
    env.preferences.global_put.assert_not_awaited()


async def test_loading_hook_does_not_wait_for_market_or_core_lock(auto_env):
    env = auto_env
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch(**kwargs):
        entered.set()
        await release.wait()
        return {env.identifier: env.remote}, None

    env.fetch.side_effect = fetch
    async with env.manager._pm_lock:
        await asyncio.wait_for(env.plugin.on_plugin_loaded(env.target), 1)
        await asyncio.wait_for(entered.wait(), 5)
        env.preferences.global_put.assert_not_awaited()
    release.set()
    await asyncio.wait_for(env.sources.auto_task, 5)
    assert env.prefs["plugin_install_sources"][env.target.root_dir_name]


async def test_source_selected_while_market_fetches_wins_over_autobinding(auto_env):
    env = auto_env
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch(**kwargs):
        entered.set()
        await release.wait()
        return {env.identifier: env.remote}, None

    env.fetch.side_effect = fetch
    await env.sources.schedule_missing_bindings()
    await asyncio.wait_for(entered.wait(), 5)
    selected = {
        "install_method": "repository",
        "repo": "https://github.com/selected/repo",
    }
    env.prefs["plugin_install_sources"] = {env.target.root_dir_name: selected}
    release.set()
    await asyncio.wait_for(env.sources.auto_task, 5)
    assert env.prefs["plugin_install_sources"][env.target.root_dir_name] == selected
    env.preferences.global_put.assert_not_awaited()


async def test_shutdown_cancels_pending_network_and_prevents_binding(auto_env):
    env = auto_env
    entered = asyncio.Event()

    async def fetch(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    env.fetch.side_effect = fetch
    await env.sources.schedule_missing_bindings()
    await asyncio.wait_for(entered.wait(), 5)
    await env.plugin.terminate()
    assert env.sources.auto_task.cancelled()
    env.preferences.global_put.assert_not_awaited()
    env.sources.schedule_binding(env.target.root_dir_name)
    assert not env.sources.auto_pending


async def test_shutdown_finishes_source_write_already_in_progress(
    auto_env, monkeypatch
):
    env = auto_env
    entered, release = asyncio.Event(), asyncio.Event()
    original = env.sources.operations.web.save_plugin_install_sources

    async def save(records):
        entered.set()
        await release.wait()
        await original(records)

    monkeypatch.setattr(env.sources.operations.web, "save_plugin_install_sources", save)
    await env.sources.schedule_missing_bindings()
    await asyncio.wait_for(entered.wait(), 5)
    closing = asyncio.create_task(env.sources.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await asyncio.wait_for(closing, 5)
    record = await env.sources.detail(env.target.root_dir_name)
    assert record["current_source"]["verification"] == "identity_matched"
    assert record["history"][-1]["action"] == "source_auto_bound"


async def test_metadata_changed_during_discovery_is_never_bound(auto_env):
    env = auto_env

    async def fetch(**kwargs):
        path = env.sources.root / env.target.root_dir_name / "metadata.yaml"
        metadata = yaml.safe_load(path.read_text(encoding="utf-8"))
        metadata["author"] = "Different Author"
        path.write_text(yaml.safe_dump(metadata), encoding="utf-8")
        return {env.identifier: env.remote}, None

    env.fetch.side_effect = fetch
    record = await autobind(env)
    assert record["current_source"] is None
    assert record["metadata_changed"]
    env.preferences.global_put.assert_not_awaited()


async def test_restored_plugin_without_repo_can_bind_matching_market(auto_env):
    env = auto_env
    async with env.plugin.transfers.operation_lock, env.manager._pm_lock:
        warnings = await env.sources.bind_restored(env.target.root_dir_name, "admin")
    assert not warnings
    record = await env.sources.detail(env.target.root_dir_name)
    assert record["current_source"]["kind"] == "market"
    assert env.target.repo == env.repo
    env.inspect.assert_not_awaited()


async def test_startup_hook_finds_plugins_loaded_before_manager_was_ready(auto_env):
    env = auto_env
    env.plugin.ready = False
    await env.plugin.on_plugin_loaded(env.target)
    assert env.sources.auto_task is None
    env.plugin.ready = True
    await env.plugin.on_astrbot_loaded()
    await asyncio.wait_for(env.sources.auto_task, 5)
    assert env.prefs["plugin_install_sources"][env.target.root_dir_name]


async def test_automatically_bound_source_is_usable_for_update_checks(auto_env):
    env = auto_env
    await autobind(env)
    official = await env.plugin.service.web.resolve_market_update_info(env.target.name)
    assert official["record"]["market_plugin_id"] == env.identifier
    assert official["market_plugin"]["version"] == "2.0.0"
    await env.plugin.updates.start_check("admin")
    await asyncio.wait_for(env.plugin.updates.task, 5)
    result = env.plugin.updates.state("admin")["results"][0]
    assert result["status"] == "update_available" and result["can_update"]
    assert result["local_version"] == "1.0.0" and result["remote_version"] == "2.0.0"
