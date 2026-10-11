"""Exercise manifest validation and real staged ZIP installs without executing code."""

import copy
import stat
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml
from astrbot_plugin_manager.manifests import MANIFEST_FORMAT, validate_manifest
from astrbot_plugin_manager.market_archive import unpack_market_archive
from astrbot_plugin_manager.transfers import TransferService

NAME = "astrbot_plugin_shared"
AUTHOR = "Alice"
ID = f"{AUTHOR}/{NAME}"


def manifest():
    return {
        "format": MANIFEST_FORMAT,
        "schema_version": 1,
        "created_at": "2026-10-11T00:00:00Z",
        "plugins": [
            {
                "name": NAME,
                "author": AUTHOR,
                "market_plugin_id": ID,
                "observed_version": "1.0.0",
                "registry": "official",
                "version_policy": "latest",
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
def market_mock(env, monkeypatch):
    data = registry()
    fetch = AsyncMock(side_effect=lambda **kwargs: (copy.deepcopy(data), None))
    monkeypatch.setattr(env.plugin.service.web, "get_online_plugins", fetch)

    async def download(url, path):
        assert url == data[ID]["download_url"]
        write_archive(path)

    downloader = AsyncMock(side_effect=download)
    monkeypatch.setattr(
        "astrbot_plugin_manager.manifests.download_market_archive", downloader
    )
    return data, fetch, downloader


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


async def test_manifest_install_writes_disabled_code_and_real_core_source_only(
    env, market_mock
):
    data, fetch, downloader = market_mock
    service = env.plugin.manifests
    preview = await service.preview("admin", manifest())
    assert preview["plugins"][0]["status"] == "install"
    assert "remote" not in preview["plugins"][0]
    await service.start("admin", preview["job"]["id"], [ID], True)
    await env.plugin.transfers.task
    job = env.plugin.transfers.get_job(preview["job"]["id"], "admin")
    assert job["status"] == "completed", job
    assert job["results"][0]["status"] == "installed"
    assert job["reload_required"]
    assert len(job["installed_artifacts"][0]["sha256"]) == 64
    code = Path(env.manager.plugin_store_path) / NAME
    assert (code / "main.py").is_file()
    assert f"data.plugins.{NAME}.main" in env.prefs["inactivated_plugins"]
    assert env.prefs["plugin_install_sources"][NAME]["market_plugin_id"] == ID
    assert (await env.plugin.sources.detail(NAME))["provenance"][
        "evidence"
    ] == "manager_operation"
    env.manager.load.assert_not_awaited()
    env.manager.install_plugin.assert_not_awaited()
    downloader.assert_awaited_once()
    assert fetch.await_count == 2
    restarted = TransferService(env.plugin.service, env.plugin._operations)
    await restarted.initialize()
    assert restarted.history("admin")[0]["status"] == "completed"


async def test_existing_same_identity_is_skipped_and_conflicting_identity_blocked(
    env, market_mock
):
    target = env.add_target(name=NAME)
    target.author = AUTHOR
    first = await env.plugin.manifests.preview("admin", manifest())
    assert first["plugins"][0]["status"] == "skip"
    target.author = "impostor"
    second = await env.plugin.manifests.preview("admin", manifest())
    assert second["plugins"][0]["status"] == "blocked"
    env.manager.install_plugin.assert_not_awaited()


async def test_remote_change_after_preview_stops_item_before_downloading(
    env, market_mock
):
    data, fetch, download = market_mock
    preview = await env.plugin.manifests.preview("admin", manifest())
    data[ID]["version"] = "3.0.0"
    await env.plugin.manifests.start("admin", preview["job"]["id"], [ID], True)
    await env.plugin.transfers.task
    job = env.plugin.transfers.get_job(preview["job"]["id"], "admin")
    assert job["results"][0]["status"] == "failed"
    assert "变化" in job["results"][0]["message"]
    download.assert_not_awaited()
    assert not (Path(env.manager.plugin_store_path) / NAME).exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"author": "impostor"},
        {"version": "1.0.0"},
        {"requirement": "definitely-missing-plugin-manager-dependency==99.0"},
    ],
)
async def test_invalid_download_identity_or_dependencies_never_reach_live_directory(
    env, market_mock, monkeypatch, changes
):
    async def download(url, path):
        write_archive(path, **changes)

    monkeypatch.setattr(
        "astrbot_plugin_manager.manifests.download_market_archive", download
    )
    preview = await env.plugin.manifests.preview("admin", manifest())
    await env.plugin.manifests.start("admin", preview["job"]["id"], [ID], True)
    await env.plugin.transfers.task
    job = env.plugin.transfers.get_job(preview["job"]["id"], "admin")
    assert job["results"][0]["status"] == "failed"
    assert not (Path(env.manager.plugin_store_path) / NAME).exists()
    env.manager.load.assert_not_awaited()


async def test_preview_ownership_confirmation_expiry_and_replay(env, market_mock):
    service = env.plugin.manifests
    preview = await service.preview("admin", manifest())
    with pytest.raises(ValueError, match="无权"):
        await service.start("other", preview["job"]["id"], [ID], True)
    with pytest.raises(ValueError, match="确认"):
        await service.start("admin", preview["job"]["id"], [ID], False)
    job = env.plugin.transfers.get_job(preview["job"]["id"], "admin")
    job["created_at"] = 0
    with pytest.raises(ValueError, match="过期"):
        await service.start("admin", preview["job"]["id"], [ID], True)


async def test_export_only_shares_market_identity_and_latest_policy(env, market_mock):
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
    exported = await env.plugin.manifests.export([target.root_dir_name])
    value = exported["manifest"]
    assert validate_manifest(value) == value
    assert value["plugins"][0]["observed_version"] == target.version
    assert (
        "repo" not in value["plugins"][0] and "download_url" not in value["plugins"][0]
    )


@pytest.mark.parametrize("failure", ["core_source", "local_history"])
async def test_install_source_write_failure_restores_new_directory_and_preferences(
    env, market_mock, monkeypatch, failure
):
    env.prefs["inactivated_plugins"] = ["data.plugins.other.main"]
    env.prefs["plugin_install_sources"] = {"other": {"install_method": "upload"}}
    original = copy.deepcopy(env.prefs)
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
    await env.plugin.manifests.start("admin", preview["job"]["id"], [ID], True)
    await env.plugin.transfers.task
    job = env.plugin.transfers.get_job(preview["job"]["id"], "admin")
    assert job["status"] == "completed_with_warning", job
    assert job["results"][0]["status"] == "failed"
    assert "已恢复" in job["results"][0]["message"]
    assert not (Path(env.manager.plugin_store_path) / NAME).exists()
    assert env.prefs == original
    assert "pending_install" not in job
    assert NAME not in env.plugin.sources._store()["plugins"]


async def test_one_failed_package_does_not_prevent_other_selected_installs(
    env, market_mock, monkeypatch
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
    value["plugins"].append(
        {**value["plugins"][0], "name": second, "market_plugin_id": second_id}
    )

    async def download(url, path):
        if url == data[ID]["download_url"]:
            write_archive(path, author="impostor")
        else:
            write_archive(path, name=second)

    monkeypatch.setattr(
        "astrbot_plugin_manager.manifests.download_market_archive", download
    )
    preview = await env.plugin.manifests.preview("admin", value)
    await env.plugin.manifests.start(
        "admin", preview["job"]["id"], [ID, second_id], True
    )
    await env.plugin.transfers.task
    job = env.plugin.transfers.get_job(preview["job"]["id"], "admin")
    assert job["status"] == "completed_with_warning", job
    assert [item["status"] for item in job["results"]] == ["failed", "installed"]
    assert not (Path(env.manager.plugin_store_path) / NAME).exists()
    assert (Path(env.manager.plugin_store_path) / second / "main.py").exists()


async def test_failed_automatic_recovery_preserves_journal_and_stops_task(
    env, market_mock, monkeypatch
):
    monkeypatch.setattr(
        env.plugin.sources,
        "observe_install",
        AsyncMock(side_effect=OSError("write failure")),
    )
    monkeypatch.setattr(
        env.plugin.sources,
        "restore_sources",
        AsyncMock(side_effect=OSError("recovery failure")),
    )
    preview = await env.plugin.manifests.preview("admin", manifest())
    await env.plugin.manifests.start("admin", preview["job"]["id"], [ID], True)
    await env.plugin.transfers.task
    job = env.plugin.transfers.get_job(preview["job"]["id"], "admin")
    assert job["status"] == "rollback_failed", job
    assert job["pending_install"]["market_plugin_id"] == ID
    assert job["results"][0]["status"] == "failed"
    assert "后续" in job["error"]


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
