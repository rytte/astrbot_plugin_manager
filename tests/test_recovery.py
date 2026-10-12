"""Restore real pre-import files, KV and sources with private, checked jobs."""

import asyncio
import copy
import json
import shutil
import time

import pytest
from astrbot_plugin_manager.transfer_bundle import load_json
from astrbot_plugin_manager.transfers import TransferService


@pytest.fixture
async def imported(transfer_env):
    env = transfer_env
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    directory = target.root_dir_name
    code = transfers.code_root / directory
    config = transfers.config_root / (directory + "_config.json")
    data = transfers.data_root / target.name
    data.mkdir()
    (code / "main.py").write_text("# incoming\n", encoding="utf-8")
    config.write_text('{"enabled": true}', encoding="utf-8")
    (data / "state.txt").write_text("incoming", encoding="utf-8")
    env.kv_values[("plugin", target.plugin_id)] = {"value": "incoming"}
    rule = await transfers.save_rule(
        [{"directory": directory, "data": True, "config": True}]
    )
    exported = await transfers.start_export("admin", rule["updated_at"], True)
    await transfers.task
    package = transfers.download(exported["id"], "admin", "package")
    (code / "main.py").write_text("# before\n", encoding="utf-8")
    config.write_text('{"enabled": false}', encoding="utf-8")
    (data / "state.txt").write_text("before", encoding="utf-8")
    env.kv_values[("plugin", target.plugin_id)] = {"value": "before"}
    source = transfers.new_upload("admin")
    shutil.copyfile(package, transfers.job_directory(source["id"]) / "upload.zip")
    await transfers.prepare_import(source)
    selection = {"directory": directory, "code": True, "config": True, "data": True}
    await transfers.start_import(source["id"], "admin", [selection], True)
    await transfers.task
    assert source["status"] == "completed", source
    env.import_job = source
    env.selection = selection
    env.target = target
    env.code, env.config, env.data = code, config, data
    return env


async def test_restore_all_components_preserves_both_backups_and_stays_disabled(
    imported,
):
    env = imported
    transfers = env.plugin.transfers
    original = transfers.job_directory(env.import_job["id"])
    original_archive = (original / "rollback.zip").read_bytes()
    preview = await transfers.recovery.preview("admin", env.import_job["id"])
    assert preview["plugins"][0]["components"] == {
        "code": "replace",
        "config": "replace",
        "data": "replace",
    }
    result = await transfers.recovery.start(
        "admin", preview["job"]["id"], [env.selection], True
    )
    await transfers.task
    restored = transfers.get_job(result["id"], "admin")
    assert restored["status"] == "completed", restored
    assert (env.code / "main.py").read_text(encoding="utf-8") == "# before\n"
    assert json.loads(env.config.read_text()) == {"enabled": False}
    assert (env.data / "state.txt").read_text() == "before"
    assert env.kv_values[("plugin", env.target.plugin_id)] == {"value": "before"}
    assert (
        await env.plugin.sources.source_snapshot(
            env.target.root_dir_name, env.target.name
        )
        == load_json(original / "rollback.json")["plugins"][0]["source_state"]
    )
    assert (original / "rollback.zip").read_bytes() == original_archive
    before = transfers.job_directory(result["id"]) / "before" / env.target.root_dir_name
    assert (before / "code" / "main.py").read_text() == "# incoming\n"
    assert load_json(before / "kv.json") != load_json(
        original / "before" / env.target.root_dir_name / "kv.json"
    )
    assert transfers.download(result["id"], "admin", "rollback").is_file()
    assert env.target.module_path in env.prefs["inactivated_plugins"]
    env.manager.load.assert_not_awaited()


async def test_partial_restore_leaves_unselected_components_and_sources(imported):
    env = imported
    transfers = env.plugin.transfers
    sources = copy.deepcopy(
        await env.plugin.sources.source_snapshot(
            env.target.root_dir_name, env.target.name
        )
    )
    preview = await transfers.recovery.preview("admin", env.import_job["id"])
    selection = {**env.selection, "code": False, "data": False}
    result = await transfers.recovery.start(
        "admin", preview["job"]["id"], [selection], True
    )
    await transfers.task
    assert transfers.get_job(result["id"], "admin")["status"] == "completed"
    assert (env.code / "main.py").read_text() == "# incoming\n"
    assert json.loads(env.config.read_text()) == {"enabled": False}
    assert (env.data / "state.txt").read_text() == "incoming"
    assert env.kv_values[("plugin", env.target.plugin_id)] == {"value": "incoming"}
    assert (
        await env.plugin.sources.source_snapshot(
            env.target.root_dir_name, env.target.name
        )
        == sources
    )


async def test_restore_missing_marks_remove_new_plugin_files_and_kv(transfer_env):
    env = transfer_env
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    directory = target.root_dir_name
    data = transfers.data_root / target.name
    data.mkdir()
    (data / "file").write_text("incoming")
    env.kv_values[("plugin", target.plugin_id)] = {"new": 1}
    rule = await transfers.save_rule(
        [{"directory": directory, "data": True, "config": False}]
    )
    exported = await transfers.start_export("admin", rule["updated_at"], True)
    await transfers.task
    package = transfers.download(exported["id"], "admin", "package")
    env.owners.pop(target.module_path)
    shutil.rmtree(transfers.code_root / directory)
    shutil.rmtree(data)
    env.kv_values[("plugin", target.plugin_id)] = {}
    source = transfers.new_upload("admin")
    shutil.copyfile(package, transfers.job_directory(source["id"]) / "upload.zip")
    await transfers.prepare_import(source)
    selection = {"directory": directory, "code": True, "config": False, "data": True}
    await transfers.start_import(source["id"], "admin", [selection], True)
    await transfers.task
    assert source["status"] == "completed", source
    preview = await transfers.recovery.preview("admin", source["id"])
    assert preview["plugins"][0]["components"] == {"code": "remove", "data": "remove"}
    await transfers.recovery.start("admin", preview["job"]["id"], [selection], True)
    await transfers.task
    assert transfers.jobs[preview["job"]["id"]]["status"] == "completed"
    assert not (transfers.code_root / directory).exists()
    assert not data.exists()
    assert env.kv_values[("plugin", target.plugin_id)] == {}


@pytest.mark.parametrize("change", ["code", "config", "data", "kv", "state", "sources"])
async def test_changes_after_preview_refuse_before_writing(imported, change):
    env = imported
    transfers = env.plugin.transfers
    preview = await transfers.recovery.preview("admin", env.import_job["id"])
    if change == "code":
        (env.code / "new.py").write_text("# changed")
    elif change == "config":
        env.config.write_text('{"enabled": false}')
    elif change == "data":
        (env.data / "new").write_text("changed")
    elif change == "kv":
        env.kv_values[("plugin", env.target.plugin_id)] = {"new": 2}
    elif change == "state":
        env.prefs["inactivated_plugins"] = []
    else:
        env.prefs["plugin_install_sources"] = {
            env.target.root_dir_name: {"repo": "https://github.com/test/changed"}
        }
    with pytest.raises(ValueError, match="预检后变化"):
        await transfers.recovery.start(
            "admin", preview["job"]["id"], [env.selection], True
        )
    assert (env.code / "main.py").read_text() == "# incoming\n"
    assert not (transfers.job_directory(preview["job"]["id"]) / "before").exists()


async def test_backup_changes_missing_files_and_source_deletion_refuse(imported):
    env = imported
    transfers = env.plugin.transfers
    preview = await transfers.recovery.preview("admin", env.import_job["id"])
    before = (
        transfers.job_directory(env.import_job["id"])
        / "before"
        / env.target.root_dir_name
        / "code"
        / "main.py"
    )
    original = before.read_bytes()
    before.write_text("# corrupted")
    with pytest.raises(ValueError, match="校验失败"):
        await transfers.recovery.start(
            "admin", preview["job"]["id"], [env.selection], True
        )
    before.write_bytes(original)
    before.unlink()
    with pytest.raises(ValueError, match="缺少|不一致"):
        await transfers.recovery.preview("admin", env.import_job["id"])
    before.write_bytes(original)
    await transfers.delete_job(env.import_job["id"], "admin", True)
    with pytest.raises(ValueError, match="不存在"):
        await transfers.recovery.start(
            "admin", preview["job"]["id"], [env.selection], True
        )


async def test_recovery_is_private_confirmed_and_expires(imported):
    env = imported
    transfers = env.plugin.transfers
    with pytest.raises(ValueError, match="无权访问"):
        await transfers.recovery.preview("other", env.import_job["id"])
    preview = await transfers.recovery.preview("admin", env.import_job["id"])
    with pytest.raises(ValueError, match="无权访问"):
        transfers.recovery.plan("other", preview["job"]["id"])
    with pytest.raises(ValueError, match="请确认"):
        await transfers.recovery.start(
            "admin", preview["job"]["id"], [env.selection], False
        )
    job = transfers.jobs[preview["job"]["id"]]
    job["created_at"] = time.time() - 3601
    with pytest.raises(ValueError, match="过期"):
        await transfers.recovery.start("admin", job["id"], [env.selection], True)
    transfers._save(job)
    restarted = TransferService(env.plugin.service, env.plugin._operations)
    await restarted.initialize()
    assert restarted.public_job(restarted.jobs[job["id"]])["preview_expired"]


async def test_restore_failure_recovers_current_state(imported, monkeypatch):
    env = imported
    transfers = env.plugin.transfers
    preview = await transfers.recovery.preview("admin", env.import_job["id"])
    replace = transfers._replace
    original_id = env.import_job["id"]

    def fail_data_once(root, relative, source):
        if source and original_id in str(source) and str(source).endswith("data"):
            raise OSError("injected restore failure")
        return replace(root, relative, source)

    monkeypatch.setattr(transfers, "_replace", fail_data_once)
    result = await transfers.recovery.start(
        "admin", preview["job"]["id"], [env.selection], True
    )
    await transfers.task
    job = transfers.jobs[result["id"]]
    assert job["status"] == "failed", job
    assert job["results"][0]["status"] == "failed"
    assert (env.code / "main.py").read_text() == "# incoming\n"
    assert json.loads(env.config.read_text()) == {"enabled": True}
    assert (env.data / "state.txt").read_text() == "incoming"
    assert env.kv_values[("plugin", env.target.plugin_id)] == {"value": "incoming"}
    assert job["backup_ready"]


async def test_concurrent_starts_execute_only_once(imported):
    env = imported
    transfers = env.plugin.transfers
    preview = await transfers.recovery.preview("admin", env.import_job["id"])
    results = await asyncio.gather(
        transfers.recovery.start("admin", preview["job"]["id"], [env.selection], True),
        transfers.recovery.start("admin", preview["job"]["id"], [env.selection], True),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1
    await transfers.task
    assert transfers.jobs[preview["job"]["id"]]["status"] == "completed"


async def test_preview_survives_restart_with_private_plan(imported):
    env = imported
    transfers = env.plugin.transfers
    preview = await transfers.recovery.preview("admin", env.import_job["id"])
    restarted = TransferService(env.plugin.service, env.plugin._operations)
    await restarted.initialize()
    recovered = restarted.recovery.plan("admin", preview["job"]["id"])
    assert recovered == preview
    result = await restarted.recovery.start(
        "admin", preview["job"]["id"], [env.selection], True
    )
    await restarted.task
    assert restarted.jobs[result["id"]]["status"] == "completed"


@pytest.mark.parametrize("field", ["source", "components", "identity", "extra"])
async def test_invalid_recovery_records_fail_before_target_changes(imported, field):
    env = imported
    transfers = env.plugin.transfers
    path = transfers.job_directory(env.import_job["id"]) / "rollback.json"
    journal = load_json(path)
    record = journal["plugins"][0]
    if field == "source":
        record["source_state"]["core"]["another_plugin"] = {}
    elif field == "components":
        record["components"]["code"] = "true"
    elif field == "identity":
        record["name"] = "another_plugin"
    else:
        record["legacy_directory"] = record["directory"]
    path.write_text(json.dumps(journal), encoding="utf-8")
    with pytest.raises(ValueError):
        await transfers.recovery.preview("admin", env.import_job["id"])
    assert (env.code / "main.py").read_text() == "# incoming\n"


async def test_low_space_refuses_before_backups_or_target_mutations(
    imported, monkeypatch
):
    env = imported
    transfers = env.plugin.transfers
    preview = await transfers.recovery.preview("admin", env.import_job["id"])

    async def no_space(size):
        raise ValueError("磁盘可用空间不足")

    monkeypatch.setattr(transfers, "_require_space", no_space)
    result = await transfers.recovery.start(
        "admin", preview["job"]["id"], [env.selection], True
    )
    await transfers.task
    assert transfers.jobs[result["id"]]["status"] == "failed"
    assert not transfers.jobs[result["id"]]["backup_ready"]
    assert (env.code / "main.py").read_text() == "# incoming\n"


@pytest.mark.parametrize("failure", [False, True])
async def test_running_plugin_is_paused_and_resumes_only_on_failure(
    imported, monkeypatch, failure
):
    env = imported
    transfers = env.plugin.transfers
    current = env.add_target(
        name=env.target.name, directory=env.target.root_dir_name, active=True
    )
    env.prefs["inactivated_plugins"] = []
    preview = await transfers.recovery.preview("admin", env.import_job["id"])
    assert not preview["plugins"][0]["problem"]
    if failure:
        replace = transfers._replace

        def fail_original(root, relative, source):
            if source and env.import_job["id"] in str(source):
                raise OSError("injected original restore failure")
            return replace(root, relative, source)

        monkeypatch.setattr(transfers, "_replace", fail_original)
    result = await transfers.recovery.start(
        "admin", preview["job"]["id"], [env.selection], True
    )
    await transfers.task
    job = transfers.jobs[result["id"]]
    env.manager._terminate_plugin.assert_awaited_once()
    if failure:
        assert job["status"] == "failed", job
        assert env.owners[current.module_path].activated
        assert current.module_path not in env.prefs["inactivated_plugins"]
        assert (env.code / "main.py").read_text() == "# incoming\n"
        env.manager.load.assert_awaited_once()
    else:
        assert job["status"] == "completed", job
        assert current.module_path not in env.owners
        assert current.module_path in env.prefs["inactivated_plugins"]
        env.manager.load.assert_not_awaited()


async def test_failed_automatic_recovery_keeps_downloadable_current_backup(
    imported, monkeypatch
):
    env = imported
    transfers = env.plugin.transfers
    preview = await transfers.recovery.preview("admin", env.import_job["id"])

    def fail_replace(*args):
        raise OSError("injected replacement failure")

    monkeypatch.setattr(transfers, "_replace", fail_replace)
    result = await transfers.recovery.start(
        "admin", preview["job"]["id"], [env.selection], True
    )
    await transfers.task
    job = transfers.jobs[result["id"]]
    assert job["status"] == "rollback_failed", job
    assert job["results"][0]["status"] == "rollback_failed"
    assert transfers.download(result["id"], "admin", "rollback").is_file()
