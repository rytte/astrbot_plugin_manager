"""Exercise manifest previews and the shared official installation workflow."""

import asyncio
import copy
import stat
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml
from astrbot.core.star.star_manager import PluginVersionUnsupportedError
from astrbot_plugin_manager.manifests import (
    MANIFEST_FORMAT,
    ManifestService,
    validate_manifest,
)
from astrbot_plugin_manager.market_archive import unpack_market_archive
from astrbot_plugin_manager.transfers import TransferService

NAME = "astrbot_plugin_shared"
AUTHOR = "Alice"
ID = f"{AUTHOR}/{NAME}"


def manifest():
    return {
        "format": MANIFEST_FORMAT,
        "schema_version": 1,
        "name": "分享清单",
        "plugins": [
            {
                "display_name": "分享插件",
                "market_plugin_id": ID,
                "repo_url": "",
            }
        ],
    }


def registry():
    return {
        ID: {
            "name": NAME,
            "author": AUTHOR,
            "version": "2.0.0",
            "repo": "https://github.com/alice/shared",
            "download_url": "https://cloud.astrbot.app/packages/shared.zip",
            "astrbot_version": ">=4.27,<5",
        }
    }


def write_archive(path, *, name=NAME, author=AUTHOR, version="2.0.0", requirement=""):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("plugin/", "")
        archive.writestr(
            "plugin/metadata.yaml",
            yaml.safe_dump(
                {
                    "name": name,
                    "author": author,
                    "version": version,
                    "desc": "Shared plugin",
                    "astrbot_version": ">=4.27,<5",
                }
            ),
        )
        archive.writestr("plugin/main.py", "raise RuntimeError('MUST NEVER EXECUTE')")
        if requirement:
            archive.writestr("plugin/requirements.txt", requirement)


@pytest.fixture
def market_mock(env, monkeypatch, core_installs):
    data = registry()
    fetch = AsyncMock(side_effect=lambda **kwargs: (copy.deepcopy(data), None))
    monkeypatch.setattr(env.plugin.service.web, "get_online_plugins", fetch)

    core_installs[data[ID]["download_url"]] = {
        "name": NAME,
        "author": AUTHOR,
        "version": "2.0.0",
    }
    return data, fetch, env.manager.install_plugin


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(schema_version=True),
        lambda value: value.update(code="malicious"),
        lambda value: value["plugins"][0].update(
            download_url="https://evil.example/code.zip"
        ),
        lambda value: value["plugins"][0].update(registry="custom"),
        lambda value: value["plugins"][0].update(version_policy="pinned"),
        lambda value: value["plugins"][0].update(name="../escaped"),
        lambda value: value["plugins"].append(copy.deepcopy(value["plugins"][0])),
    ],
)
def test_manifest_rejects_unknown_fields_arbitrary_downloads_and_other_sources(
    mutation,
):
    value = manifest()
    mutation(value)
    with pytest.raises(ValueError):
        validate_manifest(value)


@pytest.mark.parametrize("author", [AUTHOR, AUTHOR + ", Coauthor"])
async def test_manifest_install_uses_official_pipeline_and_reports_loaded_state(
    env, market_mock, core_installs, author
):
    data, fetch, installer = market_mock
    directory = NAME + "_source"
    core_installs[data[ID]["download_url"]]["directory"] = directory
    core_installs[data[ID]["download_url"]]["author"] = author
    service = env.plugin.manifests
    preview = await service.preview("admin", manifest())
    assert preview["plugins"][0]["sources"][0]["status"] == "install"
    assert "remote" not in preview["plugins"][0]["sources"][0]
    await service.start(
        "admin", preview["job"]["id"], [{"id": "0", "source": "market"}], True
    )
    await env.plugin.transfers.task
    job = env.plugin.transfers.get_job(preview["job"]["id"], "admin")
    assert job["status"] == "completed", job
    assert job["results"][0]["status"] == "installed"
    assert not job.get("reload_required")
    assert job["results"][0]["enabled"] is True
    assert "已启用" in job["results"][0]["message"]
    assert job["installed_artifacts"][0]["version"] == "2.0.0"
    code = Path(env.manager.plugin_store_path) / directory
    assert (code / "main.py").is_file()
    assert not env.prefs.get("inactivated_plugins")
    assert env.prefs["plugin_install_sources"][directory]["market_plugin_id"] == ID
    record = await env.plugin.sources.detail(directory)
    assert record["provenance"]["evidence"] == "manager_operation"
    assert record["local"]["status"] == "enabled"
    assert record["local"]["author"] == author
    assert record["history"][-1]["actor"] == "admin"
    installer.assert_awaited_once_with(
        data[ID]["repo"],
        "",
        ignore_version_check=False,
        download_url=data[ID]["download_url"],
    )
    assert env.context.get_registered_star(NAME).activated
    env.sync.assert_awaited_once()
    assert fetch.await_count == 3
    restarted = TransferService(env.plugin.service, env.plugin._operations)
    await restarted.initialize()
    assert restarted.history("admin")[0]["status"] == "completed"


async def test_market_without_archive_url_uses_official_repository_fallback(
    env, market_mock, core_installs
):
    data, _, installer = market_mock
    package = core_installs.pop(data[ID]["download_url"])
    data[ID]["download_url"] = ""
    core_installs[data[ID]["repo"]] = package
    preview = await env.plugin.manifests.preview("admin", manifest())
    assert preview["plugins"][0]["sources"][0]["status"] == "install"
    await env.plugin.manifests.start(
        "admin", preview["job"]["id"], [{"id": "0", "source": "market"}], True
    )
    await env.plugin.transfers.task
    assert (
        env.plugin.transfers.get_job(preview["job"]["id"], "admin")["status"]
        == "completed"
    )
    installer.assert_awaited_once_with(
        data[ID]["repo"], "", ignore_version_check=False, download_url=""
    )


@pytest.mark.parametrize("failure", ["missing_metadata", "uninitialized"])
async def test_incomplete_core_registration_is_never_reported_as_installed(
    env, market_mock, failure
):
    original = env.manager.install_plugin.side_effect

    async def install(*args, **kwargs):
        info = await original(*args, **kwargs)
        if failure == "missing_metadata":
            return {}
        env.context.get_registered_star(info["name"]).star_cls = None
        return info

    env.manager.install_plugin.side_effect = install
    preview = await env.plugin.manifests.preview("admin", manifest())
    await env.plugin.manifests.start(
        "admin", preview["job"]["id"], [{"id": "0", "source": "market"}], True
    )
    await env.plugin.transfers.task
    job = env.plugin.transfers.get_job(preview["job"]["id"], "admin")
    assert job["results"][0]["status"] == "failed"
    assert not job.get("installed_artifacts")


@pytest.mark.parametrize("author", [AUTHOR, AUTHOR + ", Coauthor"])
async def test_existing_same_identity_is_skipped_and_conflicting_identity_blocked(
    env, market_mock, author
):
    target = env.add_target(name=NAME)
    target.author = author
    first = await env.plugin.manifests.preview("admin", manifest())
    assert first["plugins"][0]["sources"][0]["status"] == "skip"
    target.author = "impostor"
    second = await env.plugin.manifests.preview("admin", manifest())
    assert second["plugins"][0]["sources"][0]["status"] == "blocked"
    env.manager.install_plugin.assert_not_awaited()


async def test_remote_change_after_preview_stops_item_before_installing(
    env, market_mock
):
    data, fetch, installer = market_mock
    preview = await env.plugin.manifests.preview("admin", manifest())
    data[ID]["version"] = "3.0.0"
    await env.plugin.manifests.start(
        "admin", preview["job"]["id"], [{"id": "0", "source": "market"}], True
    )
    await env.plugin.transfers.task
    job = env.plugin.transfers.get_job(preview["job"]["id"], "admin")
    assert job["results"][0]["status"] == "failed"
    assert "变化" in job["results"][0]["message"]
    installer.assert_not_awaited()
    assert not (Path(env.manager.plugin_store_path) / NAME).exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"author": "impostor"},
        {"name": NAME + "_changed"},
    ],
)
async def test_core_install_identity_mismatch_is_reported_as_partial_failure(
    env, market_mock, core_installs, changes
):
    data, _, _ = market_mock
    core_installs[data[ID]["download_url"]].update(changes)
    preview = await env.plugin.manifests.preview("admin", manifest())
    await env.plugin.manifests.start(
        "admin", preview["job"]["id"], [{"id": "0", "source": "market"}], True
    )
    await env.plugin.transfers.task
    job = env.plugin.transfers.get_job(preview["job"]["id"], "admin")
    assert job["results"][0]["status"] == "failed"
    assert "不一致" in job["results"][0]["message"]
    assert "部分变更" in job["results"][0]["message"]
    assert (Path(env.manager.plugin_store_path) / changes.get("name", NAME)).exists()


async def test_official_version_failure_is_visible_and_never_ignored(
    env, market_mock, core_installs
):
    data, _, installer = market_mock
    core_installs[data[ID]["download_url"]] = PluginVersionUnsupportedError(
        "unsupported version"
    )
    preview = await env.plugin.manifests.preview("admin", manifest())
    await env.plugin.manifests.start(
        "admin", preview["job"]["id"], [{"id": "0", "source": "market"}], True
    )
    await env.plugin.transfers.task
    job = env.plugin.transfers.get_job(preview["job"]["id"], "admin")
    assert job["results"][0]["status"] == "failed"
    assert "AstrBot 版本" in job["results"][0]["message"]
    assert installer.await_args.kwargs["ignore_version_check"] is False
    assert not (Path(env.manager.plugin_store_path) / NAME).exists()


async def test_preview_ownership_confirmation_expiry_and_replay(env, market_mock):
    service = env.plugin.manifests
    preview = await service.preview("admin", manifest())
    with pytest.raises(ValueError, match="无权"):
        await service.start(
            "other", preview["job"]["id"], [{"id": "0", "source": "market"}], True
        )
    with pytest.raises(ValueError, match="确认"):
        await service.start(
            "admin", preview["job"]["id"], [{"id": "0", "source": "market"}], False
        )
    job = env.plugin.transfers.get_job(preview["job"]["id"], "admin")
    job["preview_expires_at"] = 0
    with pytest.raises(ValueError, match="过期"):
        await service.start(
            "admin", preview["job"]["id"], [{"id": "0", "source": "market"}], True
        )


@pytest.mark.parametrize(
    "name,expected", [("", "plugins"), (" 生产环境备份 ", "生产环境备份")]
)
async def test_export_shares_display_name_and_associated_sources(
    env, market_mock, name, expected
):
    target = env.add_target(name=NAME)
    target.author = AUTHOR
    env.prefs["plugin_install_sources"] = {
        target.root_dir_name: {
            "install_method": "market",
            "market_plugin_id": ID,
            "repo": "https://github.com/alice/shared",
            "registry_url": None,
        }
    }
    exported = await env.plugin.manifests.export([target.root_dir_name], name=name)
    value = exported["manifest"]
    assert validate_manifest(value) == value
    assert value["schema_version"] == 1
    assert value["name"] == expected
    assert value["plugins"][0] == {
        "display_name": target.name,
        "market_plugin_id": ID,
        "repo_url": "https://github.com/alice/shared",
    }


@pytest.mark.parametrize("failure", ["core_source", "local_history"])
async def test_source_write_failure_keeps_core_installed_plugin_and_reports_warning(
    env, market_mock, monkeypatch, failure
):
    env.prefs["inactivated_plugins"] = ["data.plugins.other.main"]
    env.prefs["plugin_install_sources"] = {"other": {"install_method": "upload"}}
    if failure == "core_source":
        service = env.plugin.service.web
        saved = service.save_plugin_install_sources
        calls = 0

        async def fail_once(records):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("disk write failure")
            await saved(records)

        monkeypatch.setattr(service, "save_plugin_install_sources", fail_once)
    else:
        monkeypatch.setattr(
            env.plugin.sources,
            "observe_install",
            AsyncMock(side_effect=OSError("history write failure")),
        )
    preview = await env.plugin.manifests.preview("admin", manifest())
    await env.plugin.manifests.start(
        "admin", preview["job"]["id"], [{"id": "0", "source": "market"}], True
    )
    await env.plugin.transfers.task
    job = env.plugin.transfers.get_job(preview["job"]["id"], "admin")
    assert job["status"] == "completed_with_warning", job
    assert job["results"][0]["status"] == (
        "failed" if failure == "core_source" else "installed"
    )
    expected_message = "部分变更" if failure == "core_source" else "来源历史保存失败"
    assert expected_message in job["results"][0]["message"]
    assert (Path(env.manager.plugin_store_path) / NAME / "main.py").exists()
    assert env.context.get_registered_star(NAME).activated
    assert env.prefs["inactivated_plugins"] == ["data.plugins.other.main"]
    assert env.prefs["plugin_install_sources"]["other"] == {"install_method": "upload"}
    assert "pending_install" not in job
    assert NAME not in env.plugin.sources._store()["plugins"]


async def test_one_failed_package_does_not_prevent_other_selected_installs(
    env, market_mock, core_installs
):
    data, _, _ = market_mock
    second = NAME + "_second"
    second_id = f"{AUTHOR}/{second}"
    data[second_id] = {
        **data[ID],
        "name": second,
        "download_url": "https://cloud.astrbot.app/second.zip",
    }
    value = manifest()
    value["plugins"].append({**value["plugins"][0], "market_plugin_id": second_id})

    core_installs[data[ID]["download_url"]] = RuntimeError("dependency install failed")
    core_installs[data[second_id]["download_url"]] = {
        "name": second,
        "author": AUTHOR,
        "version": "2.0.0",
    }
    preview = await env.plugin.manifests.preview("admin", value)
    await env.plugin.manifests.start(
        "admin",
        preview["job"]["id"],
        [{"id": "0", "source": "market"}, {"id": "1", "source": "market"}],
        True,
    )
    await env.plugin.transfers.task
    job = env.plugin.transfers.get_job(preview["job"]["id"], "admin")
    assert job["status"] == "completed_with_warning", job
    assert [item["status"] for item in job["results"]] == ["failed", "installed"]
    assert not (Path(env.manager.plugin_store_path) / NAME).exists()
    assert (Path(env.manager.plugin_store_path) / second / "main.py").exists()


async def test_shutdown_waits_for_official_install_to_finish_loading(env, market_mock):
    entered, release = asyncio.Event(), asyncio.Event()
    original = env.manager.install_plugin.side_effect

    async def install(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    env.manager.install_plugin.side_effect = install
    preview = await env.plugin.manifests.preview("admin", manifest())
    await env.plugin.manifests.start(
        "admin", preview["job"]["id"], [{"id": "0", "source": "market"}], True
    )
    await entered.wait()
    job = env.plugin.transfers.get_job(preview["job"]["id"], "admin")
    assert job["pending_install"]["market_plugin_id"] == ID
    closing = asyncio.create_task(env.plugin.transfers.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await closing
    job = env.plugin.transfers.get_job(preview["job"]["id"], "admin")
    assert job["status"] == "completed", job
    assert "pending_install" not in job
    assert env.context.get_registered_star(NAME).activated


@pytest.mark.parametrize(
    "paths",
    [
        ["../escaped.py"],
        ["C:/escaped.py"],
        ["plugin/A/file", "plugin/a/other"],
        ["plugin/data", "plugin/data/child"],
        ["plugin/CON.txt"],
    ],
)
def test_market_zip_rejects_unsafe_or_conflicting_paths(tmp_path, paths):
    archive_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for path in paths:
            archive.writestr(path, "bad")
    with pytest.raises(ValueError):
        unpack_market_archive(archive_path, tmp_path / "checked")


def test_market_zip_rejects_symbolic_links(tmp_path):
    archive_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        entry = zipfile.ZipInfo("link")
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(entry, "outside")
    with pytest.raises(ValueError, match="链接"):
        unpack_market_archive(archive_path, tmp_path / "checked")


REPO = "https://github.com/alice/shared"


def repository_manifest(*, with_market=False):
    value = manifest()
    value["plugins"][0].update(
        repo_url=REPO, market_plugin_id=ID if with_market else ""
    )
    return value


@pytest.fixture
def repository_mock(monkeypatch, core_installs):
    core_installs[REPO] = {"name": NAME, "author": AUTHOR, "version": "2.0.0"}

    async def download(url, path):
        assert url == REPO
        write_archive(path)

    downloader = AsyncMock(side_effect=download)
    monkeypatch.setattr(
        "astrbot_plugin_manager.manifests.download_repository_archive", downloader
    )
    return downloader


def test_other_versions_are_rejected_and_optional_source_keys_are_normalized():
    unsupported = manifest()
    unsupported["schema_version"] = 2
    with pytest.raises(ValueError, match="v1"):
        validate_manifest(unsupported)
    missing_name = manifest()
    del missing_name["name"]
    with pytest.raises(ValueError, match="name.*重新导出"):
        validate_manifest(missing_name)
    value = repository_manifest()
    del value["plugins"][0]["market_plugin_id"]
    assert validate_manifest(value)["plugins"][0]["market_plugin_id"] == ""
    value["plugins"][0]["repo_url"] = ""
    with pytest.raises(ValueError, match="至少"):
        validate_manifest(value)
    value["plugins"][0]["repo_url"] = "https://user:password@example.com/owner/repo"
    with pytest.raises(ValueError):
        validate_manifest(value)


@pytest.mark.parametrize(
    "name", ["", "  ", None, "../escaped", "CON", "备" * 81, " 清单"]
)
def test_manifest_rejects_invalid_top_level_names(name):
    value = manifest()
    value["name"] = name
    with pytest.raises(ValueError):
        validate_manifest(value)


async def test_named_manifest_preview_and_recheck_keep_name(env, market_mock):
    value = manifest()
    value["name"] = "生产环境备份"
    preview = await env.plugin.manifests.preview("admin", value)
    assert preview["job"]["name"] == "生产环境备份"
    assert preview["job"]["preview_task"] is True
    pending = env.plugin.manifests.recheck("admin", preview["job"]["id"])
    assert pending["job"]["name"] == "生产环境备份"
    await env.plugin.transfers.task
    rechecked = env.plugin.manifests.read_preview("admin", pending["job"]["id"])
    assert rechecked["job"]["name"] == "生产环境备份"


async def test_repository_only_export_uses_existing_sources_without_market(
    env, monkeypatch
):
    target = env.add_target(name=NAME)
    env.prefs["plugin_install_sources"] = {
        target.root_dir_name: {"install_method": "repository", "repo": REPO}
    }
    fetch = AsyncMock(side_effect=AssertionError("Market must not be requested"))
    monkeypatch.setattr(env.plugin.sources, "official_market", fetch)
    exported = await env.plugin.manifests.export([target.root_dir_name])
    assert exported["manifest"]["plugins"] == [
        {"display_name": NAME, "market_plugin_id": "", "repo_url": REPO}
    ]
    record = await env.plugin.sources.detail(target.root_dir_name)
    record["bindings"][0]["verification"] = "imported"
    await env.plugin.sources._save_record(record)
    with pytest.raises(ValueError, match="来源"):
        await env.plugin.manifests.export([target.root_dir_name])
    fetch.assert_not_awaited()


async def test_repository_install_uses_shared_official_workflow_and_loads(
    env, repository_mock, monkeypatch
):
    fetch = AsyncMock(side_effect=AssertionError("Market must not be requested"))
    monkeypatch.setattr(env.plugin.sources, "official_market", fetch)
    service = env.plugin.manifests
    preview = await service.preview("admin", repository_manifest())
    item = preview["plugins"][0]
    assert item["default_source"] == "repository"
    assert item["sources"][0]["name"] == NAME
    assert item["sources"][0]["version"] == "2.0.0"
    await service.start(
        "admin", preview["job"]["id"], [{"id": "0", "source": "repository"}], True
    )
    await env.plugin.transfers.task
    job = env.plugin.transfers.get_job(preview["job"]["id"], "admin")
    assert job["status"] == "completed", job
    source = env.prefs["plugin_install_sources"][NAME]
    assert source["install_method"] == "repository"
    assert source["repo"] == REPO
    assert not source.get("market_plugin_id")
    assert not env.prefs.get("inactivated_plugins")
    assert (Path(env.manager.plugin_store_path) / NAME / "main.py").exists()
    repository_mock.assert_awaited_once()
    fetch.assert_not_awaited()
    assert env.context.get_registered_star(NAME).activated
    env.manager.install_plugin.assert_awaited_once_with(
        REPO, "", ignore_version_check=False, download_url=""
    )


async def test_market_failure_needs_explicit_repository_choice(
    env, repository_mock, monkeypatch
):
    monkeypatch.setattr(
        env.plugin.sources,
        "official_market",
        AsyncMock(side_effect=ValueError("市场离线")),
    )
    service = env.plugin.manifests
    preview = await service.preview("admin", repository_manifest(with_market=True))
    item = preview["plugins"][0]
    assert item["default_source"] == "market"
    assert [source["status"] for source in item["sources"]] == ["blocked", "install"]
    with pytest.raises(ValueError, match="来源"):
        await service.start(
            "admin", preview["job"]["id"], [{"id": "0", "source": "market"}], True
        )
    await service.start(
        "admin", preview["job"]["id"], [{"id": "0", "source": "repository"}], True
    )
    await env.plugin.transfers.task
    assert env.prefs["plugin_install_sources"][NAME]["install_method"] == "repository"


async def test_repository_identity_must_match_market_id(env, market_mock, monkeypatch):
    async def download(url, path):
        write_archive(path, author="Impostor")

    monkeypatch.setattr(
        "astrbot_plugin_manager.manifests.download_repository_archive", download
    )
    preview = await env.plugin.manifests.preview(
        "admin", repository_manifest(with_market=True)
    )
    options = preview["plugins"][0]["sources"]
    assert options[0]["status"] == "install"
    assert options[1]["status"] == "blocked"
    assert "不一致" in options[1]["problem"]


async def test_repository_with_coauthors_can_install_with_one_market_author(
    env, market_mock, core_installs, monkeypatch
):
    author = AUTHOR + ", Coauthor"

    async def download(url, path):
        write_archive(path, author=author)

    monkeypatch.setattr(
        "astrbot_plugin_manager.manifests.download_repository_archive", download
    )
    core_installs[REPO] = {"name": NAME, "author": author, "version": "2.0.0"}
    preview = await env.plugin.manifests.preview(
        "admin", repository_manifest(with_market=True)
    )
    assert preview["plugins"][0]["sources"][1]["status"] == "install"
    await env.plugin.manifests.start(
        "admin", preview["job"]["id"], [{"id": "0", "source": "repository"}], True
    )
    await env.plugin.transfers.task
    job = env.plugin.transfers.get_job(preview["job"]["id"], "admin")
    assert job["status"] == "completed", job["error"]
    assert job["results"][0]["status"] == "installed", job["results"]
    assert env.context.get_registered_star(NAME).author == author


async def test_repository_install_records_actual_version_from_official_installer(
    env, repository_mock, core_installs
):
    service = env.plugin.manifests
    preview = await service.preview("admin", repository_manifest())
    identifier = preview["job"]["id"]
    core_installs[REPO]["version"] = "3.0.0"
    await service.start(
        "admin", identifier, [{"id": "0", "source": "repository"}], True
    )
    await env.plugin.transfers.task
    job = env.plugin.transfers.get_job(identifier, "admin")
    assert job["results"][0]["status"] == "installed"
    assert job["results"][0]["version"] == "3.0.0"
    assert job["installed_artifacts"][0]["version"] == "3.0.0"


@pytest.mark.parametrize(
    "selections",
    [
        [{"id": "0", "source": "repository"}],
        [{"id": "0", "source": "market"}, {"id": "0", "source": "market"}],
        [{"id": "1", "source": "market"}],
        [{"id": "0", "source": "market", "repo_url": REPO}],
    ],
)
async def test_install_cannot_inject_unpreviewed_sources(env, market_mock, selections):
    preview = await env.plugin.manifests.preview("admin", manifest())
    with pytest.raises(ValueError):
        await env.plugin.manifests.start(
            "admin", preview["job"]["id"], selections, True
        )
    assert not (Path(env.manager.plugin_store_path) / NAME).exists()


async def test_same_plugin_from_different_entries_cannot_be_selected_twice(
    env, market_mock, repository_mock
):
    value = manifest()
    value["plugins"].extend(repository_manifest()["plugins"])
    preview = await env.plugin.manifests.preview("admin", value)
    with pytest.raises(ValueError, match="同名插件"):
        await env.plugin.manifests.start(
            "admin",
            preview["job"]["id"],
            [{"id": "0", "source": "market"}, {"id": "1", "source": "repository"}],
            True,
        )


async def test_background_preview_is_owned_and_cannot_be_deleted_while_downloading(
    env, monkeypatch
):
    entered, release = asyncio.Event(), asyncio.Event()

    async def download(url, path):
        entered.set()
        await release.wait()
        write_archive(path)

    monkeypatch.setattr(
        "astrbot_plugin_manager.manifests.download_repository_archive", download
    )
    service = env.plugin.manifests
    result = service.begin_preview("admin", repository_manifest())
    identifier = result["job"]["id"]
    assert result["ready"] is False
    await entered.wait()
    with pytest.raises(ValueError, match="无权"):
        service.read_preview("other", identifier)
    with pytest.raises(ValueError):
        await env.plugin.transfers.delete_job(identifier, "admin", True)
    env.plugin.transfers.task.cancel()
    await env.plugin.transfers.task
    assert env.plugin.transfers.get_job(identifier, "admin")["status"] == "interrupted"
    assert not (Path(env.manager.plugin_store_path) / NAME).exists()


async def test_preview_and_selection_survive_restart_without_redownloading(
    env, repository_mock
):
    preview = await env.plugin.manifests.preview("admin", repository_manifest())
    identifier = preview["job"]["id"]
    draft = [{"id": "0", "source": "repository", "selected": False}]
    assert env.plugin.manifests.save_draft("admin", identifier, draft) == {
        "saved": True
    }
    restarted = TransferService(env.plugin.service, env.plugin._operations)
    await restarted.initialize()
    service = ManifestService(env.plugin.sources, restarted)
    restored = service.read_preview("admin", identifier)
    assert restored["plugins"] == preview["plugins"]
    assert restored["draft"] == draft
    assert restored["job"]["preview_task"]
    assert not restored["job"]["preview_expired"]
    assert restarted.history("other") == []
    repository_mock.assert_awaited_once()
    # Reloading or saving a draft cannot execute the install.
    assert not (Path(env.manager.plugin_store_path) / NAME).exists()
    await service.start(
        "admin", identifier, [{"id": "0", "source": "repository"}], True
    )
    await restarted.task
    assert restarted.get_job(identifier, "admin")["status"] == "completed"
    assert not restarted.public_job(restarted.get_job(identifier, "admin")).get(
        "preview_task"
    )


async def test_preview_deadline_starts_after_checking_and_expired_task_can_recheck(
    env, market_mock, monkeypatch
):
    clock = [1000.0]
    monkeypatch.setattr("astrbot_plugin_manager.manifests.time.time", lambda: clock[0])
    pending = env.plugin.manifests.begin_preview("admin", manifest())
    identifier = pending["job"]["id"]
    clock[0] += 7200
    await env.plugin.transfers.task
    ready = env.plugin.manifests.read_preview("admin", identifier)
    assert ready["expires_at"] == clock[0] + 3600
    clock[0] = ready["expires_at"] + 1
    public = env.plugin.transfers.history("admin")[0]
    assert public["preview_expired"]
    assert not public["expired"], (
        "Expired confirmation must not expire or delete task files"
    )
    with pytest.raises(ValueError, match="过期"):
        env.plugin.manifests.read_preview("admin", identifier)
    with pytest.raises(ValueError, match="过期"):
        env.plugin.manifests.save_draft("admin", identifier, [])
    retry = env.plugin.manifests.recheck("admin", identifier)
    assert retry["job"]["id"] != identifier
    await env.plugin.transfers.task
    assert env.plugin.manifests.read_preview("admin", retry["job"]["id"])["ready"]
    assert env.plugin.transfers.job_directory(identifier).is_dir()


@pytest.mark.parametrize(
    "draft",
    [
        [],
        [{"id": "other", "source": "market", "selected": True}],
        [{"id": "0", "source": "repository", "selected": True}],
        [{"id": "0", "source": "market", "selected": "yes"}],
        [{"id": "0", "source": "market", "selected": True, "repo_url": REPO}],
    ],
)
async def test_invalid_draft_never_changes_saved_selection(env, market_mock, draft):
    preview = await env.plugin.manifests.preview("admin", manifest())
    identifier = preview["job"]["id"]
    original = [{"id": "0", "source": "market", "selected": False}]
    env.plugin.manifests.save_draft("admin", identifier, original)
    with pytest.raises(ValueError):
        env.plugin.manifests.save_draft("admin", identifier, draft)
    assert env.plugin.manifests.read_preview("admin", identifier)["draft"] == original
