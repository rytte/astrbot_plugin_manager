"""The manager's own standard data cannot be exported or restored."""

import copy
import shutil
import zipfile

import pytest
import yaml
from astrbot_plugin_manager.transfer_bundle import pack, unpack, write_json


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


async def export_zip(env, targets, *, data=False):
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


async def test_manager_data_is_unavailable_and_code_export_stays_running(transfer_env):
    env = transfer_env
    manager = await seed_manager(env)
    transfers = env.plugin.transfers
    item = next(
        item
        for item in await transfers.inventory(statistics=True)
        if item["directory"] == manager.root_dir_name
    )
    assert not item["data_available"]
    assert item["data_size"] == 0 and item["kv_count"] == 0
    assert not item["ranges"]
    preview = await transfers.export_preview(
        [{"directory": manager.root_dir_name, "data": False, "config": False}]
    )
    assert not preview["plugins"][0]["pause_required"]
    package = await export_zip(env, [manager])
    with zipfile.ZipFile(package) as archive:
        names = archive.namelist()
        assert not any(
            "/data/" in name or name.endswith(("/kv.json", "/manager-data.json"))
            for name in names
        )
    env.preferences.range_get_async.assert_not_awaited()
    env.manager._terminate_plugin.assert_not_awaited()
    env.manager._unbind_plugin.assert_not_awaited()
    env.manager.load.assert_not_awaited()


@pytest.mark.parametrize("entrypoint", ["preview", "rule", "export"])
async def test_backend_rejects_manager_standard_data(transfer_env, entrypoint):
    env = transfer_env
    manager = await seed_manager(env)
    transfers = env.plugin.transfers
    selected = [{"directory": manager.root_dir_name, "data": True, "config": False}]
    with pytest.raises(ValueError, match="标准数据不可用"):
        if entrypoint == "preview":
            await transfers.export_preview(selected)
        elif entrypoint == "rule":
            await transfers.save_rule(selected)
        else:
            selected[0].update(name=manager.name, plugin_id=manager.plugin_id)
            await transfers.start_export("admin", None, True, selections=selected)
    assert not transfers.jobs
    env.preferences.range_get_async.assert_not_awaited()
    env.manager._terminate_plugin.assert_not_awaited()


@pytest.mark.parametrize("component", ["code", "config", "data"])
async def test_backend_rejects_online_manager_restore(transfer_env, component):
    env = transfer_env
    manager = await seed_manager(env)
    transfers = env.plugin.transfers
    job, preview = await prepare(env, await export_zip(env, [manager]))
    item = preview["plugins"][0]
    assert not item["restore_available"]
    assert "不能在线恢复插件管家" in item["problem"]
    with pytest.raises(ValueError, match="不能在线恢复插件管家"):
        await transfers.start_import(
            job["id"],
            "admin",
            [
                selection(
                    manager,
                    code=component == "code",
                    config=component == "config",
                    data=component == "data",
                )
            ],
            True,
        )
    assert job["status"] == "preview"
    assert not job["backup_ready"]
    env.manager._terminate_plugin.assert_not_awaited()


@pytest.mark.parametrize(
    "payload", ["manager-data.json", "kv.json", "data/private.json"]
)
async def test_manager_data_payloads_are_rejected(transfer_env, tmp_path, payload):
    env = transfer_env
    manager = await seed_manager(env)
    stage = tmp_path / "stage"
    manifest = unpack(await export_zip(env, [manager]), stage)
    (stage / "manifest.json").unlink()
    manifest["plugins"][0].update(data=True, file_data=payload.startswith("data/"))
    destination = stage / "payload" / manager.root_dir_name / payload
    destination.parent.mkdir(parents=True, exist_ok=True)
    write_json(destination, {})
    package = tmp_path / "modified.zip"
    pack(stage, package, manifest)
    with pytest.raises(ValueError, match="标准数据不可用"):
        await prepare(env, package)
    env.manager._terminate_plugin.assert_not_awaited()
    env.preferences.clear_async.assert_not_awaited()


async def test_bundle_with_manager_code_can_restore_other_plugins(transfer_env):
    env = transfer_env
    manager = await seed_manager(env)
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    config = transfers.config_root / (manager.root_dir_name + "_config.json")
    write_json(config, {"allow_prefix_omission": True})
    rule = await transfers.save_rule(
        [
            {"directory": manager.root_dir_name, "data": False, "config": True},
            {"directory": target.root_dir_name, "data": True, "config": False},
        ]
    )
    original_rule = copy.deepcopy(rule)
    checked = await transfers.export_preview()
    result = await transfers.start_export("admin", rule["updated_at"], True)
    await transfers.task
    package = transfers.download(result["id"], "admin", "package")
    assert checked["plugins"][0]["config"]
    with zipfile.ZipFile(package) as archive:
        assert f"payload/{manager.root_dir_name}/config.json" in archive.namelist()
    imported, preview = await prepare(env, package)
    assert any(item.get("restore_available") is False for item in preview["plugins"])
    result = await transfers.start_import(
        imported["id"], "admin", [selection(target, code=True)], True
    )
    await transfers.task
    job = transfers.get_job(result["id"], "admin")
    assert job["status"] == "completed", job
    assert transfers.rule() == original_rule
    assert env.context.get_registered_star(manager.name) is manager
    assert manager.activated
    assert not (
        transfers.job_directory(imported["id"]) / "before" / manager.root_dir_name
    ).exists()
    rollback = await transfers.recovery.preview("admin", imported["id"])
    assert [item["directory"] for item in rollback["plugins"]] == [target.root_dir_name]
