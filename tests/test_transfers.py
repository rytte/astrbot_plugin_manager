"""End-to-end transfer behavior with real registries and isolated side effects."""

import asyncio
import copy
import json
import shutil
import sqlite3
import time
import zipfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml
from astrbot_plugin_manager import transfer_bundle as bundle
from astrbot_plugin_manager.sources import SourceService
from astrbot_plugin_manager.transfer_bundle import (
    load_json,
    remove_path,
    tree_files,
    unpack,
)
from astrbot_plugin_manager.transfers import TransferService


async def export_package(env, target, *, data=False, config=False):
    transfers = env.plugin.transfers
    rule = await transfers.save_rule(
        [{"directory": target.root_dir_name, "data": data, "config": config}]
    )
    result = await transfers.start_export("admin", rule["updated_at"], True)
    await transfers.task
    job = transfers.get_job(result["id"], "admin")
    assert job["status"] in {"completed", "completed_with_warning"}, job
    return transfers.download(job["id"], "admin", "package")


async def prepare(env, path):
    transfers = env.plugin.transfers
    job = transfers.new_upload("admin")
    shutil.copyfile(path, transfers.job_directory(job["id"]) / "upload.zip")
    result = await transfers.prepare_import(job)
    return job, result


async def test_code_only_backup_carries_sources_and_restores_them_pending(
    transfer_env, monkeypatch
):
    env = transfer_env
    target = env.make_transfer_plugin()
    sources, transfers = env.plugin.sources, env.plugin.transfers
    repo = "https://github.com/test/demo"
    market_id = f"{target.author}/{target.name}"
    env.prefs["plugin_install_sources"] = {
        target.root_dir_name: {
            "install_method": "market",
            "registry_url": None,
            "market_plugin_id": market_id,
            "repo": repo,
            "installed_at": "2025-01-01T00:00:00Z",
        }
    }
    inspect = AsyncMock(return_value={"name": target.name, "author": target.author})
    monkeypatch.setattr(
        env.manager, "inspect_plugin_repository", inspect, raising=False
    )
    binding_preview = await sources.preview_binding(
        "admin", target.root_dir_name, "repository", repo
    )
    original = await sources.bind("admin", binding_preview["id"], True, True)
    package = await export_package(env, target)
    with zipfile.ZipFile(package) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        data = json.loads(archive.read(f"payload/{target.root_dir_name}/sources.json"))
    assert manifest["format_version"] == 1
    assert not manifest["plugins"][0]["config"] and not manifest["plugins"][0]["data"]
    assert data["provenance"]["method"] == "market"
    assert data["provenance"]["reported_installed_at"].startswith("2025")
    assert len(data["bindings"]) == 2
    assert data["active_binding_id"] == original["active_binding_id"]
    assert "history" not in data
    assert all(
        "verification" not in binding and "checked_at" not in binding
        for binding in data["bindings"]
    )
    # Simulate moving to an empty installation, without executing plugin code.
    env.owners.pop(target.module_path)
    shutil.rmtree(transfers.code_root / target.root_dir_name)
    sources.path.unlink()
    env.prefs["plugin_install_sources"] = {}
    job, _ = await prepare(env, package)
    await transfers.start_import(job["id"], "admin", [restore_selection(target)], True)
    await transfers.task
    assert job["status"] == "completed", job
    restored = await sources.detail(target.root_dir_name)
    assert restored["active_binding_id"] is None
    assert {binding["verification"] for binding in restored["bindings"]} == {"imported"}
    assert (
        next(binding for binding in restored["bindings"] if binding["imported_active"])[
            "id"
        ]
        == original["active_binding_id"]
    )
    assert not env.prefs["plugin_install_sources"]
    assert restored["provenance"]["evidence"] == "bundle_record"
    restarted = SourceService(env.plugin.service, transfers)
    assert (await restarted.detail(target.root_dir_name))["bindings"] == restored[
        "bindings"
    ]
    # Backup/restore again must retain the preferred source without promoting trust.
    repeated, warnings = await sources.export_sources(
        target.root_dir_name, restored["local"]
    )
    assert not warnings and repeated["active_binding_id"] == data["active_binding_id"]
    market = {
        market_id: {
            "name": target.name,
            "author": target.author,
            "repo": repo,
            "download_url": "https://example.com/official.zip",
        }
    }
    monkeypatch.setattr(
        sources.operations.web,
        "get_online_plugins",
        AsyncMock(return_value=(market, None)),
    )
    with pytest.raises(ValueError, match="缺少可分享的来源"):
        await env.plugin.manifests.export([target.root_dir_name])
    selected = await sources.select(
        "admin",
        target.root_dir_name,
        original["active_binding_id"],
        restored["revision"],
    )
    assert selected["active_binding_id"] == original["active_binding_id"]
    assert inspect.await_count == 2
    assert all(not binding.get("imported_active") for binding in selected["bindings"])
    assert (
        env.prefs["plugin_install_sources"][target.root_dir_name]["install_method"]
        == "repository"
    )
    market_binding = next(
        binding for binding in selected["bindings"] if binding["kind"] == "market"
    )
    selected = await sources.select(
        "admin", target.root_dir_name, market_binding["id"], selected["revision"]
    )
    assert (
        next(
            binding
            for binding in selected["bindings"]
            if binding["id"] == market_binding["id"]
        )["verification"]
        == "identity_matched"
    )
    assert (await env.plugin.manifests.export([target.root_dir_name]))["manifest"][
        "plugins"
    ][0]["market_plugin_id"] == market_id
    env.manager.load.assert_not_awaited()


async def test_source_backup_strips_credentials_and_omits_unsafe_locators(transfer_env):
    env = transfer_env
    target = env.make_transfer_plugin()
    env.prefs["plugin_install_sources"] = {
        target.root_dir_name: {
            "install_method": "market",
            "market_plugin_id": f"{target.author}/{target.name}",
            "repo": "https://user:password@github.com/test/demo?token=secret#private",
            "registry_url": None,
            "download_url": "https://example.com/download?token=secret",
        }
    }
    record = await env.plugin.sources.detail(target.root_dir_name)
    record["bindings"].append(
        {"id": "local", "kind": "repository", "repo_url": "file:///private/repo"}
    )
    await env.plugin.sources._save_record(record)
    package = await export_package(env, target)
    with zipfile.ZipFile(package) as archive:
        text = archive.read(f"payload/{target.root_dir_name}/sources.json").decode()
    assert not any(
        value in text for value in ("password", "secret", "private", "download_url")
    )
    data = json.loads(text)
    assert len(data["bindings"]) == 1
    assert data["bindings"][0]["repo_url"] == "https://github.com/test/demo"
    job = next(
        job for job in env.plugin.transfers.jobs.values() if job["kind"] == "export"
    )
    assert job["status"] == "completed_with_warning"
    assert any("来源地址" in warning for warning in job["warnings"])


@pytest.mark.parametrize("component", ["config", "data"])
async def test_restoring_only_config_or_data_keeps_current_local_sources(
    transfer_env, component
):
    env = transfer_env
    target = env.make_transfer_plugin()
    transfers, sources = env.plugin.transfers, env.plugin.sources
    config = transfers.config_root / (target.root_dir_name + "_config.json")
    config.write_text('{"enabled": true}', encoding="utf-8")
    env.kv_values[("plugin", target.plugin_id)] = {"saved": "data"}
    env.prefs["plugin_install_sources"] = {
        target.root_dir_name: {
            "install_method": "repository",
            "repo": "https://github.com/test/original",
        }
    }
    package = await export_package(env, target, config=True, data=True)
    env.prefs["plugin_install_sources"][target.root_dir_name]["repo"] = (
        "https://github.com/test/current"
    )
    before = await sources.detail(target.root_dir_name)
    core_before = copy.deepcopy(env.prefs["plugin_install_sources"])
    job, _ = await prepare(env, package)
    await transfers.start_import(
        job["id"],
        "admin",
        [
            restore_selection(
                target,
                code=False,
                config=component == "config",
                data=component == "data",
            )
        ],
        True,
    )
    await transfers.task
    assert job["status"] == "completed", job
    after = await sources.detail(target.root_dir_name)
    for field in ("bindings", "active_binding_id", "provenance", "history"):
        assert after[field] == before[field]
    assert env.prefs["plugin_install_sources"] == core_before


async def test_inventory_defers_file_scans_and_kv_until_statistics(
    transfer_env, monkeypatch
):
    env = transfer_env
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    code = transfers.code_root / target.root_dir_name
    config = transfers.config_root / (target.root_dir_name + "_config.json")
    config.write_bytes(b'{"enabled": true}')
    data = transfers.data_root / target.name
    data.mkdir()
    (data / "state.txt").write_bytes(b"saved state")
    env.kv_values[("plugin", target.plugin_id)] = {"first": "a", "second": "b"}
    scan = Mock(wraps=tree_files)
    monkeypatch.setattr("astrbot_plugin_manager.transfers.tree_files", scan)

    basic = next(
        entry
        for entry in await transfers.inventory()
        if entry["directory"] == target.root_dir_name
    )
    assert basic["statistics_status"] == "pending"
    assert basic["code_size"] is None and basic["kv_count"] is None
    assert basic["data_available"] and basic["config_available"]
    assert basic["plugin_id"] == target.plugin_id
    assert not basic["blocked"]
    scan.assert_not_called()
    env.preferences.range_get_async.assert_not_awaited()

    detailed = next(
        entry
        for entry in await transfers.inventory(statistics=True)
        if entry["directory"] == target.root_dir_name
    )
    assert detailed["statistics_status"] == "ready"
    assert detailed["code_size"] == sum(path.stat().st_size for path in code.iterdir())
    assert detailed["data_size"] == len(b"saved state")
    assert detailed["config_size"] == config.stat().st_size
    assert detailed["kv_count"] == 2
    assert scan.call_count == 2
    env.preferences.range_get_async.assert_awaited_once_with("plugin", target.plugin_id)


async def test_export_rechecks_files_after_page_statistics(transfer_env):
    env = transfer_env
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    assert any(
        entry["statistics_status"] == "ready"
        for entry in await transfers.inventory(statistics=True)
        if entry["directory"] == target.root_dir_name
    )
    outside = env.root / "private.txt"
    outside.write_text("must not be exported", encoding="utf-8")
    linked = transfers.code_root / target.root_dir_name / "linked.txt"
    try:
        linked.hardlink_to(outside)
    except OSError:
        pytest.skip("Hardlink creation is unavailable")

    rule = await transfers.save_rule(
        [{"directory": target.root_dir_name, "config": False, "data": False}]
    )
    started = await transfers.start_export("admin", rule["updated_at"], True)
    await transfers.task
    job = transfers.get_job(started["id"], "admin")
    assert job["status"] == "failed"
    assert "硬链接" in job["error"]
    assert not (transfers.job_directory(job["id"]) / "package.zip").exists()


@pytest.mark.parametrize("kind", ["code", "data"])
@pytest.mark.parametrize("disappear", [False, True])
async def test_export_change_error_identifies_plugin_component_and_path(
    transfer_env, monkeypatch, caplog, kind, disappear
):
    env = transfer_env
    target = env.make_transfer_plugin(active=True)
    transfers = env.plugin.transfers
    if kind == "code":
        root = transfers.code_root / target.root_dir_name
        expected_directory = f"plugins/{target.root_dir_name}/"
        expected_component = "代码目录"
    else:
        root = transfers.data_root / target.name
        root.mkdir()
        expected_directory = f"plugin_data/{target.name}/"
        expected_component = "标准数据目录"
    changing = root / "session.json"
    changing.write_text("before", encoding="utf-8")
    original_copy = bundle.shutil.copyfile

    def change_during_copy(path, destination):
        if path == changing and disappear:
            changing.unlink()
        original_copy(path, destination)
        if path == changing:
            changing.write_text("after modification", encoding="utf-8")

    monkeypatch.setattr(bundle.shutil, "copyfile", change_during_copy)
    rule = await transfers.save_rule(
        [{"directory": target.root_dir_name, "config": False, "data": True}]
    )
    started = await transfers.start_export("admin", rule["updated_at"], True)
    await transfers.task
    job = transfers.get_job(started["id"], "admin")
    assert job["status"] == "failed"
    assert f"插件 {target.name}（目录 {target.root_dir_name}）" in job["error"]
    assert expected_component in job["error"]
    assert expected_directory in job["error"]
    assert "session.json" in job["error"]
    assert ("路径消失" if disappear else "修改：session.json") in job["error"]
    assert job["error"] in caplog.text
    job_directory = transfers.job_directory(job["id"])
    assert load_json(job_directory / "job.json")["error"] == job["error"]
    assert not (job_directory / "package.zip").exists()
    assert env.owners[target.module_path].activated
    assert env.actions == ["terminate", "unbind", "load"]


async def test_export_closed_wal_database_with_media_restores_enabled_plugin(
    transfer_env,
):
    env = transfer_env
    target = env.make_transfer_plugin(
        name="astrbot_plugin_custom_plan",
        directory="astrbot_plugin_custom_plan",
        active=True,
    )
    transfers = env.plugin.transfers
    data = transfers.data_root / target.name
    (data / "media").mkdir(parents=True)
    (data / "media" / "image.png").write_bytes(b"saved image")
    with closing(sqlite3.connect(data / "plans.sqlite3")) as database:
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("CREATE TABLE plans(media TEXT)")
        database.execute("INSERT INTO plans VALUES ('image.png')")
        database.commit()
    assert not (data / "plans.sqlite3-wal").exists()
    assert not (data / "plans.sqlite3-shm").exists()

    package = await export_package(env, target, data=True)
    stage = env.root / "wal-export"
    unpack(package, stage)
    archived_data = stage / "payload" / target.root_dir_name / "data"
    with closing(sqlite3.connect(archived_data / "plans.sqlite3")) as snapshot:
        assert snapshot.execute("SELECT media FROM plans").fetchone() == ("image.png",)
        assert snapshot.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert (archived_data / "media" / "image.png").read_bytes() == b"saved image"
    assert not (archived_data / "plans.sqlite3-wal").exists()
    assert not (archived_data / "plans.sqlite3-shm").exists()
    assert env.owners[target.module_path].activated
    assert env.actions == ["terminate", "unbind", "load"]


@pytest.mark.parametrize("active", [False, True])
async def test_author_edits_keep_real_status_and_original_kv_scope(
    transfer_env, active
):
    env = transfer_env
    target = env.make_transfer_plugin(active=active)
    transfers = env.plugin.transfers
    metadata_path = transfers.code_root / target.root_dir_name / "metadata.yaml"
    metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    metadata["author"] = "New Author"
    metadata_path.write_text(yaml.safe_dump(metadata), encoding="utf-8")
    config = transfers.config_root / (target.root_dir_name + "_config.json")
    config.write_text('{"enabled": true}', encoding="utf-8")
    old_id, new_id = target.plugin_id, f"new author/{target.name}"
    env.kv_values[("plugin", old_id)] = {"original": "runtime value"}
    env.kv_values[("plugin", new_id)] = {"unrelated": "other value", "extra": 1}
    entry = next(
        item
        for item in await transfers.inventory(statistics=True)
        if item["directory"] == target.root_dir_name
    )
    assert entry["status"] == ("enabled" if active else "disabled")
    assert not entry["blocked"] and entry["metadata_changed"]
    assert entry["config_available"] and entry["data_available"]
    assert entry["plugin_id"] == new_id and entry["data_plugin_id"] == old_id
    assert entry["kv_count"] == 1
    preview = await transfers.export_preview(
        [{"directory": target.root_dir_name, "config": True, "data": False}]
    )
    assert preview["plugins"][0]["config"]
    if active:
        with pytest.raises(ValueError, match="先停用"):
            await transfers.export_preview(
                [{"directory": target.root_dir_name, "config": False, "data": True}]
            )
        env.manager._terminate_plugin.assert_not_awaited()
    else:
        preview = await transfers.export_preview(
            [{"directory": target.root_dir_name, "config": False, "data": True}]
        )
        started = await transfers.start_export(
            "admin", None, True, selections=preview["selections"]
        )
        await transfers.task
        package = transfers.download(started["id"], "admin", "package")
        stage = env.root / "edited-author-export"
        manifest = unpack(package, stage)
        assert manifest["plugins"][0]["plugin_id"] == new_id
        assert set(load_json(stage / "payload" / target.root_dir_name / "kv.json")) == {
            "original"
        }
        assert env.kv_values[("plugin", new_id)]["unrelated"] == "other value"
        env.preferences.put_async.assert_not_awaited()
        env.manager.load.assert_not_awaited()


async def test_metadata_name_conflicts_do_not_invent_loading_failure(transfer_env):
    env = transfer_env
    target = env.make_transfer_plugin(active=True)
    transfers = env.plugin.transfers
    path = transfers.code_root / target.root_dir_name / "metadata.yaml"
    metadata = yaml.safe_load(path.read_text(encoding="utf-8"))
    metadata["name"] = "another_plugin"
    path.write_text(yaml.safe_dump(metadata), encoding="utf-8")
    entry = next(
        item
        for item in await transfers.inventory()
        if item["directory"] == target.root_dir_name
    )
    assert entry["status"] == "enabled"
    assert entry["blocked"] and not entry["data_available"]
    with pytest.raises(ValueError, match="插件名"):
        await transfers.export_preview(
            [{"directory": target.root_dir_name, "config": False, "data": False}]
        )


@pytest.mark.parametrize("fail_after_source_change", [False, True])
@pytest.mark.parametrize("legacy_source_key", [False, True])
async def test_code_migration_restores_pending_sources_and_rollback_restores_original(
    transfer_env, monkeypatch, fail_after_source_change, legacy_source_key
):
    env = transfer_env
    target = env.make_transfer_plugin()
    sources, transfers = env.plugin.sources, env.plugin.transfers
    env.prefs["plugin_install_sources"] = {
        (target.name if legacy_source_key else target.root_dir_name): {
            "install_method": "market",
            "registry_url": None,
            "market_plugin_id": f"{target.author}/{target.name}",
            "repo": "https://github.com/test/demo",
        }
    }
    original_core = copy.deepcopy(env.prefs["plugin_install_sources"])
    original_record = await sources.detail(target.root_dir_name)
    package = await export_package(env, target)
    job, _ = await prepare(env, package)
    if fail_after_source_change:
        saved = sources.note_bundle

        async def fail(directory, actor, incoming):
            await saved(directory, actor, incoming)
            raise OSError("failure after clearing old source")

        monkeypatch.setattr(sources, "note_bundle", fail)
    await transfers.start_import(job["id"], "admin", [restore_selection(target)], True)
    await transfers.task
    current = await sources.detail(target.root_dir_name)
    rollback = load_json(transfers.job_directory(job["id"]) / "rollback.json")
    assert "source_state" in rollback["plugins"][0]
    if fail_after_source_change:
        assert job["status"] == "failed", job
        assert current["bindings"] == original_record["bindings"]
        assert env.prefs["plugin_install_sources"] == original_core
    else:
        assert job["status"] == "completed", job
        assert current["active_binding_id"] is None
        assert len(current["bindings"]) == len(original_record["bindings"])
        assert (
            current["provenance"]["method"] == original_record["provenance"]["method"]
        )
        assert current["provenance"]["evidence"] == "bundle_record"
        assert current["bindings"][0]["verification"] == "imported"
        assert current["bindings"][0]["imported_active"]
        assert target.root_dir_name not in env.prefs["plugin_install_sources"]
        assert target.name not in env.prefs["plugin_install_sources"]


@pytest.mark.parametrize("old_format", ["v1-without-sources", "v2"])
async def test_old_bundle_formats_fail_preflight_without_changing_local_plugins(
    transfer_env, old_format
):
    env = transfer_env
    target = env.make_transfer_plugin(active=True)
    transfers, sources = env.plugin.transfers, env.plugin.sources
    env.prefs["plugin_install_sources"] = {
        target.root_dir_name: {
            "install_method": "repository",
            "repo": "https://github.com/test/demo",
        }
    }
    original_record = await sources.detail(target.root_dir_name)
    original_core = copy.deepcopy(env.prefs["plugin_install_sources"])
    code = transfers.code_root / target.root_dir_name / "main.py"
    original_code = code.read_bytes()
    package = await export_package(env, target)
    with zipfile.ZipFile(package) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(contents["manifest.json"])
    if old_format == "v2":
        manifest["format_version"] = 2
        error = "版本不受支持"
    else:
        relative = f"payload/{target.root_dir_name}/sources.json"
        contents.pop(relative)
        manifest["files"].pop(relative)
        error = "缺少来源快照"
    contents["manifest.json"] = json.dumps(manifest).encode()
    old_package = env.root / "old-format.zip"
    with zipfile.ZipFile(old_package, "w") as archive:
        for name, content in contents.items():
            archive.writestr(name, content)
    job = transfers.new_upload("admin")
    shutil.copyfile(old_package, transfers.job_directory(job["id"]) / "upload.zip")
    with pytest.raises(ValueError, match=error):
        await transfers.prepare_import(job)
    assert job["status"] == "failed"
    assert "manifest" not in job
    assert code.read_bytes() == original_code
    assert (await sources.detail(target.root_dir_name))["bindings"] == original_record[
        "bindings"
    ]
    assert env.prefs["plugin_install_sources"] == original_core
    env.manager._terminate_plugin.assert_not_awaited()
    env.manager.load.assert_not_awaited()


@pytest.mark.parametrize("old_format", ["v1-without-sources", "v2"])
@pytest.mark.parametrize("phase", ["confirmation", "execution"])
async def test_retained_old_previews_are_rejected_before_modifying_plugins(
    transfer_env, old_format, phase
):
    env = transfer_env
    target = env.make_transfer_plugin(active=True)
    transfers = env.plugin.transfers
    job, _ = await prepare(env, await export_package(env, target))
    if phase == "execution":
        await env.manager._pm_lock.acquire()
    try:
        if phase == "execution":
            await transfers.start_import(
                job["id"], "admin", [restore_selection(target)], True
            )
        if old_format == "v2":
            job["manifest"]["format_version"] = 2
            error = "版本不受支持"
        else:
            relative = f"payload/{target.root_dir_name}/sources.json"
            job["manifest"]["files"].pop(relative)
            (transfers.job_directory(job["id"]) / "checked" / relative).unlink()
            error = "缺少来源快照"
        if phase == "confirmation":
            transfers._save(job)
            restarted = TransferService(env.plugin.service, env.plugin._operations)
            await restarted.initialize()
            with pytest.raises(ValueError, match=error):
                await restarted.start_import(
                    job["id"], "admin", [restore_selection(target)], True
                )
    finally:
        if phase == "execution":
            env.manager._pm_lock.release()
    if phase == "execution":
        await transfers.task
        assert job["status"] == "failed" and error in job["error"]
    assert target.activated
    assert not (transfers.job_directory(job["id"]) / "before").exists()
    env.manager._terminate_plugin.assert_not_awaited()
    env.manager.load.assert_not_awaited()


async def test_rule_persists_components_and_can_be_cleared(transfer_env):
    env = transfer_env
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    rule = await transfers.save_rule(
        [{"directory": target.root_dir_name, "config": False, "data": True}]
    )
    restarted = TransferService(env.plugin.service, env.plugin._operations)
    await restarted.initialize()
    assert restarted.rule() == rule
    assert rule["selections"][0]["name"] == target.name
    assert rule["selections"][0]["plugin_id"] == target.plugin_id
    await restarted.save_rule([])
    with pytest.raises(ValueError, match="没有插件"):
        await restarted.export_preview()


@pytest.mark.parametrize("existing_rule", [False, True])
async def test_current_selection_export_is_independent_of_saved_rule(
    transfer_env, existing_rule
):
    env = transfer_env
    target = env.make_transfer_plugin()
    other = env.make_transfer_plugin(name="other", directory="other_dir")
    transfers = env.plugin.transfers
    if existing_rule:
        await transfers.save_rule(
            [{"directory": other.root_dir_name, "config": False, "data": False}]
        )
    saved_before = transfers.rule()
    preview = await transfers.export_preview(
        [{"directory": target.root_dir_name, "config": False, "data": False}]
    )
    assert transfers.rule() == saved_before
    assert transfers.rule_path.exists() == existing_rule
    assert preview["selections"][0]["plugin_id"] == target.plugin_id
    if existing_rule:
        # Another page may change the reusable rule while confirmation is open.
        await transfers.save_rule([])
    saved_after = transfers.rule()
    async with env.manager._pm_lock:
        started = await transfers.start_export(
            "admin", None, True, selections=preview["selections"]
        )
        # The queued job owns a copy, including its component choices.
        preview["selections"][0].update(directory=other.root_dir_name, data=True)
    await transfers.task
    job = transfers.get_job(started["id"], "admin")
    assert job["status"] == "completed", job
    package = transfers.download(job["id"], "admin", "package")
    manifest = unpack(package, env.root / "current-export")
    assert [item["directory"] for item in manifest["plugins"]] == [target.root_dir_name]
    assert not job["rule"]["selections"][0]["data"]
    assert transfers.rule() == saved_after
    assert transfers.rule_path.exists() == existing_rule


@pytest.mark.parametrize("change", ["identity", "configuration", "missing"])
async def test_current_selection_export_rechecks_after_confirmation(
    transfer_env, change
):
    env = transfer_env
    target = env.make_transfer_plugin(active=True)
    transfers = env.plugin.transfers
    config = transfers.config_root / (target.root_dir_name + "_config.json")
    config.write_text('{"enabled": true}', encoding="utf-8")
    preview = await transfers.export_preview(
        [{"directory": target.root_dir_name, "config": True, "data": True}]
    )
    if change == "identity":
        preview["selections"][0]["plugin_id"] = "Another/plugin"
    elif change == "configuration":
        config.unlink()
    else:
        shutil.rmtree(transfers.code_root / target.root_dir_name)
    with pytest.raises(ValueError):
        await transfers.start_export(
            "admin", None, True, selections=preview["selections"]
        )
    assert not transfers.jobs
    assert not transfers.rule_path.exists()
    env.manager._terminate_plugin.assert_not_awaited()


async def test_export_codes_do_not_import_code_or_include_unselected_artifacts(
    transfer_env, tmp_path
):
    env = transfer_env
    target = env.make_transfer_plugin()
    code = Path(env.manager.plugin_store_path) / target.root_dir_name
    (code / "__pycache__").mkdir()
    (code / "__pycache__" / "cached.pyc").write_bytes(b"cache")
    (code / ".git").mkdir()
    (code / ".git" / "config").write_text("credential", encoding="utf-8")
    (code / "empty_assets").mkdir()
    data = env.plugin.transfers.data_root / target.name
    data.mkdir()
    (data / "private.txt").write_text("private-data", encoding="utf-8")
    config = Path(env.manager.plugin_config_path) / (
        target.root_dir_name + "_config.json"
    )
    config.write_text('{"enabled":true}', encoding="utf-8")
    path = await export_package(env, target)
    manifest = unpack(path, tmp_path / "unpacked")
    plugin = manifest["plugins"][0]
    assert plugin["config"] is False and plugin["data"] is False
    assert not any(
        ".git" in name or "__pycache__" in name for name in manifest["files"]
    )
    assert (
        tmp_path
        / "unpacked"
        / "payload"
        / target.root_dir_name
        / "code"
        / "empty_assets"
    ).is_dir()
    env.manager.load.assert_not_awaited()
    env.manager._terminate_plugin.assert_not_awaited()


async def test_export_data_uses_name_and_plugin_id_and_restores_active_plugin(
    transfer_env, tmp_path
):
    env = transfer_env
    target = env.make_transfer_plugin(active=True)
    data = env.plugin.transfers.data_root / target.name
    data.mkdir()
    (data / "state.txt").write_text("real data", encoding="utf-8")
    (data / "empty").mkdir()
    wrong = env.plugin.transfers.data_root / target.root_dir_name
    wrong.mkdir()
    (wrong / "state.txt").write_text("wrong data", encoding="utf-8")
    env.kv_values[("plugin", target.plugin_id)] = {
        "bytes": b"\x00\xff",
        "nested": {"type": "bytes", "value": "not a marker"},
    }
    env.kv_values[("plugin", "other/plugin")] = {"private": "not exported"}
    path = await export_package(env, target, data=True)
    manifest = unpack(path, tmp_path / "unpacked")
    prefix = tmp_path / "unpacked" / "payload" / target.root_dir_name
    assert (prefix / "data" / "state.txt").read_text() == "real data"
    assert (prefix / "data" / "empty").is_dir()
    assert set(load_json(prefix / "kv.json")) == {"bytes", "nested"}
    assert manifest["plugins"][0]["was_enabled"] is True
    assert env.plugin.service.resolve(target.name).plugin.activated
    assert target.module_path not in env.prefs["inactivated_plugins"]
    assert env.actions == ["terminate", "unbind", "load"]


async def test_bom_config_is_exported_and_restored_without_rewriting(transfer_env):
    env = transfer_env
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    config = transfers.config_root / (target.root_dir_name + "_config.json")
    content = '{"enabled":true}'.encode("utf-8-sig")
    config.write_bytes(content)
    path = await export_package(env, target, config=True)
    with zipfile.ZipFile(path) as archive:
        assert archive.read(f"payload/{target.root_dir_name}/config.json") == content
    assert config.read_bytes() == content
    config.write_bytes(b'{"enabled":false}')
    job, _ = await prepare(env, path)
    await transfers.start_import(
        job["id"],
        "admin",
        [
            {
                "directory": target.root_dir_name,
                "code": False,
                "config": True,
                "data": False,
            }
        ],
        True,
    )
    await transfers.task
    assert job["status"] == "completed", job
    assert config.read_bytes() == content
    env.manager._terminate_plugin.assert_not_awaited()
    env.manager.load.assert_not_awaited()


@pytest.mark.parametrize("change_after_start", [False, True])
async def test_incomplete_runtime_is_identified_before_pausing_any_plugin(
    transfer_env, change_after_start
):
    env = transfer_env
    ready = env.make_transfer_plugin(active=True)
    incomplete = env.make_transfer_plugin(
        name="incomplete_plugin", directory="incomplete_directory", active=True
    )
    transfers = env.plugin.transfers
    selections = [
        {"directory": target.root_dir_name, "config": False, "data": True}
        for target in (ready, incomplete)
    ]
    if change_after_start:
        preview = await transfers.export_preview(selections)
        started = await transfers.start_export(
            "admin", None, True, selections=preview["selections"]
        )
        incomplete.star_cls = None
        await transfers.task
        job = transfers.get_job(started["id"], "admin")
        assert job["status"] == "failed"
        assert "incomplete_directory" in job["error"]
        assert "没有已初始化的运行实例" in job["error"]
        assert not (transfers.job_directory(job["id"]) / "package.zip").exists()
    else:
        incomplete.star_cls = None
        with pytest.raises(
            ValueError, match="incomplete_directory.*没有已初始化的运行实例"
        ):
            await transfers.export_preview(selections)
        assert not transfers.jobs
    env.manager._terminate_plugin.assert_not_awaited()
    env.manager.load.assert_not_awaited()
    await export_package(env, incomplete)
    env.manager._terminate_plugin.assert_not_awaited()
    env.manager.load.assert_not_awaited()


async def test_stale_rule_and_missing_configuration_fail_without_pause(transfer_env):
    env = transfer_env
    target = env.make_transfer_plugin(active=True)
    transfers = env.plugin.transfers
    with pytest.raises(ValueError, match="没有已保存配置"):
        await transfers.save_rule(
            [{"directory": target.root_dir_name, "config": True, "data": False}]
        )
    await transfers.save_rule(
        [{"directory": target.root_dir_name, "config": False, "data": False}]
    )
    metadata_path = transfers.code_root / target.root_dir_name / "metadata.yaml"
    metadata_path.write_text(
        metadata_path.read_text(encoding="utf-8").replace("Tester", "Another"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="身份"):
        await transfers.export_preview()
    env.manager._terminate_plugin.assert_not_awaited()


async def test_new_import_restores_files_without_loading_or_installing_dependencies(
    transfer_env,
):
    env = transfer_env
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    path = await export_package(env, target)
    env.owners.pop(target.module_path)
    shutil.rmtree(transfers.code_root / target.root_dir_name)
    job, preview = await prepare(env, path)
    assert preview["plugins"][0]["exists"] is False
    await transfers.start_import(
        job["id"],
        "admin",
        [
            {
                "directory": target.root_dir_name,
                "code": True,
                "config": False,
                "data": False,
            }
        ],
        True,
    )
    await transfers.task
    assert job["status"] == "completed", job
    assert job["reload_required"] is True
    assert (transfers.code_root / target.root_dir_name / "main.py").exists()
    assert target.module_path in env.prefs["inactivated_plugins"]
    env.manager.load.assert_not_awaited()
    env.manager.install_plugin.assert_not_awaited()
    assert any(
        entry["status"] == "pending"
        for entry in await transfers.inventory()
        if entry["directory"] == target.root_dir_name
    )
    assert transfers.download(job["id"], "admin", "rollback").exists()


async def test_config_only_import_keeps_code_data_and_other_kv(transfer_env):
    env = transfer_env
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    config = transfers.config_root / (target.root_dir_name + "_config.json")
    config.write_text('{"enabled":true}', encoding="utf-8")
    data = transfers.data_root / target.name
    data.mkdir()
    (data / "state.txt").write_text("source", encoding="utf-8")
    path = await export_package(env, target, config=True, data=True)
    config.write_text('{"enabled":false}', encoding="utf-8")
    (data / "state.txt").write_text("target", encoding="utf-8")
    code = transfers.code_root / target.root_dir_name / "main.py"
    code.write_text("TARGET_CODE = True\n", encoding="utf-8")
    env.kv_values[("plugin", target.plugin_id)] = {"target": "keep"}
    env.kv_values[("plugin", "other/plugin")] = {"other": "keep"}
    job, _ = await prepare(env, path)
    await transfers.start_import(
        job["id"],
        "admin",
        [
            {
                "directory": target.root_dir_name,
                "code": False,
                "config": True,
                "data": False,
            }
        ],
        True,
    )
    await transfers.task
    assert job["status"] == "completed", job
    assert load_json(config) == {"enabled": True}
    assert code.read_text() == "TARGET_CODE = True\n"
    assert (data / "state.txt").read_text() == "target"
    assert env.kv_values[("plugin", target.plugin_id)] == {"target": "keep"}
    assert env.kv_values[("plugin", "other/plugin")] == {"other": "keep"}
    before = transfers.job_directory(job["id"]) / "before" / target.root_dir_name
    assert load_json(before / "config") == {"enabled": False}
    assert not (before / "code").exists()


async def test_data_import_replaces_namespace_including_bytes_but_not_other_plugins(
    transfer_env,
):
    env = transfer_env
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    data = transfers.data_root / target.name
    data.mkdir()
    (data / "state").write_bytes(b"original")
    env.kv_values[("plugin", target.plugin_id)] = {
        "secret": b"\x00\xff",
        "nested": [False, None, 1.25],
    }
    path = await export_package(env, target, data=True)
    (data / "state").write_bytes(b"new")
    (data / "obsolete").write_bytes(b"remove me")
    env.kv_values[("plugin", target.plugin_id)] = {"obsolete": True}
    env.kv_values[("plugin", "other/plugin")] = {"unchanged": True}
    job, _ = await prepare(env, path)
    await transfers.start_import(
        job["id"],
        "admin",
        [
            {
                "directory": target.root_dir_name,
                "code": False,
                "config": False,
                "data": True,
            }
        ],
        True,
    )
    await transfers.task
    assert job["status"] == "completed", job
    assert (data / "state").read_bytes() == b"original"
    assert not (data / "obsolete").exists()
    assert env.kv_values[("plugin", target.plugin_id)] == {
        "secret": b"\x00\xff",
        "nested": [False, None, 1.25],
    }
    assert env.kv_values[("plugin", "other/plugin")] == {"unchanged": True}


async def test_empty_data_snapshot_clears_target_only_when_selected(transfer_env):
    env = transfer_env
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    path = await export_package(env, target, data=True)
    data = transfers.data_root / target.name
    data.mkdir()
    (data / "new").write_text("new", encoding="utf-8")
    env.kv_values[("plugin", target.plugin_id)] = {"new": True}
    job, _ = await prepare(env, path)
    await transfers.start_import(
        job["id"],
        "admin",
        [
            {
                "directory": target.root_dir_name,
                "code": False,
                "config": False,
                "data": True,
            }
        ],
        True,
    )
    await transfers.task
    assert job["status"] == "completed", job
    assert not data.exists()
    assert env.kv_values[("plugin", target.plugin_id)] == {}


async def test_changed_target_and_invalid_config_are_rejected_before_writes(
    transfer_env,
):
    env = transfer_env
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    config = transfers.config_root / (target.root_dir_name + "_config.json")
    config.write_text('{"enabled":"invalid"}', encoding="utf-8")
    path = await export_package(env, target, config=True)
    job, _ = await prepare(env, path)
    with pytest.raises(ValueError, match="类型不匹配"):
        await transfers.start_import(
            job["id"],
            "admin",
            [
                {
                    "directory": target.root_dir_name,
                    "code": False,
                    "config": True,
                    "data": False,
                }
            ],
            True,
        )
    config.write_text('{"enabled":false}', encoding="utf-8")
    with pytest.raises(ValueError, match="状态或文件已变化"):
        await transfers.start_import(
            job["id"],
            "admin",
            [
                {
                    "directory": target.root_dir_name,
                    "code": True,
                    "config": False,
                    "data": False,
                }
            ],
            True,
        )
    env.manager._terminate_plugin.assert_not_awaited()


async def test_import_failure_restores_old_data_and_kv(transfer_env, monkeypatch):
    env = transfer_env
    target = env.make_transfer_plugin(active=True)
    transfers = env.plugin.transfers
    data = transfers.data_root / target.name
    data.mkdir()
    (data / "state").write_text("source", encoding="utf-8")
    env.kv_values[("plugin", target.plugin_id)] = {"key": "source"}
    path = await export_package(env, target, data=True)
    (data / "state").write_text("target", encoding="utf-8")
    env.kv_values[("plugin", target.plugin_id)] = {"key": "target"}
    original_write = transfers._write_kv
    calls = 0

    async def fail_once(plugin_id, values):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("Injected write failure")
        await original_write(plugin_id, values)

    monkeypatch.setattr(transfers, "_write_kv", fail_once)
    job, _ = await prepare(env, path)
    await transfers.start_import(
        job["id"],
        "admin",
        [
            {
                "directory": target.root_dir_name,
                "code": False,
                "config": False,
                "data": True,
            }
        ],
        True,
    )
    await transfers.task
    assert job["status"] == "failed", job
    assert (data / "state").read_text() == "target"
    assert env.kv_values[("plugin", target.plugin_id)] == {"key": "target"}
    assert env.plugin.service.resolve(target.name).plugin.activated
    assert job["backup_ready"]


async def test_busy_jobs_owner_expiration_and_interruptions_are_explicit(
    transfer_env, monkeypatch
):
    env = transfer_env
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    rule = await transfers.save_rule(
        [{"directory": target.root_dir_name, "config": False, "data": False}]
    )
    await env.manager._pm_lock.acquire()
    try:
        result = await transfers.start_export("admin", rule["updated_at"], True)
        with pytest.raises(ValueError, match="已有"):
            await transfers.start_export("admin", rule["updated_at"], True)
        with pytest.raises(ValueError, match="无权访问"):
            transfers.get_job(result["id"], "other")
        await transfers.close()
        assert transfers.jobs[result["id"]]["status"] == "interrupted"
    finally:
        env.manager._pm_lock.release()


async def test_registered_routes_are_removed_on_termination(transfer_env):
    env = transfer_env
    assert len(env.context.registered_web_apis) == 35
    env.plugin.manager_data.previews["pending"] = {}
    await env.plugin.terminate()
    assert env.context.registered_web_apis == []
    assert not env.plugin.manager_data.previews
    assert env.plugin.transfers.closing


def restore_selection(target, *, code=True, config=False, data=False):
    return {
        "directory": target.root_dir_name,
        "code": code,
        "config": config,
        "data": data,
    }


async def test_extra_staging_directory_prevents_any_live_changes(transfer_env):
    env = transfer_env
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    path = await export_package(env, target)
    job, _ = await prepare(env, path)
    checked = transfers.job_directory(job["id"]) / "checked"
    (checked / "payload" / target.root_dir_name / "code" / "extra_empty").mkdir()
    await transfers.start_import(job["id"], "admin", [restore_selection(target)], True)
    await transfers.task
    assert job["status"] == "failed"
    assert "目录发生变化" in job["error"]
    assert job["results"][0]["status"] == "not_executed"
    assert not job["backup_ready"]
    env.manager._terminate_plugin.assert_not_awaited()


async def test_failed_plugin_is_rolled_back_and_later_plugins_are_not_executed(
    transfer_env, monkeypatch
):
    env = transfer_env
    first = env.make_transfer_plugin(name="first", directory="first_dir", active=True)
    second = env.make_transfer_plugin(name="second", directory="second_dir")
    transfers = env.plugin.transfers
    for target in (first, second):
        data = transfers.data_root / target.name
        data.mkdir()
        (data / "empty").mkdir()
        (data / "state").write_text("exported", encoding="utf-8")
    rule = await transfers.save_rule(
        [
            {"directory": target.root_dir_name, "config": False, "data": True}
            for target in (first, second)
        ]
    )
    exported = await transfers.start_export("admin", rule["updated_at"], True)
    await transfers.task
    path = transfers.download(exported["id"], "admin", "package")
    for target in (first, second):
        (transfers.data_root / target.name / "state").write_text(
            "local", encoding="utf-8"
        )
    original = transfers._write_kv
    calls = 0

    async def fail_once(plugin_id, values):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("injected failure")
        await original(plugin_id, values)

    monkeypatch.setattr(transfers, "_write_kv", fail_once)
    job, _ = await prepare(env, path)
    await transfers.start_import(
        job["id"],
        "admin",
        [
            restore_selection(target, code=False, data=True)
            for target in (first, second)
        ],
        True,
    )
    await transfers.task
    assert job["status"] == "failed"
    assert [result["status"] for result in job["results"]] == ["failed", "not_executed"]
    for target in (first, second):
        assert (transfers.data_root / target.name / "state").read_text() == "local"
    assert env.plugin.service.resolve(first.name).plugin.activated
    directory = transfers.job_directory(job["id"])
    journal = load_json(directory / "rollback.json")
    assert journal["plugins"][0]["was_enabled"] is True
    assert journal["plugins"][0]["was_inactive"] is False
    assert journal["plugins"][0]["module_path"] == first.module_path
    with zipfile.ZipFile(directory / "rollback.zip") as archive:
        assert "before/first_dir/data/empty/" in archive.namelist()


async def test_preview_expiration_is_checked_before_start_and_after_queue(transfer_env):
    env = transfer_env
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    path = await export_package(env, target)
    job, _ = await prepare(env, path)
    job["created_at"] = time.time() - 3601
    with pytest.raises(ValueError, match="过期"):
        await transfers.start_import(
            job["id"], "admin", [restore_selection(target)], True
        )
    job["created_at"] = time.time()
    async with env.manager._pm_lock:
        await transfers.start_import(
            job["id"], "admin", [restore_selection(target)], True
        )
        job["created_at"] = time.time() - 3601
    await transfers.task
    assert job["status"] == "failed"
    assert "排队期间过期" in job["error"]
    assert not job["backup_ready"]


async def test_old_downloads_remain_available_without_retention_deadline(transfer_env):
    env = transfer_env
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    await export_package(env, target)
    job = transfers.jobs[transfers.active_job]
    job["created_at"] = time.time() - 8 * 86400
    assert not transfers.history("admin")[0]["expired"]
    assert (transfers.job_directory(job["id"]) / "package.zip").exists()
    assert transfers.download(job["id"], "admin", "package").is_file()


async def test_interrupted_import_recovery_files_are_not_automatically_expired(
    transfer_env,
):
    transfers = transfer_env.plugin.transfers
    job = transfers.new_upload("admin")
    directory = transfers.job_directory(job["id"])
    (directory / "rollback.zip").write_bytes(b"recovery information")
    job.update(status="running", backup_ready=True, created_at=time.time() - 30 * 86400)
    transfers._save(job)
    restarted = TransferService(
        transfer_env.plugin.service, transfer_env.plugin._operations
    )
    await restarted.initialize()
    interrupted = restarted.get_job(job["id"], "admin")
    assert interrupted["status"] == "interrupted"
    assert not restarted.public_job(interrupted)["expired"]
    assert restarted.download(job["id"], "admin", "rollback").is_file()
    transfer_env.manager.load.assert_not_awaited()


@pytest.mark.parametrize("failure", ["missing", "not_initialized"])
async def test_resume_verifies_registration_and_initialization(transfer_env, failure):
    env = transfer_env
    target = env.make_transfer_plugin(active=True)
    original_load = env.manager.load.side_effect

    async def invalid_load(*, specified_module_path):
        if failure == "not_initialized":
            await original_load(specified_module_path=specified_module_path)
            env.owners[specified_module_path].star_cls = None
        return True, None

    env.manager.load.side_effect = invalid_load
    await export_package(env, target, data=True)
    transfers = env.plugin.transfers
    job = transfers.jobs[transfers.active_job]
    assert job["status"] == "completed_with_warning"
    assert job["warnings"]
    assert target.module_path in await env.preferences.global_get(
        "inactivated_plugins", []
    )
    assert not any(
        plugin.activated
        for plugin in env.context.get_all_stars()
        if plugin.module_path == target.module_path
    )


async def test_small_transfers_work_without_gigabytes_of_free_space(
    transfer_env, monkeypatch
):
    env = transfer_env
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    monkeypatch.setattr(
        "astrbot_plugin_manager.transfers.shutil.disk_usage",
        lambda path: SimpleNamespace(free=100 * 1024 * 1024),
    )
    path = await export_package(env, target)
    job, _ = await prepare(env, path)
    await transfers.start_import(job["id"], "admin", [restore_selection(target)], True)
    await transfers.task
    assert job["status"] == "completed", job


async def test_insufficient_space_fails_before_pausing_plugins(
    transfer_env, monkeypatch
):
    env = transfer_env
    target = env.make_transfer_plugin(active=True)
    transfers = env.plugin.transfers
    monkeypatch.setattr(
        "astrbot_plugin_manager.transfers.shutil.disk_usage",
        lambda path: SimpleNamespace(free=0),
    )
    rule = await transfers.save_rule(
        [{"directory": target.root_dir_name, "config": False, "data": True}]
    )
    result = await transfers.start_export("admin", rule["updated_at"], True)
    await transfers.task
    job = transfers.get_job(result["id"], "admin")
    assert job["status"] == "failed"
    assert "空间不足" in job["error"]
    env.manager._terminate_plugin.assert_not_awaited()


async def test_missing_dependency_is_visible_at_preview_and_blocks_restore(
    transfer_env,
):
    env = transfer_env
    target = env.make_transfer_plugin()
    code = Path(env.manager.plugin_store_path) / target.root_dir_name
    (code / "requirements.txt").write_text(
        "definitely-missing-transfer-package>=1", encoding="utf-8"
    )
    path = await export_package(env, target)
    job, preview = await prepare(env, path)
    assert "缺少依赖" in preview["plugins"][0]["problem"]
    with pytest.raises(ValueError, match="缺少依赖"):
        await env.plugin.transfers.start_import(
            job["id"], "admin", [restore_selection(target)], True
        )
    assert not job["backup_ready"]
    env.manager.load.assert_not_awaited()


async def test_concurrent_rule_saves_are_serialized(transfer_env):
    env = transfer_env
    first = env.make_transfer_plugin(name="first", directory="first_dir")
    second = env.make_transfer_plugin(name="second", directory="second_dir")
    transfers = env.plugin.transfers
    results = await asyncio.gather(
        *[
            transfers.save_rule(
                [{"directory": target.root_dir_name, "config": False, "data": False}]
            )
            for target in (first, second)
        ]
    )
    assert transfers.rule() in results
    assert not (transfers.root / "rule.json.tmp").exists()


async def test_concurrent_export_starts_do_not_leave_unstarted_jobs(
    transfer_env, monkeypatch
):
    env = transfer_env
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    rule = await transfers.save_rule(
        [{"directory": target.root_dir_name, "config": False, "data": False}]
    )
    barrier = asyncio.Event()
    original_sources = transfers._rule_sources
    calls = 0

    async def synchronized_sources(rule):
        nonlocal calls
        sources = await original_sources(rule)
        calls += 1
        if calls == 2:
            barrier.set()
        await barrier.wait()
        return sources

    monkeypatch.setattr(transfers, "_rule_sources", synchronized_sources)
    async with env.manager._pm_lock:
        results = await asyncio.gather(
            transfers.start_export("admin", rule["updated_at"], True),
            transfers.start_export("admin", rule["updated_at"], True),
            return_exceptions=True,
        )
        assert len(transfers.jobs) == 1
        assert sum(isinstance(result, dict) for result in results) == 1
        assert sum(isinstance(result, ValueError) for result in results) == 1
        await transfers.close()


async def test_unregistered_same_name_in_another_directory_blocks_import(transfer_env):
    env = transfer_env
    incoming = env.make_transfer_plugin(name="duplicate", directory="incoming_dir")
    path = await export_package(env, incoming)
    env.owners.pop(incoming.module_path)
    await asyncio.to_thread(
        remove_path, Path(env.manager.plugin_store_path), incoming.root_dir_name
    )
    pending = env.make_transfer_plugin(name="duplicate", directory="pending_dir")
    env.owners.pop(pending.module_path)
    with pytest.raises(ValueError, match="同名插件"):
        await prepare(env, path)
    env.manager.load.assert_not_awaited()


async def test_failed_automatic_restore_keeps_recovery_files_indefinitely(
    transfer_env, monkeypatch
):
    env = transfer_env
    target = env.make_transfer_plugin(active=True)
    transfers = env.plugin.transfers
    data = transfers.data_root / target.name
    data.mkdir()
    (data / "state").write_text("exported", encoding="utf-8")
    path = await export_package(env, target, data=True)
    (data / "state").write_text("local", encoding="utf-8")
    job, _ = await prepare(env, path)

    async def fail_write(plugin_id, values):
        raise ValueError("injected KV failure")

    monkeypatch.setattr(transfers, "_write_kv", fail_write)
    await transfers.start_import(
        job["id"], "admin", [restore_selection(target, code=False, data=True)], True
    )
    await transfers.task
    assert job["status"] == "rollback_failed"
    assert job["results"][0]["status"] == "rollback_failed"
    assert job["backup_ready"]
    job["created_at"] = time.time() - 30 * 86400
    assert not transfers.public_job(job)["expired"]
    assert transfers.download(job["id"], "admin", "rollback").is_file()
