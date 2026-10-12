"""Verify official registry searches, exact identities and installation sources."""

import asyncio
import shlex
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from astrbot.core.star.star_manager import PluginVersionUnsupportedError
from astrbot.dashboard.services.plugin_service import PluginService, PluginServiceError

MARKET_ID = "alice/astrbot_plugin_weather"
REPOSITORY = "https://github.com/different-owner/weather-repository"
DOWNLOAD_URL = "https://downloads.example.invalid/weather-v1.2.0.zip"


@pytest.fixture(autouse=True)
def market_cache(monkeypatch, tmp_path):
    """Keep AstrBot's real cache reader and writer inside each test's directory."""
    monkeypatch.setattr(
        "astrbot.dashboard.services.plugin_service.get_astrbot_data_path",
        lambda: str(tmp_path),
    )
    return Path(PluginService.build_registry_source(None).cache_file)


def market_data(
    *, author="alice", name="astrbot_plugin_weather", download_url=DOWNLOAD_URL
):
    return {
        "$meta": {"schema_version": 2},
        f"{author}/{name}": {
            "author": author,
            "name": name,
            "display_name": "天气助手",
            "desc": "每日天气预报",
            "tags": ["weather", "forecast"],
            "version": "1.2.0",
            "stars": 42,
            "download_count": 1234,
            "repo": REPOSITORY,
            "download_url": download_url,
        },
    }


def output(event):
    return "\n".join(event.sent)


def mock_registry(env, monkeypatch, data=None, notice=None):
    fetch = AsyncMock(return_value=(market_data() if data is None else data, notice))
    monkeypatch.setattr(env.plugin.service.web, "get_online_plugins", fetch)
    return fetch


@pytest.mark.parametrize("download_url", [DOWNLOAD_URL, ""])
@pytest.mark.parametrize("author", ["alice", "alice, Coauthor"])
async def test_market_install_uses_official_service_and_records_source(
    env, monkeypatch, download_url, author
):
    target = env.add_target("astrbot_plugin_weather", "weather_directory")
    target.author = author
    env.manager.install_plugin.return_value = {"name": target.name, "repo": REPOSITORY}
    fetch = mock_registry(env, monkeypatch, market_data(download_url=download_url))
    monkeypatch.setattr("astrbot.dashboard.services.plugin_service.sp", env.preferences)
    event = env.event(f"/pm add {MARKET_ID}")
    await env.scheduler.execute(event)
    fetch.assert_awaited_once_with(custom_registry=None, force_refresh=False)
    env.manager.install_plugin.assert_awaited_once_with(
        REPOSITORY,
        "",
        ignore_version_check=False,
        download_url=download_url,
    )
    record = env.prefs["plugin_install_sources"][target.root_dir_name]
    assert record["install_method"] == "market"
    assert record["market_plugin_id"] == MARKET_ID
    assert record["registry_url"] is None
    assert record["repo"] == REPOSITORY
    assert record["download_url"] == download_url
    assert target.author == author
    assert (await env.plugin.sources.detail(target.root_dir_name))["local"][
        "author"
    ] == author
    assert target.plugin_id == author.lower() + "/" + target.name
    assert "已安装" in output(event)
    env.sync.assert_awaited_once()
    assert event.is_stopped() and not env.model_calls


@pytest.mark.parametrize(
    "identifier",
    [
        "https://github.com/alice/astrbot_plugin_weather",
        "https://github.com/alice/astrbot_plugin_weather/tree/dev",
        "https://gitlab.com/team/subgroup/weather.git",
        "ssh://git@example.com/team/weather.git",
        "git@github.com:alice/weather.git",
    ],
)
async def test_full_repository_addresses_do_not_access_the_market(
    env, monkeypatch, identifier
):
    target = env.add_target()
    fetch = mock_registry(env, monkeypatch)
    install = AsyncMock(return_value=({"name": target.name}, "安装成功"))
    monkeypatch.setattr(env.plugin.service.web, "install_plugin", install)
    event = env.event(f"/pm add {identifier}")
    await env.scheduler.execute(event)
    fetch.assert_not_awaited()
    install.assert_awaited_once_with({"url": identifier, "ignore_version_check": False})
    assert "已安装" in output(event)


@pytest.mark.parametrize(
    "env",
    [{"allow_prefix_omission": True}, {"allow_prefix_omission": False}],
    indirect=True,
)
@pytest.mark.parametrize(
    "identifier",
    ["alice/weather", "ALICE/astrbot_plugin_weather", "bob/astrbot_plugin_weather"],
)
async def test_market_id_is_exact_and_never_falls_back_to_a_repository(
    env, monkeypatch, identifier
):
    fetch = mock_registry(env, monkeypatch)
    event = env.event(f"/pm add {identifier}")
    await env.scheduler.execute(event)
    fetch.assert_awaited_once_with(custom_registry=None, force_refresh=False)
    env.manager.install_plugin.assert_not_awaited()
    assert "未找到" in output(event)
    assert "已安装" not in output(event)
    assert not env.model_calls and event.is_stopped()


@pytest.mark.parametrize(
    "env",
    [{"allow_prefix_omission": True}, {"allow_prefix_omission": False}],
    indirect=True,
)
async def test_market_payload_preserves_the_entire_id(env, monkeypatch):
    target = env.add_target("astrbot_plugin_weather", "weather_directory")
    target.author = "alice"
    install = AsyncMock(return_value=({"name": target.name}, "安装成功"))
    monkeypatch.setattr(env.plugin.service.web, "install_plugin", install)
    event = env.event(f"/pm add {MARKET_ID}")
    await env.scheduler.execute(event)
    install.assert_awaited_once_with(
        {
            "install_method": "market",
            "registry_url": None,
            "market_plugin_id": MARKET_ID,
            "ignore_version_check": False,
        }
    )
    assert "已安装" in output(event)


@pytest.mark.parametrize(
    "arguments",
    [
        "add",
        "add alice",
        "add alice//weather",
        "add alice/weather/",
        "add /alice/weather",
        "add alice/weather/extra",
        "add alice/weather --force",
        "add market alice/weather",
        "add alice/weather extra",
        'add "alice/weather',
        'add ""',
        'add " alice/weather"',
        'add "alice/ weather"',
        "add file:///tmp/weather",
        "add ftp://example.com/team/weather",
        "add https://alice:secret@example.com/team/weather",
        "add https://example.com/weather.zip",
        "add deploy@example.com:team/weather.git",
    ],
)
async def test_invalid_install_syntax_is_rejected_before_installing(
    env, monkeypatch, arguments
):
    install = AsyncMock()
    fetch = mock_registry(env, monkeypatch)
    monkeypatch.setattr(env.plugin.service.web, "install_plugin", install)
    event = env.event()
    await env.invoke(event, arguments)
    assert "用法" in output(event) or "仓库地址无效" in output(event)
    assert "secret" not in output(event)
    install.assert_not_awaited()
    fetch.assert_not_awaited()
    assert event.is_stopped()


@pytest.mark.parametrize(
    "author,name", [("other", "astrbot_plugin_weather"), ("alice", "weather")]
)
async def test_installed_market_identity_must_match_the_request(
    env, monkeypatch, author, name
):
    target = env.add_target(name, "weather_directory")
    target.author = author
    install = AsyncMock(return_value=({"name": target.name}, "安装成功"))
    monkeypatch.setattr(env.plugin.service.web, "install_plugin", install)
    event = env.event(f"/pm add {MARKET_ID}")
    await env.scheduler.execute(event)
    assert "与市场 ID 不一致" in output(event)
    assert "部分变更" in output(event) and "WebUI" in output(event)
    assert "已安装" not in output(event)
    assert install.await_count == 1


async def test_market_install_retains_core_version_rejection(env, monkeypatch):
    mock_registry(env, monkeypatch)
    env.manager.install_plugin.side_effect = PluginVersionUnsupportedError(
        "version mismatch"
    )
    event = env.event(f"/pm add {MARKET_ID}")
    await env.scheduler.execute(event)
    assert "当前 AstrBot 版本不满足插件要求" in output(event)
    assert "已安装" not in output(event)
    assert env.manager.install_plugin.await_args.kwargs["ignore_version_check"] is False
    assert env.manager.install_plugin.await_count == 1
    env.sync.assert_not_awaited()


@pytest.mark.parametrize("keyword", ["天气", "WEATHER", "alice", "每日", "FORECAST"])
async def test_market_search_matches_metadata_without_mutating_plugins(
    env, monkeypatch, keyword
):
    fetch = mock_registry(env, monkeypatch)
    event = env.event(f"/pm search {keyword}", group="group")
    await env.scheduler.execute(event)
    text = output(event)
    assert "官方插件市场" in text and "共 1 项" in text
    assert "天气助手｜1.2.0" in text
    assert f"市场 ID：{MARKET_ID}" in text
    assert "⭐ Star：42｜下载量：1234" in text
    assert "简介：每日天气预报" in text
    assert f"安装：/pm add {MARKET_ID}" in text
    assert "different-owner" not in text
    fetch.assert_awaited_once_with(custom_registry=None, force_refresh=False)
    env.manager.install_plugin.assert_not_awaited()
    env.manager.load.assert_not_awaited()
    env.preferences.global_put.assert_not_awaited()
    assert event.is_stopped() and not env.model_calls


async def test_search_supports_multiword_keywords_and_explicit_paging(env, monkeypatch):
    data = market_data()
    data[MARKET_ID]["desc"] = "Weather Station plugin"
    mock_registry(env, monkeypatch, data)
    event = env.event("/pm search weather station --page 1")
    await env.scheduler.execute(event)
    assert "共 1 项" in output(event)


async def test_market_results_are_sorted_and_paginated_with_copyable_commands(
    env, monkeypatch
):
    data = {"$meta": {"schema_version": 2}}
    for index in reversed(range(23)):
        name = f"astrbot_plugin_weather_{index:02}"
        data.update(market_data(name=name))
    mock_registry(env, monkeypatch, data)
    first = env.event("/pm search weather")
    await env.scheduler.execute(first)
    text = output(first)
    assert "共 23 项｜第 1/2 页" in text
    assert text.count("安装：/pm add ") == 20
    assert "1. 天气助手" in text and "10. 天气助手" in text
    assert "astrbot_plugin_weather_00" in text
    assert "astrbot_plugin_weather_20" not in text
    assert "下一页：/pm search weather --page 2" in text
    second = env.event("/pm search weather --page 2")
    await env.scheduler.execute(second)
    text = output(second)
    assert "第 2/2 页" in text
    assert text.count("安装：/pm add ") == 3
    assert "21. 天气助手" in text
    assert "astrbot_plugin_weather_19" not in text
    assert "下一页" not in text


async def test_search_and_paging_read_shared_cache_without_network(env, monkeypatch):
    data = {}
    for index in range(23):
        data.update(market_data(name=f"astrbot_plugin_weather_{index:02}"))
    source = env.plugin.service.web.build_registry_source(None)
    env.plugin.service.web.save_plugin_cache(source.cache_file, data)
    fetch = mock_registry(env, monkeypatch)
    fetch.side_effect = AssertionError("Cached searches must not request the registry")
    md5 = AsyncMock(side_effect=AssertionError("Cached searches must not check MD5"))
    monkeypatch.setattr(env.plugin.service.web, "fetch_remote_md5", md5)

    first = env.event("/pm search weather")
    await env.scheduler.execute(first)
    assert "共 23 项｜第 1/2 页" in output(first)
    assert "AstrBot 本地市场缓存" in output(first)
    assert "/pm refresh-market" in output(first)

    data["alice/astrbot_plugin_weather_20"]["version"] = "2.0.0"
    env.plugin.service.web.save_plugin_cache(source.cache_file, data)
    second = env.event("/pm search weather --page 2")
    await env.scheduler.execute(second)
    assert "第 2/2 页" in output(second)
    assert output(second).count("安装：/pm add ") == 3
    assert "天气助手｜2.0.0" in output(second)
    fetch.assert_not_awaited()
    md5.assert_not_awaited()
    env.manager.install_plugin.assert_not_awaited()
    env.manager.load.assert_not_awaited()


async def test_cached_search_without_matches_does_not_request_network(env, monkeypatch):
    source = env.plugin.service.web.build_registry_source(None)
    env.plugin.service.web.save_plugin_cache(source.cache_file, market_data())
    fetch = mock_registry(env, monkeypatch)
    fetch.side_effect = AssertionError("No search matches must not trigger a request")
    event = env.event("/pm search unknown")
    await env.scheduler.execute(event)
    assert "共 0 项｜第 1/1 页" in output(event)
    assert "没有找到匹配的插件" in output(event)
    fetch.assert_not_awaited()


@pytest.mark.parametrize(
    "contents",
    [None, "{}", "{broken", '{"timestamp": "2000-01-01", "data": {}}'],
)
async def test_unavailable_cache_fetches_once_then_reuses_core_cache(
    env, monkeypatch, market_cache, contents
):
    if contents is not None:
        market_cache.write_text(contents, encoding="utf-8")

    async def fetch_and_cache(*, custom_registry, force_refresh):
        data = market_data()
        env.plugin.service.web.save_plugin_cache(str(market_cache), data)
        return data, None

    fetch = AsyncMock(side_effect=fetch_and_cache)
    monkeypatch.setattr(env.plugin.service.web, "get_online_plugins", fetch)
    first = env.event("/pm search weather")
    await env.scheduler.execute(first)
    assert "共 1 项" in output(first)
    assert "本地市场缓存" not in output(first)
    second = env.event("/pm search weather --page 1")
    await env.scheduler.execute(second)
    assert "共 1 项" in output(second)
    assert "本地市场缓存" in output(second)
    fetch.assert_awaited_once_with(custom_registry=None, force_refresh=False)


async def test_manual_refresh_updates_shared_cache_for_search(
    env, monkeypatch, market_cache
):
    env.plugin.service.web.save_plugin_cache(str(market_cache), market_data())
    updated = market_data()
    updated[MARKET_ID]["version"] = "2.0.0"

    async def fetch_and_cache(*, custom_registry, force_refresh):
        env.plugin.service.web.save_plugin_cache(str(market_cache), updated)
        return updated, None

    fetch = AsyncMock(side_effect=fetch_and_cache)
    monkeypatch.setattr(env.plugin.service.web, "get_online_plugins", fetch)
    refresh = env.event("/pm refresh-market", group="group")
    await env.scheduler.execute(refresh)
    assert "官方插件市场已刷新" in output(refresh)
    search = env.event("/pm search weather")
    await env.scheduler.execute(search)
    assert "天气助手｜2.0.0" in output(search)
    fetch.assert_awaited_once_with(custom_registry=None, force_refresh=True)
    env.manager.install_plugin.assert_not_awaited()
    env.manager.load.assert_not_awaited()
    assert refresh.is_stopped() and not env.model_calls


@pytest.mark.parametrize(
    "failure",
    [
        None,
        asyncio.TimeoutError(),
        PluginServiceError("registry unavailable", public_message="无法获取插件市场"),
        RuntimeError("unexpected registry failure"),
    ],
)
async def test_refresh_failure_does_not_report_success_and_keeps_cache(
    env, monkeypatch, market_cache, failure
):
    env.plugin.service.web.save_plugin_cache(str(market_cache), market_data())
    fetch = mock_registry(env, monkeypatch, notice="使用缓存数据，可能不是最新版本")
    fetch.side_effect = failure
    refresh = env.event("/pm refresh-market")
    await env.scheduler.execute(refresh)
    text = output(refresh)
    assert "未完成" in text or "查询失败" in text
    assert "官方插件市场已刷新" not in text
    assert "部分变更" not in text and "插件状态" not in text
    if isinstance(failure, asyncio.TimeoutError):
        assert "响应超时" in text
    search = env.event("/pm search weather")
    await env.scheduler.execute(search)
    assert "天气助手｜1.2.0" in output(search)
    fetch.assert_awaited_once_with(custom_registry=None, force_refresh=True)


@pytest.mark.parametrize(
    "arguments", ["refresh-market extra", "refresh-market --force"]
)
async def test_refresh_rejects_arguments_before_requesting_network(
    env, monkeypatch, arguments
):
    fetch = mock_registry(env, monkeypatch)
    event = env.event()
    await env.invoke(event, arguments)
    assert "用法：/pm refresh-market" in output(event)
    fetch.assert_not_awaited()


async def test_refresh_reports_core_cache_write_failure(env, monkeypatch, market_cache):
    env.plugin.service.web.save_plugin_cache(str(market_cache), market_data())
    updated = market_data()
    updated[MARKET_ID]["version"] = "2.0.0"
    fetch = mock_registry(env, monkeypatch, updated)
    refresh = env.event("/pm refresh-market")
    await env.scheduler.execute(refresh)
    assert "AstrBot 缓存未更新" in output(refresh)
    assert "官方插件市场已刷新" not in output(refresh)
    search = env.event("/pm search weather")
    await env.scheduler.execute(search)
    assert "天气助手｜1.2.0" in output(search)
    fetch.assert_awaited_once_with(custom_registry=None, force_refresh=True)


@pytest.mark.parametrize(
    "command,force_refresh",
    [("/pm search weather", False), ("/pm refresh-market", True)],
)
async def test_network_market_requests_are_cancelled_on_timeout(
    env, monkeypatch, command, force_refresh
):
    async def stalled_request(*, custom_registry, force_refresh):
        await asyncio.Event().wait()

    fetch = AsyncMock(side_effect=stalled_request)
    monkeypatch.setattr(env.plugin.service.web, "get_online_plugins", fetch)
    monkeypatch.setattr(env.service_module, "MARKET_QUERY_TIMEOUT_SECONDS", 0.01)
    event = env.event(command)
    await env.scheduler.execute(event)
    assert "响应超时" in output(event)
    assert "部分变更" not in output(event)
    fetch.assert_awaited_once_with(custom_registry=None, force_refresh=force_refresh)


@pytest.mark.parametrize(
    "arguments",
    [
        "search",
        "search --page 1",
        "search weather --page",
        "search weather --page 0",
        "search weather --page -1",
        "search weather --page １",
        "search weather --page nope",
        "search weather --page 1 extra",
        "search --page 1 weather",
        "search weather --page 1 --page 2",
        "search weather --source unofficial",
    ],
)
async def test_invalid_market_search_arguments_do_not_fetch_data(
    env, monkeypatch, arguments
):
    fetch = mock_registry(env, monkeypatch)
    event = env.event()
    await env.invoke(event, arguments)
    assert "用法：/pm search" in output(event)
    fetch.assert_not_awaited()
    assert event.is_stopped() and not env.model_calls


async def test_search_empty_results_cache_notice_and_out_of_range_page(
    env, monkeypatch
):
    mock_registry(env, monkeypatch, notice="使用缓存数据，可能不是最新版本")
    empty = env.event("/pm search unknown")
    await env.scheduler.execute(empty)
    assert "共 0 项｜第 1/1 页" in output(empty)
    assert "没有找到匹配" in output(empty)
    assert "使用缓存数据，可能不是最新版本" in output(empty)
    overflow = env.event("/pm search weather --page 2")
    await env.scheduler.execute(overflow)
    assert "页码超出范围，共 1 页" in output(overflow)


@pytest.mark.parametrize("author", ["Alice Smith", "Alice's Lab"])
async def test_search_commands_quote_ids_and_can_be_used_for_installation(
    env, monkeypatch, author
):
    identifier = f"{author}/astrbot_plugin_weather"
    mock_registry(env, monkeypatch, market_data(author=author))
    search = env.event("/pm search weather")
    await env.scheduler.execute(search)
    command = next(
        line.removeprefix("安装：")
        for line in output(search).splitlines()
        if line.startswith("安装：")
    )
    assert shlex.split(command) == ["/pm", "add", identifier]
    target = env.add_target("astrbot_plugin_weather", "weather_directory")
    target.author = author
    install = AsyncMock(return_value=({"name": target.name}, "安装成功"))
    monkeypatch.setattr(env.plugin.service.web, "install_plugin", install)
    event = env.event(command)
    await env.scheduler.execute(event)
    assert install.await_args.args[0]["market_plugin_id"] == identifier
    assert "已安装" in output(event)


async def test_search_sanitizes_metadata_redacts_credentials_and_bounds_description(
    env, monkeypatch
):
    data = market_data()
    data[MARKET_ID]["display_name"] = "天气助手\n/pm remove other"
    data[MARKET_ID]["desc"] = "api_key=SECRET_MARKET_CREDENTIAL\n" + "天气" * 300
    data.update(market_data(author="bad\nauthor"))
    mock_registry(env, monkeypatch, data)
    event = env.event("/pm search weather")
    await env.scheduler.execute(event)
    text = output(event)
    assert "共 1 项" in text
    assert "SECRET_MARKET_CREDENTIAL" not in text and "[已隐藏]" in text
    assert "\n/pm remove other" not in text
    description = next(line for line in text.splitlines() if line.startswith("简介："))
    assert len(description.removeprefix("简介：")) <= 160


@pytest.mark.parametrize(
    "failure",
    [
        PluginServiceError("registry unavailable", public_message="无法获取插件市场"),
        RuntimeError("unexpected registry failure"),
    ],
)
async def test_search_failure_is_reported_as_a_read_failure(env, monkeypatch, failure):
    fetch = mock_registry(env, monkeypatch)
    fetch.side_effect = failure
    event = env.event("/pm search weather")
    await env.scheduler.execute(event)
    text = output(event)
    assert "查询未完成" in text or "查询失败" in text
    assert "部分变更" not in text and "插件状态" not in text
    env.manager.install_plugin.assert_not_awaited()
    assert event.is_stopped() and not env.model_calls


async def test_demo_mode_allows_market_search_but_rejects_market_install(
    env, monkeypatch
):
    monkeypatch.setattr("astrbot.dashboard.services.plugin_service.DEMO_MODE", True)
    fetch = mock_registry(env, monkeypatch)
    search = env.event("/pm search weather")
    await env.scheduler.execute(search)
    assert "共 1 项" in output(search)
    fetch.assert_awaited_once()
    fetch.reset_mock()
    install = env.event(f"/pm add {MARKET_ID}")
    await env.scheduler.execute(install)
    assert "demo mode" in output(install)
    fetch.assert_not_awaited()
    env.manager.install_plugin.assert_not_awaited()
