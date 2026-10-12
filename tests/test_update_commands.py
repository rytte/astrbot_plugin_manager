"""Exercise chat checks and direct updates through the real command pipeline."""

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml


def output(event):
    return "\n".join(event.sent)


def metadata(target, version="2.0.0", **changes):
    return {
        "name": target.name,
        "author": target.author,
        "version": version,
        "repo": f"https://github.com/test/{target.name}",
        **changes,
    }


def bind(env, target, kind="market"):
    remote = metadata(target)
    core = {
        "install_method": kind,
        "repo": remote["repo"],
        "market_plugin_id": f"{target.author}/{target.name}"
        if kind == "market"
        else "",
        "registry_url": None,
        "installed_at": "2025-01-01T00:00:00Z",
    }
    env.prefs.setdefault("plugin_install_sources", {})[target.root_dir_name] = core
    return core


async def add_pending_repository_source(env, target):
    record = await env.plugin.sources.detail(target.root_dir_name)
    record["pending_source"] = env.plugin.sources._binding(
        {"install_method": "repository", "repo": metadata(target)["repo"]}
    )
    await env.plugin.sources._save_record(record)


@pytest.fixture
def chat_plugin(env, monkeypatch):
    target = env.add_target("astrbot_plugin_demo", "demo_directory")
    target.display_name = "示例插件"
    target.repo = metadata(target)["repo"]
    bind(env, target)
    market = AsyncMock(return_value=({target.plugin_id: metadata(target)}, None))
    repository = AsyncMock(return_value=metadata(target))
    monkeypatch.setattr(env.plugin.service.web, "get_online_plugins", market)
    monkeypatch.setattr(
        env.manager, "inspect_plugin_repository", repository, raising=False
    )
    return env, target, market, repository


async def test_check_reports_current_sources_without_listing_unchanged_plugins(chat_plugin):
    env, target, market, repository = chat_plugin
    await add_pending_repository_source(env, target)
    unchanged = env.add_target("unchanged", "unchanged")
    bind(env, unchanged)
    broken = env.add_target("broken", "broken")
    bind(env, broken)
    alternative = env.add_target("alternative", "alternative")
    bind(env, alternative, "repository")
    market.return_value = (
        {
            target.plugin_id: metadata(target),
            unchanged.plugin_id: metadata(unchanged, "1.0.0"),
            alternative.plugin_id: metadata(alternative, "1.0.0"),
        },
        None,
    )
    repository.side_effect = lambda url: {
        metadata(target)["repo"]: metadata(target),
        metadata(alternative)["repo"]: metadata(alternative),
    }[url]
    await env.plugin.updates.start_check("web-user")
    await env.plugin.updates.task
    web_report = env.plugin.updates.state("web-user")
    market.reset_mock()
    repository.reset_mock()

    event = env.event("/pm check-update", group="admins")
    await env.scheduler.execute(event)

    text = output(event)
    assert event.sent[0] == "正在按各插件的当前来源检查更新，请稍候。"
    assert "4 个插件。" in text
    assert "可更新 2 个插件 · 无版本更新 1 个插件 · 检查失败 1 个插件" in text
    assert "示例插件（astrbot_plugin_demo）：1.0.0 → 2.0.0" in text
    assert "/pm update demo_directory" in text
    assert "/pm update alternative" in text and "其他来源有新版本" not in text
    assert "检查失败" in text and "来源中未找到该插件" in text
    assert "unchanged" not in text
    market.assert_awaited_once_with(custom_registry=None, force_refresh=True)
    assert repository.await_count == 1
    assert env.plugin.updates.state("web-user") == web_report
    assert not env.plugin.transfers.jobs
    assert event.is_stopped() and not env.model_calls


async def test_check_without_current_sources_does_not_access_network(env, monkeypatch):
    fetch = AsyncMock(side_effect=AssertionError("must not query the market"))
    monkeypatch.setattr(env.plugin.service.web, "get_online_plugins", fetch)
    event = env.event("/pm check-update")
    await env.scheduler.execute(event)
    assert "没有当前更新源可检查" in output(event)
    fetch.assert_not_awaited()
    assert event.is_stopped() and not env.model_calls


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("kind", ["market", "repository"])
async def test_direct_update_uses_current_source_and_preserves_state(
    chat_plugin, monkeypatch, active, kind
):
    env, target, market, repository = chat_plugin
    target.activated = active
    target.star_cls = object() if active else None
    bind(env, target, kind)
    if kind == "market":
        await add_pending_repository_source(env, target)
    original = await env.plugin.sources.detail(target.root_dir_name)
    old_provenance = copy.deepcopy(original["provenance"])
    path = Path(env.manager.plugin_store_path) / target.root_dir_name
    download_url = "https://example.com/plugin.zip" if kind == "market" else ""
    market.return_value = (
        {target.plugin_id: metadata(target, download_url=download_url)},
        None,
    )

    async def download(plugin, *, repo_url, download_url):
        assert env.manager._pm_lock.locked()
        assert env.plugin.transfers.operation_lock.locked()
        assert repo_url == metadata(target)["repo"]
        assert download_url == (
            "https://example.com/plugin.zip" if kind == "market" else ""
        )
        plugin.version = "2.0.0"
        (path / "metadata.yaml").write_text(
            yaml.safe_dump({**metadata(plugin), "display_name": plugin.display_name}),
            encoding="utf-8",
        )

    async def load(*, specified_module_path):
        assert env.manager._pm_lock.locked()
        current = copy.copy(target)
        current.star_cls = object() if active else None
        env.owners[specified_module_path] = current
        return True, None

    env.manager._updater = SimpleNamespace(update=AsyncMock(side_effect=download))
    env.manager._ensure_plugin_requirements = AsyncMock()
    env.manager.load.side_effect = load
    await env.plugin.updates.start_check("web-user")
    await env.plugin.updates.task
    web_report = env.plugin.updates.state("web-user")
    market.reset_mock()
    repository.reset_mock()
    event = env.event("/pm update demo", group="admins" if active else None)
    await env.scheduler.execute(event)

    assert "已更新至 2.0.0" in output(event), output(event)
    assert ("已启用" if active else "已停用") in output(event)
    assert "confirm" not in output(event)
    assert len(event.sent) == 2
    assert env.owners[target.module_path].activated is active
    env.manager._updater.update.assert_awaited_once()
    env.manager._ensure_plugin_requirements.assert_awaited_once_with(
        str(path), target.name
    )
    assert market.await_count == (1 if kind == "market" else 0)
    assert repository.await_count == (1 if kind == "repository" else 0)
    current = await env.plugin.sources.detail(target.root_dir_name)
    assert current["provenance"] == old_provenance
    assert current["current_source"] == original["current_source"]
    assert not current["metadata_changed"]
    assert env.plugin.updates.state("web-user") == web_report
    assert not env.plugin.transfers.history("web-user")
    job = env.plugin.transfers.jobs[env.plugin.transfers.active_job]
    assert job["status"] == "completed" and job["owner"].startswith("chat:")
    assert job["results"][0]["status"] == "updated"
    assert event.is_stopped() and not env.model_calls


@pytest.mark.parametrize("version", ["1.0.0", "0.9.0"])
async def test_direct_update_without_new_version_does_not_download(
    chat_plugin, version
):
    env, target, market, repository = chat_plugin
    market.return_value = ({target.plugin_id: metadata(target, version)}, None)
    original = await env.plugin.sources.detail(target.root_dir_name)
    event = env.event("/pm update demo_directory")
    await env.scheduler.execute(event)
    assert "已是最新版本" in output(event)
    assert (await env.plugin.sources.detail(target.root_dir_name)) == original
    assert market.await_count == 1
    repository.assert_not_awaited()
    env.manager.load.assert_not_awaited()
    env.manager._terminate_plugin.assert_not_awaited()
    job = env.plugin.transfers.jobs[env.plugin.transfers.active_job]
    assert job["status"] == "completed" and job["results"][0]["status"] == "no_update"


async def test_missing_current_source_does_not_infer_repo_or_activate_pending_source(
    chat_plugin,
):
    env, target, market, repository = chat_plugin
    await add_pending_repository_source(env, target)
    env.prefs["plugin_install_sources"] = {}
    target.repo = metadata(target)["repo"]
    event = env.event("/pm update demo")
    await env.scheduler.execute(event)
    assert "没有当前更新源" in output(event)
    market.assert_not_awaited()
    repository.assert_not_awaited()
    assert not env.plugin.transfers.jobs


@pytest.mark.parametrize(
    "failure", ["identity", "version", "cache", "timeout", "network"]
)
async def test_current_source_failure_does_not_fall_back_to_pending_source(
    chat_plugin, failure
):
    env, target, market, repository = chat_plugin
    await add_pending_repository_source(env, target)
    if failure == "identity":
        market.return_value = (
            {target.plugin_id: metadata(target, author="Impostor")},
            None,
        )
    elif failure == "version":
        market.return_value = ({target.plugin_id: metadata(target, "latest")}, None)
    elif failure == "cache":
        market.return_value = ({target.plugin_id: metadata(target)}, "offline cache")
    else:
        market.side_effect = (
            asyncio.TimeoutError() if failure == "timeout" else RuntimeError("offline")
        )
    event = env.event("/pm update demo")
    await env.scheduler.execute(event)
    assert "已更新至" not in output(event) and "已是最新版本" not in output(event)
    assert "可能已修改" not in output(event)
    if failure == "timeout":
        assert "当前更新源检查超时" in output(event)
    repository.assert_not_awaited()
    env.manager._terminate_plugin.assert_not_awaited()
    job = env.plugin.transfers.jobs[env.plugin.transfers.active_job]
    assert job["status"] == "failed" and job["results"][0]["status"] == "failed"


async def test_queued_direct_update_rejects_changed_source_before_network(chat_plugin):
    env, target, market, repository = chat_plugin
    async with env.plugin._operations:
        await env.plugin.chat_updates.start_current_update("chat-user", "demo")
        bind(env, target, "repository")
    await env.plugin.transfers.task
    job = env.plugin.transfers.jobs[env.plugin.transfers.active_job]
    assert job["status"] == "failed" and "当前更新源已变化" in job["error"]
    market.assert_not_awaited()
    repository.assert_not_awaited()
    env.manager._terminate_plugin.assert_not_awaited()


@pytest.mark.parametrize("change", ["source", "version", "identity"])
async def test_changes_during_current_source_check_refuse_update(chat_plugin, change):
    env, target, market, repository = chat_plugin
    remote = metadata(target, "1.0.0")

    async def fetch(**kwargs):
        if change == "source":
            bind(env, target, "repository")
        elif change == "version":
            target.version = "1.5.0"
        else:
            target.author = "Changed"
        return {target.plugin_id: remote}, None

    market.side_effect = fetch
    event = env.event("/pm update demo")
    await env.scheduler.execute(event)
    assert "变化" in output(event)
    assert "已是最新版本" not in output(event) and "已更新至" not in output(event)
    repository.assert_not_awaited()
    env.manager._terminate_plugin.assert_not_awaited()


async def test_changed_local_metadata_requires_sync_before_network(chat_plugin):
    env, target, market, repository = chat_plugin
    await env.plugin.sources.detail(target.root_dir_name)
    target.display_name = "已修改名称"
    event = env.event("/pm update demo")
    await env.scheduler.execute(event)
    assert "更新信息" in output(event)
    market.assert_not_awaited()
    repository.assert_not_awaited()
    assert not env.plugin.transfers.jobs


async def test_direct_update_rejects_busy_task_without_extra_network(chat_plugin):
    env, _, market, repository = chat_plugin
    async with env.plugin._operations:
        started = await env.plugin.chat_updates.start_current_update("first", "demo")
        event = env.event("/pm update demo")
        await env.scheduler.execute(event)
        assert "已有插件管理任务" in output(event)
        assert list(env.plugin.transfers.jobs) == [started["id"]]
        market.assert_not_awaited()
        repository.assert_not_awaited()
    market.side_effect = ValueError("expected check failure")
    await env.plugin.transfers.task


async def test_direct_update_reports_failures_after_download(chat_plugin, monkeypatch):
    env, _, _, _ = chat_plugin
    monkeypatch.setattr(
        env.plugin.service,
        "update_target",
        AsyncMock(side_effect=ValueError("下载失败")),
    )
    event = env.event("/pm update demo")
    await env.scheduler.execute(event)
    assert "下载失败" in output(event) and "可能已修改插件文件" in output(event)
    assert not env.plugin.transfers.in_critical


async def test_plugin_shutdown_cancels_chat_check_and_returns_interruption(chat_plugin):
    env, _, market, _ = chat_plugin
    started, waiting = asyncio.Event(), asyncio.Event()

    async def fetch(**kwargs):
        started.set()
        await waiting.wait()

    market.side_effect = fetch
    event = env.event("/pm check-update")
    handling = asyncio.create_task(env.scheduler.execute(event))
    await asyncio.wait_for(started.wait(), 2)
    await env.plugin.terminate()
    await asyncio.wait_for(handling, 2)
    assert "中断" in output(event)
    assert env.plugin.chat_updates.task.done()
    assert event.is_stopped() and not env.model_calls
