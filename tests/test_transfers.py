"""End-to-end transfer behavior with real registries and isolated side effects."""

import asyncio
import copy
import shutil
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot_plugin_manager.transfer_bundle import load_json, remove_path, unpack
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


@pytest.mark.parametrize("fail_after_source_change", [False, True])
async def test_code_migration_clears_bindings_and_rollback_restores_original_source(
    transfer_env, monkeypatch, fail_after_source_change
):
    env = transfer_env
    target = env.make_transfer_plugin()
    sources, transfers = env.plugin.sources, env.plugin.transfers
    env.prefs["plugin_install_sources"] = {
        target.root_dir_name: {
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

        async def fail(directory, actor):
            await saved(directory, actor)
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
        assert not current["bindings"] and current["active_binding_id"] is None
        assert target.root_dir_name not in env.prefs["plugin_install_sources"]


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
    assert len(env.context.registered_web_apis) == 19
    await env.plugin.terminate()
    assert env.context.registered_web_apis == []
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


async def test_download_expiration_does_not_depend_on_new_jobs(transfer_env):
    env = transfer_env
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    await export_package(env, target)
    job = transfers.jobs[transfers.active_job]
    job["created_at"] = time.time() - 8 * 86400
    assert transfers.history("admin")[0]["expired"]
    assert (transfers.job_directory(job["id"]) / "package.zip").exists()
    with pytest.raises(ValueError, match="保留期"):
        transfers.download(job["id"], "admin", "package")


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
    transfers._cleanup()
    assert not transfers.public_job(job)["expired"]
    assert transfers.download(job["id"], "admin", "rollback").is_file()
