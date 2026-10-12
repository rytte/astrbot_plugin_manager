"""Portable manager data cannot activate sources or replay instance-local jobs."""

import copy
import json
import shutil
import zipfile

import pytest
import yaml
from astrbot_plugin_manager.manager_data_bundle import FORMAT, validate_manager_data
from astrbot_plugin_manager.source_bundle import portable_binding
from astrbot_plugin_manager.transfer_bundle import pack, unpack, write_json


def manager_data(*, name="demo_plugin", author="Tester", directory="old_directory"):
    binding = portable_binding(
        {"kind": "repository", "repo_url": "https://github.com/test/new"}
    )
    return {
        "format": FORMAT,
        "schema_version": 1,
        "name": "管家备份",
        "sources": [
            {
                "directory": directory,
                "name": name,
                "author": author,
                "bindings": [binding],
                "active_binding_id": binding["id"],
            }
        ],
        "rule": {
            "rule_version": 1,
            "updated_at": 1,
            "selections": [
                {
                    "directory": directory,
                    "name": name,
                    "plugin_id": f"{author.lower()}/{name.lower()}",
                    "config": False,
                    "data": False,
                }
            ],
        },
    }


@pytest.mark.parametrize(
    "attack",
    [
        "format",
        "version",
        "history",
        "jobs",
        "trust",
        "credentials",
        "duplicate",
        "rule",
        "timestamp",
        "identity",
    ],
)
def test_manager_data_rejects_unknown_fields_inherited_trust_and_invalid_rules(attack):
    value = manager_data()
    if attack == "format":
        value["format"] = "astrbot-plugin-manager-manifest"
    elif attack == "version":
        value["schema_version"] = True
    elif attack in {"history", "jobs"}:
        value[attack] = []
    elif attack == "trust":
        value["sources"][0]["bindings"][0]["verification"] = "identity_matched"
    elif attack == "credentials":
        value["sources"][0]["bindings"][0]["repo_url"] = (
            "https://user:secret@github.com/test/new"
        )
    elif attack == "duplicate":
        value["sources"].append(copy.deepcopy(value["sources"][0]))
    elif attack == "rule":
        value["rule"]["selections"][0]["data"] = 1
    elif attack == "timestamp":
        value["rule"]["updated_at"] = float("nan")
    else:
        value["sources"][0]["author"] = "Tester,"
    with pytest.raises(ValueError):
        validate_manager_data(value)


async def seed_manager(env):
    target = env.context.get_registered_star("astrbot_plugin_manager")
    target.author, target.version, target.display_name = "Tester", "1.0", "插件管家"
    root = env.plugin.transfers.code_root / target.root_dir_name
    (root / "metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "name": target.name,
                "author": target.author,
                "version": "1.0",
                "desc": "Manager",
                "display_name": target.display_name,
            }
        ),
        encoding="utf-8",
    )
    (root / "main.py").write_text(
        "raise RuntimeError('must not execute')", encoding="utf-8"
    )
    record = await env.plugin.sources.detail(target.root_dir_name)
    await env.plugin.sources.sync_metadata(
        "admin", target.root_dir_name, record["revision"]
    )
    return target


async def export_zip(env, targets, *, data=True):
    transfers = env.plugin.transfers
    selections = [
        {"directory": target.root_dir_name, "data": data, "config": False}
        for target in targets
    ]
    preview = await transfers.export_preview(selections)
    result = await transfers.start_export(
        "admin", None, True, selections=preview["selections"]
    )
    await transfers.task
    job = transfers.get_job(result["id"], "admin")
    assert job["status"] in {"completed", "completed_with_warning"}, job
    return transfers.download(job["id"], "admin", "package")


async def prepare(env, package):
    transfers = env.plugin.transfers
    job = transfers.new_upload("admin")
    shutil.copyfile(package, transfers.job_directory(job["id"]) / "upload.zip")
    preview = await transfers.prepare_import(job)
    return job, preview


def selection(target, **components):
    return {
        "directory": target.root_dir_name,
        "code": False,
        "config": False,
        "data": True,
        **components,
    }


def rewrite(package, tmp_path, target, value=None, attack=None):
    stage = tmp_path / "stage"
    manifest = unpack(package, stage)
    (stage / "manifest.json").unlink()
    prefix = stage / "payload" / target.root_dir_name
    if value is not None:
        write_json(prefix / "manager-data.json", value)
    if attack == "file_data":
        manifest["plugins"][0]["file_data"] = True
    elif attack == "directory":
        (prefix / "data").mkdir()
    elif attack == "missing":
        (prefix / "manager-data.json").unlink()
    elif attack:
        write_json(prefix / attack, {})
    output = tmp_path / "edited.zip"
    pack(stage, output, manifest)
    return output


@pytest.mark.parametrize("data", [False, True])
async def test_manager_data_is_opt_in_zip_component_and_does_not_pause(
    transfer_env, data
):
    env = transfer_env
    manager = await seed_manager(env)
    transfers = env.plugin.transfers
    inventory = await transfers.inventory(statistics=True)
    item = next(
        item for item in inventory if item["directory"] == manager.root_dir_name
    )
    assert item["data_available"] and item["manager_data"]
    assert "仅导出" in item["ranges"][0]
    preview = await transfers.export_preview(
        [{"directory": manager.root_dir_name, "data": data, "config": False}]
    )
    assert not preview["plugins"][0]["pause_required"]
    package = await export_zip(env, [manager], data=data)
    with zipfile.ZipFile(package) as archive:
        names = archive.namelist()
        assert (f"payload/{manager.root_dir_name}/manager-data.json" in names) is data
        assert not any("/data/" in name or name.endswith("/kv.json") for name in names)
    env.manager._terminate_plugin.assert_not_awaited()
    env.manager._unbind_plugin.assert_not_awaited()
    env.preferences.range_get_async.assert_not_awaited()
    job, preview = await prepare(env, package)
    assert preview["plugins"][0]["manager_data"]
    assert preview["plugins"][0]["exists"]
    if not data:
        with pytest.raises(ValueError, match="未携带"):
            await transfers.start_import(job["id"], "admin", [selection(manager)], True)


async def test_zip_snapshot_has_all_plugin_sources_rules_and_no_private_state(
    transfer_env,
):
    env = transfer_env
    manager = await seed_manager(env)
    target = env.make_transfer_plugin()
    env.prefs["plugin_install_sources"] = {
        target.root_dir_name: {
            "install_method": "repository",
            "repo": "https://user:secret@github.com/test/demo?token=secret",
        }
    }
    rule = await env.plugin.transfers.save_rule(
        [{"directory": target.root_dir_name, "data": False, "config": False}]
    )
    package = await export_zip(env, [manager])
    with zipfile.ZipFile(package) as archive:
        raw = archive.read(f"payload/{manager.root_dir_name}/manager-data.json")
        value = json.loads(raw)
        validate_manager_data(value)
        assert value["rule"] == rule
        assert value["sources"][0]["name"] == target.name
        assert (
            value["sources"][0]["bindings"][0]["repo_url"]
            == "https://github.com/test/demo"
        )
        assert not any(
            field in raw
            for field in (
                b"secret",
                b"history",
                b"job.json",
                b"checked_at",
                b"verification",
            )
        )
        assert f"payload/{manager.root_dir_name}/sources.json" in archive.namelist()


async def test_zip_import_merges_sources_maps_rule_preserves_core_and_manager(
    transfer_env, tmp_path
):
    env = transfer_env
    manager = await seed_manager(env)
    target = env.make_transfer_plugin(directory="new_directory", active=True)
    env.prefs["plugin_install_sources"] = {
        target.root_dir_name: {
            "install_method": "repository",
            "repo": "https://github.com/test/current",
        }
    }
    current = copy.deepcopy(
        (await env.plugin.sources.detail(target.root_dir_name))["bindings"][0]
    )
    before_prefs = copy.deepcopy(env.prefs)
    package = rewrite(
        await export_zip(env, [manager]), tmp_path, manager, manager_data()
    )
    job, _ = await prepare(env, package)
    await env.plugin.transfers.start_import(
        job["id"], "admin", [selection(manager)], True
    )
    await env.plugin.transfers.task
    assert job["status"] == "completed", job
    record = await env.plugin.sources.detail(target.root_dir_name)
    assert record["bindings"][0] == current
    assert record["active_binding_id"] == current["id"]
    assert record["bindings"][1]["verification"] == "imported"
    assert record["bindings"][1]["checked_at"] is None
    assert record["bindings"][1]["imported_active"] is True
    assert env.plugin.transfers.rule()["selections"][0]["directory"] == "new_directory"
    assert env.prefs == before_prefs
    assert not job.get("reload_required")
    env.manager._terminate_plugin.assert_not_awaited()
    env.manager._unbind_plugin.assert_not_awaited()
    env.manager.load.assert_not_awaited()
    repeated, _ = await prepare(env, package)
    await env.plugin.transfers.start_import(
        repeated["id"], "admin", [selection(manager)], True
    )
    await env.plugin.transfers.task
    assert repeated["status"] == "completed"
    assert len((await env.plugin.sources.detail(target.root_dir_name))["bindings"]) == 2


@pytest.mark.parametrize(
    "attack",
    ["kv.json", "directory", "file_data", "missing", "sources-store.json", "job.json"],
)
async def test_zip_rejects_full_manager_data_and_undeclared_files(
    transfer_env, tmp_path, attack
):
    env = transfer_env
    manager = await seed_manager(env)
    package = rewrite(
        await export_zip(env, [manager]), tmp_path, manager, attack=attack
    )
    with pytest.raises((ValueError, OSError)):
        await prepare(env, package)
    env.manager._terminate_plugin.assert_not_awaited()


@pytest.mark.parametrize("kind", ["code", "config"])
async def test_manager_code_and_configuration_cannot_be_restored(transfer_env, kind):
    env = transfer_env
    manager = await seed_manager(env)
    job, _ = await prepare(env, await export_zip(env, [manager]))
    with pytest.raises(ValueError, match="不能在线覆盖"):
        await env.plugin.transfers.start_import(
            job["id"], "admin", [selection(manager, **{kind: True})], True
        )
    env.manager._terminate_plugin.assert_not_awaited()


@pytest.mark.parametrize("change", ["source", "rule", "queue"])
async def test_changed_manager_state_rejects_import_without_overwriting(
    transfer_env, change
):
    env = transfer_env
    manager = await seed_manager(env)
    job, _ = await prepare(env, await export_zip(env, [manager]))
    transfers = env.plugin.transfers
    if change == "rule":
        await transfers.save_rule([])
    elif change == "source":
        env.prefs["plugin_install_sources"] = {
            manager.root_dir_name: {
                "install_method": "repository",
                "repo": "https://github.com/test/current",
            }
        }
    if change == "queue":
        async with env.manager._pm_lock:
            await transfers.start_import(job["id"], "admin", [selection(manager)], True)
            write_json(
                transfers.rule_path,
                {"rule_version": 1, "updated_at": 42, "selections": []},
            )
        await transfers.task
        assert job["status"] == "failed"
        assert "来源关联或导出规则" in job["error"]
    else:
        with pytest.raises(ValueError, match="来源关联或导出规则"):
            await transfers.start_import(job["id"], "admin", [selection(manager)], True)
    assert not job.get("backup_ready")


@pytest.mark.parametrize("rollback_fails", [False, True])
async def test_manager_failure_uses_zip_recovery_without_touching_running_tasks(
    transfer_env, tmp_path, monkeypatch, rollback_fails
):
    from astrbot_plugin_manager import manager_data as module

    env = transfer_env
    manager = await seed_manager(env)
    env.make_transfer_plugin()
    await env.plugin.transfers.save_rule([])
    package = rewrite(
        await export_zip(env, [manager]), tmp_path, manager, manager_data()
    )
    job, _ = await prepare(env, package)
    transfers = env.plugin.transfers
    before = transfers.manager_data.capture()
    original = module.write_json
    failures = []

    def fail_rule(path, value):
        if path == transfers.rule_path and (not failures or rollback_fails):
            failures.append(True)
            raise OSError("write failure")
        return original(path, value)

    monkeypatch.setattr(module, "write_json", fail_rule)
    await transfers.start_import(job["id"], "admin", [selection(manager)], True)
    await transfers.task
    assert job["status"] == ("rollback_failed" if rollback_fails else "failed"), job
    assert transfers.manager_data.capture() == before
    assert (
        transfers.job_directory(job["id"])
        / "before"
        / manager.root_dir_name
        / "manager-data.json"
    ).is_file()
    assert transfers.download(job["id"], "admin", "rollback").is_file()
    env.manager._terminate_plugin.assert_not_awaited()
    env.manager._unbind_plugin.assert_not_awaited()


async def test_page_rollback_restores_manager_data_ignores_task_writes_and_stays_running(
    transfer_env, tmp_path
):
    env = transfer_env
    manager = await seed_manager(env)
    env.make_transfer_plugin()
    transfers = env.plugin.transfers
    package = rewrite(
        await export_zip(env, [manager]), tmp_path, manager, manager_data()
    )
    imported, _ = await prepare(env, package)
    before = transfers.manager_data.capture()
    await transfers.start_import(imported["id"], "admin", [selection(manager)], True)
    await transfers.task
    assert imported["status"] == "completed", imported
    preview = await transfers.recovery.preview("admin", imported["id"])
    assert preview["plugins"][0]["manager_data"]
    assert not preview["plugins"][0]["problem"]
    write_json(
        transfers.job_directory(imported["id"]) / "unrelated-task.json",
        {"progress": 123},
    )
    result = await transfers.recovery.start(
        "admin", preview["job"]["id"], [selection(manager)], True
    )
    await transfers.task
    job = transfers.get_job(result["id"], "admin")
    assert job["status"] == "completed", job
    assert transfers.manager_data.capture() == before
    assert not job.get("reload_required")
    assert (transfers.job_directory(imported["id"]) / "unrelated-task.json").exists()
    env.manager._terminate_plugin.assert_not_awaited()
    env.manager._unbind_plugin.assert_not_awaited()


async def test_manager_data_restores_after_new_plugins_in_same_zip(transfer_env):
    env = transfer_env
    manager = await seed_manager(env)
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    env.prefs["plugin_install_sources"] = {
        target.root_dir_name: {
            "install_method": "repository",
            "repo": "https://github.com/test/current",
        }
    }
    await transfers.save_rule(
        [
            {"directory": target.root_dir_name, "data": True, "config": False},
            {"directory": manager.root_dir_name, "data": True, "config": False},
        ]
    )
    package = await export_zip(env, [manager, target])
    env.owners.pop(target.module_path)
    shutil.rmtree(transfers.code_root / target.root_dir_name)
    env.prefs["plugin_install_sources"] = {}
    env.plugin.sources.path.unlink()
    await transfers.save_rule([])
    imported, _ = await prepare(env, package)
    await transfers.start_import(
        imported["id"],
        "admin",
        [selection(manager), selection(target, code=True)],
        True,
    )
    await transfers.task
    assert imported["status"] == "completed", imported
    assert [item["directory"] for item in imported["results"]] == [
        target.root_dir_name,
        manager.root_dir_name,
    ]
    assert len(transfers.rule()["selections"]) == 2
    assert (await env.plugin.sources.detail(target.root_dir_name))["bindings"][0][
        "verification"
    ] == "imported"
    assert env.context.get_registered_star(manager.name) is manager
    assert all(
        call.args[0].module_path != manager.module_path
        for call in env.manager._terminate_plugin.call_args_list
    )


async def test_invalid_rule_targets_fail_before_any_restore(transfer_env, tmp_path):
    env = transfer_env
    manager = await seed_manager(env)
    package = rewrite(
        await export_zip(env, [manager]), tmp_path, manager, manager_data()
    )
    imported, _ = await prepare(env, package)
    with pytest.raises(ValueError, match="导出规则无法恢复"):
        await env.plugin.transfers.start_import(
            imported["id"], "admin", [selection(manager)], True
        )
    assert not imported.get("backup_ready")


@pytest.mark.parametrize("problem", ["missing", "author"])
async def test_unmatched_source_is_reported_without_inheriting_identity(
    transfer_env, tmp_path, problem
):
    env = transfer_env
    manager = await seed_manager(env)
    if problem == "author":
        env.make_transfer_plugin()
    value = manager_data(author="Someone else" if problem == "author" else "Tester")
    value["rule"]["selections"] = []
    package = rewrite(await export_zip(env, [manager]), tmp_path, manager, value)
    imported, _ = await prepare(env, package)
    await env.plugin.transfers.start_import(
        imported["id"], "admin", [selection(manager)], True
    )
    await env.plugin.transfers.task
    assert imported["status"] == "completed_with_warning", imported
    assert "未恢复来源关联" in imported["warnings"][0]
    assert not any(
        record["bindings"] for record in await env.plugin.sources.inventory()
    )


@pytest.mark.parametrize("change", ["source", "rule"])
async def test_manual_rollback_detects_later_source_changes(transfer_env, change):
    env = transfer_env
    manager = await seed_manager(env)
    transfers = env.plugin.transfers
    imported, _ = await prepare(env, await export_zip(env, [manager]))
    await transfers.start_import(imported["id"], "admin", [selection(manager)], True)
    await transfers.task
    preview = await transfers.recovery.preview("admin", imported["id"])
    if change == "rule":
        await transfers.save_rule([])
    else:
        env.prefs["plugin_install_sources"] = {
            manager.root_dir_name: {
                "install_method": "repository",
                "repo": "https://github.com/test/new",
            }
        }
    with pytest.raises(ValueError, match="预检后变化"):
        await transfers.recovery.start(
            "admin", preview["job"]["id"], [selection(manager)], True
        )


async def test_combined_page_restore_preserves_original_plugin_sources(transfer_env):
    env = transfer_env
    manager = await seed_manager(env)
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    env.prefs["plugin_install_sources"] = {
        target.root_dir_name: {
            "install_method": "repository",
            "repo": "https://github.com/test/original",
        }
    }
    original = copy.deepcopy(
        (await env.plugin.sources.detail(target.root_dir_name))["bindings"]
    )
    package = await export_zip(env, [manager, target])
    before_store = env.plugin.sources._store()
    imported, _ = await prepare(env, package)
    await transfers.start_import(
        imported["id"],
        "admin",
        [selection(manager), selection(target, code=True)],
        True,
    )
    await transfers.task
    assert imported["status"] == "completed", imported
    preview = await transfers.recovery.preview("admin", imported["id"])
    assert not any(item["problem"] for item in preview["plugins"])
    result = await transfers.recovery.start(
        "admin",
        preview["job"]["id"],
        [selection(target, code=True), selection(manager)],
        True,
    )
    await transfers.task
    recovered = transfers.get_job(result["id"], "admin")
    assert recovered["status"] == "completed", recovered
    assert (
        env.plugin.sources._store()["plugins"][target.root_dir_name]
        == before_store["plugins"][target.root_dir_name]
    )
    assert (await env.plugin.sources.detail(target.root_dir_name))[
        "bindings"
    ] == original
    assert env.context.get_registered_star(manager.name) is manager


@pytest.mark.parametrize("attack", ["history", "raw_sources"])
async def test_zip_rejects_unrestricted_manager_json_schema(
    transfer_env, tmp_path, attack
):
    env = transfer_env
    manager = await seed_manager(env)
    value = manager_data()
    if attack == "history":
        value["history"] = []
    else:
        value["sources"] = env.plugin.sources._store()
    package = rewrite(await export_zip(env, [manager]), tmp_path, manager, value)
    with pytest.raises(ValueError):
        await prepare(env, package)


async def test_manager_rollback_rejects_unexpected_local_files(transfer_env):
    env = transfer_env
    manager = await seed_manager(env)
    transfers = env.plugin.transfers
    imported, _ = await prepare(env, await export_zip(env, [manager]))
    await transfers.start_import(imported["id"], "admin", [selection(manager)], True)
    await transfers.task
    (
        transfers.job_directory(imported["id"])
        / "before"
        / manager.root_dir_name
        / "kv.json"
    ).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="范围外"):
        await transfers.recovery.preview("admin", imported["id"])


async def test_rule_can_use_configuration_restored_in_same_zip_without_code(
    transfer_env,
):
    env = transfer_env
    manager = await seed_manager(env)
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    config = transfers.config_root / (target.root_dir_name + "_config.json")
    write_json(config, {"enabled": True})
    await transfers.save_rule(
        [{"directory": target.root_dir_name, "config": True, "data": False}]
    )
    selected = [
        {"directory": manager.root_dir_name, "config": False, "data": True},
        {"directory": target.root_dir_name, "config": True, "data": False},
    ]
    checked = await transfers.export_preview(selected)
    result = await transfers.start_export(
        "admin", None, True, selections=checked["selections"]
    )
    await transfers.task
    package = transfers.download(result["id"], "admin", "package")
    config.unlink()
    await transfers.save_rule([])
    imported, _ = await prepare(env, package)
    await transfers.start_import(
        imported["id"],
        "admin",
        [selection(manager), selection(target, config=True, data=False)],
        True,
    )
    await transfers.task
    assert imported["status"] == "completed", imported
    assert config.is_file()
    assert transfers.rule()["selections"][0]["config"]
