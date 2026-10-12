"""Check each current source and guard explicit updates against stale identities."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def source(target, kind="market", **changes):
    return {
        "root_dir_name": target.root_dir_name,
        "name": target.name,
        "author": target.author,
        "install_method": kind,
        "registry_url": None,
        "market_plugin_id": f"{target.author}/{target.name}"
        if kind == "market"
        else "",
        "repo": "https://github.com/test/" + target.name,
        "installed_at": "2025-01-01T00:00:00Z",
        **changes,
    }


def metadata(target, **changes):
    return {
        "name": target.name,
        "author": target.author,
        "version": "2.0.0",
        "repo": "https://github.com/test/" + target.name,
        **changes,
    }


@pytest.fixture
async def registered(env, monkeypatch):
    target = env.add_target()
    env.prefs["plugin_install_sources"] = {target.root_dir_name: source(target)}
    market = AsyncMock(return_value=({target.plugin_id: metadata(target)}, None))
    repository = AsyncMock(return_value=metadata(target))
    monkeypatch.setattr(env.plugin.service.web, "get_online_plugins", market)
    monkeypatch.setattr(
        env.manager, "inspect_plugin_repository", repository, raising=False
    )
    await env.plugin.sources.detail(target.root_dir_name)
    return env, target, market, repository


async def check(env):
    await env.plugin.updates.start_check("admin")
    await env.plugin.updates.task
    return env.plugin.updates.state("admin")


async def associate(env, url):
    target = env.add_target(name="repository_demo", directory="repository_demo")
    env.prefs["plugin_install_sources"][target.root_dir_name] = source(
        target, "repository", repo=url
    )
    env.manager.inspect_plugin_repository.return_value = metadata(target, repo=url)
    return await env.plugin.sources.detail(target.root_dir_name)


async def queue_update(env, report):
    entry = next(item for item in report["results"] if item["can_update"])
    job = await env.plugin.updates.start_update(
        "admin", report["id"], entry["directory"], entry["binding_id"], True
    )
    await env.plugin.transfers.task
    return env.plugin.transfers.get_job(job["id"], "admin")


async def test_checks_only_current_sources_and_deduplicates_market(registered):
    env, target, market, repository = registered
    original = await env.plugin.sources.detail(target.root_dir_name)
    repo_record = await associate(env, "https://github.com/test/repository_demo")
    second = env.add_target(name="second", directory="second")
    env.prefs["plugin_install_sources"][second.root_dir_name] = source(second)
    market.return_value = (
        {target.plugin_id: metadata(target), second.plugin_id: metadata(second)},
        None,
    )
    report = await check(env)
    assert report["status"] == "completed"
    assert report["done"] == report["total"] == 3
    assert all(
        item["status"] == "update_available" and item["can_update"]
        for item in report["results"]
    )
    repo_result = next(
        item for item in report["results"] if item["kind"] == "repository"
    )
    assert repo_result["revision"] == repo_record["revision"]
    market.assert_awaited_once_with(custom_registry=None, force_refresh=True)
    repository.assert_awaited_once_with("https://github.com/test/repository_demo")
    current = await env.plugin.sources.detail(target.root_dir_name)
    assert current["current_source"] == original["current_source"]
    assert current["provenance"] == original["provenance"]
    assert not any(key.startswith("_") for item in report["results"] for key in item)
    assert env.plugin.updates.state("other")["status"] == "idle"


async def test_custom_markets_are_checked_independently(registered):
    env, target, market, _ = registered
    second = env.add_target(name="second", directory="second")
    env.prefs["plugin_install_sources"][second.root_dir_name] = source(
        second, registry_url="https://market.example/index.json"
    )

    async def fetch(*, custom_registry, force_refresh):
        assert force_refresh is True
        plugin = second if custom_registry else target
        return {plugin.plugin_id: metadata(plugin)}, None

    market.side_effect = fetch
    report = await check(env)
    assert len(report["results"]) == 2
    assert all(item["can_update"] for item in report["results"])
    assert market.await_count == 2


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"author": "Impostor"},
        {"author": "Soul"},
        {"name": "other"},
        {"repo": "https://github.com/impostor/essential"},
    ],
)
async def test_essential_market_author_is_one_of_full_local_authors(
    env, monkeypatch, change
):
    target = env.add_target(name="astrbot_plugin_essential")
    target.author = "Soulter,FateTrial"
    repo = "https://github.com/Soulter/astrbot_plugin_essential"
    market_id = "Soulter/astrbot_plugin_essential"
    env.prefs["plugin_install_sources"] = {
        target.root_dir_name: source(target, repo=repo, market_plugin_id=market_id)
    }
    remote = {**metadata(target, author="Soulter", repo=repo), **change}
    monkeypatch.setattr(
        env.plugin.service.web,
        "get_online_plugins",
        AsyncMock(return_value=({market_id: remote}, None)),
    )
    result = (await check(env))["results"][0]
    assert result["status"] == ("error" if change else "update_available")
    assert result["can_update"] is (not change)


@pytest.mark.parametrize(
    "remote, status",
    [
        ("1.0.0", "no_update"),
        ("0.9", "no_update"),
        ("v1.0.1", "update_available"),
        ("2.0rc1", "update_available"),
        ("latest", "error"),
        ("", "error"),
    ],
)
async def test_version_only_comparison(registered, remote, status):
    env, target, market, _ = registered
    market.return_value = ({target.plugin_id: metadata(target, version=remote)}, None)
    result = (await check(env))["results"][0]
    assert result["status"] == status
    assert result["can_update"] is (status == "update_available")


@pytest.mark.parametrize(
    "changes",
    [
        {"author": "Impostor"},
        {"name": "another"},
        {"repo": "https://github.com/test/other"},
    ],
)
async def test_remote_identity_or_repository_mismatch_fails(registered, changes):
    env, target, market, _ = registered
    market.return_value = ({target.plugin_id: metadata(target, **changes)}, None)
    result = (await check(env))["results"][0]
    assert result["status"] == "error" and not result["can_update"]


@pytest.mark.parametrize("failure", ["cache", "missing", "timeout", "network"])
async def test_one_failed_source_does_not_interrupt_other_plugins(registered, failure):
    env, target, market, _ = registered
    await associate(env, "https://github.com/test/demo_plugin")
    if failure == "cache":
        market.return_value = ({target.plugin_id: metadata(target)}, "offline cache")
    elif failure == "missing":
        market.return_value = ({}, None)
    else:
        market.side_effect = (
            asyncio.TimeoutError() if failure == "timeout" else RuntimeError("offline")
        )
    report = await check(env)
    assert report["status"] == "completed" and report["done"] == 2
    failed, available = report["results"]
    assert failed["status"] == "error" and not failed["can_update"]
    assert available["status"] == "update_available" and available["can_update"]


async def test_check_limits_concurrency_and_close_cancels_requests(env, monkeypatch):
    started = asyncio.Event()
    waiting = asyncio.Event()
    concurrent = 0
    cancelled = 0

    async def fetch(url):
        nonlocal concurrent, cancelled
        concurrent += 1
        if concurrent == 3:
            started.set()
        try:
            await waiting.wait()
        finally:
            concurrent -= 1
            cancelled += 1

    monkeypatch.setattr(
        env.manager,
        "inspect_plugin_repository",
        AsyncMock(side_effect=fetch),
        raising=False,
    )
    for index in range(5):
        target = env.add_target(name=f"probe_{index}", directory=f"probe_{index}")
        env.prefs.setdefault("plugin_install_sources", {})[target.root_dir_name] = (
            source(target, "repository")
        )
    await env.plugin.updates.start_check("admin")
    await asyncio.wait_for(started.wait(), 2)
    assert concurrent == 3
    with pytest.raises(ValueError, match="已有来源检查"):
        await env.plugin.updates.start_check("admin")
    await env.plugin.updates.close()
    assert concurrent == 0 and cancelled == 3
    assert env.plugin.updates.state("admin")["status"] == "interrupted"


@pytest.mark.parametrize("invalid", ["owner", "report", "confirmation", "inactive"])
async def test_update_requires_owned_completed_report_confirmation_and_active_source(
    registered, invalid
):
    env, _, _, _ = registered
    report = await check(env)
    entry = report["results"][0]
    with pytest.raises(ValueError):
        await env.plugin.updates.start_update(
            "other" if invalid == "owner" else "admin",
            "old-report" if invalid == "report" else report["id"],
            entry["directory"],
            "unrelated-source" if invalid == "inactive" else entry["binding_id"],
            invalid != "confirmation",
        )
    assert not env.plugin.transfers.jobs


@pytest.mark.parametrize(
    "change",
    [
        "source",
        "local_version",
        "identity",
        "remote_version",
        "remote_identity",
        "download_url",
    ],
)
async def test_changes_since_check_refuse_update_before_download(
    registered, monkeypatch, change
):
    env, target, market, _ = registered
    report = await check(env)
    if change == "source":
        env.prefs["plugin_install_sources"][target.root_dir_name] = source(
            target, "repository"
        )
    elif change == "local_version":
        target.version = "1.5.0"
    elif change == "identity":
        target.author = "Changed"
    else:
        changes = {
            "remote_version": {"version": "3.0.0"},
            "remote_identity": {"author": "Impostor"},
            "download_url": {"download_url": "https://example.com/new.zip"},
        }[change]
        market.return_value = ({target.plugin_id: metadata(target, **changes)}, None)
    update = AsyncMock(side_effect=AssertionError("stale update must not download"))
    monkeypatch.setattr(env.plugin.service, "update_target", update)
    job = await queue_update(env, report)
    assert job["status"] == "failed"
    update.assert_not_awaited()
    assert "可能已修改" not in job["error"]


async def test_successful_update_preserves_provenance_and_records_current_source(
    registered, monkeypatch
):
    env, target, market, _ = registered
    original = await env.plugin.sources.detail(target.root_dir_name)
    report = await check(env)

    async def update(captured, *, repo_url, download_url, source_revision):
        assert env.plugin.transfers.operation_lock.locked()
        assert env.plugin.transfers.in_critical
        assert captured.plugin is target
        assert repo_url == source(target)["repo"] and download_url == ""
        assert source_revision == original["revision"]
        current = copy.copy(target)
        current.version = "2.0.0"
        env.owners[target.module_path] = current
        return current

    monkeypatch.setattr(
        env.plugin.service, "update_target", AsyncMock(side_effect=update)
    )
    job = await queue_update(env, report)
    assert job["status"] == "completed" and job["kind"] == "plugin_update"
    assert job["results"][0]["status"] == "updated"
    current = await env.plugin.sources.detail(target.root_dir_name)
    assert current["provenance"] == original["provenance"]
    assert current["current_source"] == original["current_source"]
    assert current["metadata_snapshot"]["version"] == "2.0.0"
    assert not current["metadata_changed"]
    assert current["history"][-1]["action"] == "updated"
    assert (
        env.prefs["plugin_install_sources"][target.root_dir_name]["installed_at"]
        == "2025-01-01T00:00:00Z"
    )
    assert env.plugin.updates.state("admin")["results"][0]["status"] == "updated"
    assert "update_plan" not in env.plugin.transfers.public_job(job)
    assert market.await_count == 2
    assert not env.plugin.transfers.in_critical


async def test_download_failure_marks_job_and_check_result(registered, monkeypatch):
    env, _, _, _ = registered
    report = await check(env)
    monkeypatch.setattr(
        env.plugin.service,
        "update_target",
        AsyncMock(side_effect=ValueError("下载失败")),
    )
    job = await queue_update(env, report)
    assert job["status"] == "failed" and "下载失败" in job["error"]
    assert "可能已修改插件文件" in job["error"]
    result = env.plugin.updates.state("admin")["results"][0]
    assert result["status"] == "error" and not result["can_update"]
    assert not env.plugin.transfers.in_critical


async def test_market_resolves_repo_from_exact_id_when_core_record_has_no_repo(
    registered, monkeypatch
):
    env, target, _, _ = registered
    env.prefs["plugin_install_sources"][target.root_dir_name]["repo"] = ""
    target.repo = "https://github.com/unrelated/wrong-repository"
    record = await env.plugin.sources.detail(target.root_dir_name)
    await env.plugin.sources.sync_metadata(
        "admin", target.root_dir_name, record["revision"]
    )
    report = await check(env)

    async def update(captured, *, repo_url, **kwargs):
        assert repo_url == metadata(target)["repo"]
        target.version = "2.0.0"
        return target

    monkeypatch.setattr(
        env.plugin.service, "update_target", AsyncMock(side_effect=update)
    )
    job = await queue_update(env, report)
    assert job["status"] == "completed", job["error"]


async def test_protected_or_changed_plugins_cannot_be_updated(registered):
    env, target, _, _ = registered
    target.reserved = True
    result = (await check(env))["results"][0]
    assert result["status"] == "update_available" and not result["can_update"]


async def test_manager_itself_is_not_updatable(env, monkeypatch):
    target = env.context.get_registered_star("astrbot_plugin_manager")
    target.author = "Tester"
    target.version = "1.0.0"
    env.prefs["plugin_install_sources"] = {target.root_dir_name: source(target)}
    monkeypatch.setattr(
        env.plugin.service.web,
        "get_online_plugins",
        AsyncMock(return_value=({target.plugin_id: metadata(target)}, None)),
    )
    result = (await check(env))["results"][0]
    assert not result["can_update"] and "自身" in result["message"]


async def test_check_can_run_in_demo_but_update_cannot(registered, monkeypatch):
    env, _, _, _ = registered
    monkeypatch.setattr("astrbot.dashboard.services.plugin_service.DEMO_MODE", True)
    report = await check(env)
    assert report["results"][0]["can_update"]
    with pytest.raises(Exception, match="演示|demo"):
        await queue_update(env, report)
    assert not env.plugin.transfers.jobs


async def test_check_without_current_sources_completes_without_network(env, monkeypatch):
    fetch = AsyncMock(side_effect=AssertionError("no bindings"))
    monkeypatch.setattr(env.plugin.service.web, "get_online_plugins", fetch)
    report = await check(env)
    assert report["status"] == "completed" and report["total"] == 0
    fetch.assert_not_awaited()


async def test_source_change_while_waiting_for_core_lock_refuses_download(registered):
    env, target, _, _ = registered
    record = await env.plugin.sources.detail(target.root_dir_name)
    env.manager._updater = SimpleNamespace(update=AsyncMock())
    env.manager._ensure_plugin_requirements = AsyncMock()
    async with env.manager._pm_lock:
        task = asyncio.create_task(
            env.plugin.service.update_target(
                env.plugin.service.resolve(target.root_dir_name),
                repo_url=source(target)["repo"],
                download_url="",
                source_revision=record["revision"],
            )
        )
        await asyncio.sleep(0)
        env.prefs["plugin_install_sources"][target.root_dir_name] = source(
            target, "repository"
        )
    with pytest.raises(ValueError, match="当前更新源已变化"):
        await task
    env.manager._updater.update.assert_not_awaited()


async def test_close_waits_for_update_once_writing_has_started(registered, monkeypatch):
    env, target, _, _ = registered
    report = await check(env)
    started = asyncio.Event()
    release = asyncio.Event()

    async def update(captured, **kwargs):
        started.set()
        await release.wait()
        target.version = "2.0.0"
        return target

    monkeypatch.setattr(
        env.plugin.service, "update_target", AsyncMock(side_effect=update)
    )
    entry = report["results"][0]
    await env.plugin.updates.start_update(
        "admin", report["id"], entry["directory"], entry["binding_id"], True
    )
    await asyncio.wait_for(started.wait(), 2)
    closing = asyncio.create_task(env.plugin.transfers.close())
    await asyncio.sleep(0)
    assert not closing.done() and env.plugin.transfers.in_critical
    release.set()
    await closing
    assert env.plugin.transfers.history("admin")[0]["status"] == "completed"
