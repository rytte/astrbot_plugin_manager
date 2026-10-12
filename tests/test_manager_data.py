"""Portable manager data cannot activate sources or replay instance-local jobs."""

import copy
import json

import pytest
import yaml
from astrbot_plugin_manager.manager_data import FORMAT, validate_manager_data
from astrbot_plugin_manager.source_bundle import portable_binding
from astrbot_plugin_manager.transfer_bundle import load_json
from astrbot_plugin_manager.transfers import TransferService


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


async def test_export_excludes_history_credentials_and_task_files(transfer_env):
    env = transfer_env
    target = env.make_transfer_plugin()
    env.prefs["plugin_install_sources"] = {
        target.root_dir_name: {
            "install_method": "repository",
            "repo": "https://user:secret@github.com/test/demo?token=secret",
        }
    }
    rule = await env.plugin.transfers.save_rule(
        [{"directory": target.root_dir_name, "config": False, "data": False}]
    )
    job = await env.plugin.manager_data.export("admin", "生产环境管家数据")
    assert job["status"] == "completed"
    path = env.plugin.transfers.job_directory(job["id"]) / "manager-data.json"
    value = load_json(path)
    validate_manager_data(value)
    assert value["rule"] == rule
    assert (
        value["sources"][0]["bindings"][0]["repo_url"] == "https://github.com/test/demo"
    )
    assert all(
        f'"{field}"' not in json.dumps(value)
        for field in ("history", "jobs", "provenance", "checked_at", "verification")
    )
    assert "secret" not in path.read_text(encoding="utf-8")
    assert job["download_filename"] == "生产环境管家数据.json"
    restarted = TransferService(env.plugin.service, env.plugin._operations)
    await restarted.initialize()
    assert restarted.history("admin")[0]["download_filename"] == "生产环境管家数据.json"


async def test_import_merges_unverified_sources_preserves_core_and_maps_rule_directory(
    transfer_env,
):
    env = transfer_env
    target = env.make_transfer_plugin(directory="new_directory", active=True)
    env.prefs["plugin_install_sources"] = {
        target.root_dir_name: {
            "install_method": "repository",
            "repo": "https://github.com/test/current",
        }
    }
    before_core = copy.deepcopy(env.prefs)
    record = await env.plugin.sources.detail(target.root_dir_name)
    current = copy.deepcopy(record["bindings"][0])
    service = env.plugin.manager_data
    preview = await service.preview("admin", manager_data())
    assert preview["plugins"][0]["directory"] == "new_directory"
    assert preview["rule"]["selections"][0]["directory"] == "new_directory"
    job = service.start("admin", preview["id"], ["0"], True, True)
    await env.plugin.transfers.task
    assert env.plugin.transfers.get_job(job["id"], "admin")["status"] == "completed"
    restored = await env.plugin.sources.detail(target.root_dir_name)
    assert restored["active_binding_id"] == current["id"]
    assert restored["bindings"][0] == current
    imported = restored["bindings"][1]
    assert imported["verification"] == "imported" and imported["checked_at"] is None
    assert imported["imported_active"] is True
    assert env.prefs == before_core
    assert env.plugin.transfers.rule()["selections"][0]["directory"] == "new_directory"
    env.manager._terminate_plugin.assert_not_awaited()
    env.manager.load.assert_not_awaited()
    repeat = await service.preview("admin", manager_data())
    assert repeat["plugins"][0]["status"] == "skip"
    assert len(restored["bindings"]) == 2


async def test_manager_own_associations_can_migrate_without_copying_its_data(
    transfer_env,
):
    env = transfer_env
    target = env.context.get_registered_star("astrbot_plugin_manager")
    target.author, target.version = "Tester", "1.0"
    root = env.plugin.transfers.code_root / target.root_dir_name
    (root / "metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "name": target.name,
                "author": target.author,
                "version": "1.0",
                "desc": "Manager",
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
    value = manager_data(name=target.name, directory=target.root_dir_name)
    value["rule"]["selections"] = []
    service = env.plugin.manager_data
    preview = await service.preview("admin", value)
    assert preview["plugins"][0]["status"] == "ready"
    job = service.start("admin", preview["id"], ["0"], False, True)
    await env.plugin.transfers.task
    assert env.plugin.transfers.get_job(job["id"], "admin")["status"] == "completed"
    assert (await env.plugin.sources.detail(target.root_dir_name))["bindings"][0][
        "verification"
    ] == "imported"
    assert not env.plugin.transfers.rule_path.exists()
    exported = await service.export("admin")
    content = load_json(
        env.plugin.transfers.job_directory(exported["id"]) / "manager-data.json"
    )
    assert any(entry["name"] == target.name for entry in content["sources"])
    value["rule"]["selections"] = [
        {
            "directory": target.root_dir_name,
            "name": target.name,
            "plugin_id": "tester/astrbot_plugin_manager",
            "data": True,
            "config": False,
        }
    ]
    blocked = await service.preview("admin", value)
    assert blocked["rule"]["ready"] is False
    assert "自身数据不可导出" in blocked["rule"]["problem"]


@pytest.mark.parametrize("problem", ["missing", "author", "config", "self_data"])
async def test_preview_blocks_invalid_targets_and_rules_without_writing(
    transfer_env, problem
):
    env = transfer_env
    if problem != "missing":
        env.make_transfer_plugin()
    value = manager_data()
    if problem == "author":
        value["sources"][0]["author"] = "Someone else"
        value["rule"]["selections"][0]["plugin_id"] = "someone/demo_plugin"
    elif problem == "config":
        value["rule"]["selections"][0]["config"] = True
    elif problem == "self_data":
        value["rule"]["selections"][0].update(
            name="astrbot_plugin_manager",
            plugin_id="tester/astrbot_plugin_manager",
            data=True,
        )
    preview = await env.plugin.manager_data.preview("admin", value)
    assert preview["rule"]["ready"] is False
    if problem in {"missing", "author"}:
        assert preview["plugins"][0]["status"] == "blocked"
    assert not env.plugin.transfers.rule_path.exists()
    with pytest.raises(ValueError):
        env.plugin.manager_data.start("admin", preview["id"], [], True, True)


@pytest.mark.parametrize("change", ["source", "rule", "plugin", "expiry", "owner"])
async def test_changed_or_expired_preview_cannot_modify_data(transfer_env, change):
    env = transfer_env
    target = env.make_transfer_plugin()
    service = env.plugin.manager_data
    preview = await service.preview("admin", manager_data())
    if change == "source":
        env.prefs["plugin_install_sources"] = {
            target.root_dir_name: {
                "install_method": "repository",
                "repo": "https://github.com/test/other",
            }
        }
    elif change == "rule":
        await env.plugin.transfers.save_rule([])
    elif change == "plugin":
        target.version = "2.0"
    elif change == "expiry":
        service.previews[preview["id"]]["expires_at"] = 0
    if change in {"expiry", "owner"}:
        with pytest.raises(ValueError):
            service.start(
                "other" if change == "owner" else "admin",
                preview["id"],
                ["0"],
                False,
                True,
            )
    else:
        job = service.start("admin", preview["id"], ["0"], False, True)
        await env.plugin.transfers.task
        result = env.plugin.transfers.get_job(job["id"], "admin")
        assert result["status"] == "failed"
        assert "重新上传预检" in result["error"]
    assert (
        not (await env.plugin.sources.detail(target.root_dir_name))["bindings"]
        or change == "source"
    )


@pytest.mark.parametrize("rollback_fails", [False, True])
async def test_failed_rule_write_restores_sources_and_keeps_recovery_snapshot(
    transfer_env, monkeypatch, rollback_fails
):
    from astrbot_plugin_manager import manager_data as module

    env = transfer_env
    target = env.make_transfer_plugin()
    await env.plugin.transfers.save_rule([])
    service = env.plugin.manager_data
    preview = await service.preview("admin", manager_data())
    before_sources = env.plugin.sources.path.read_bytes()
    before_rule = env.plugin.transfers.rule_path.read_bytes()
    original = module.write_json
    failures = []

    def fail_rule(path, value):
        if path == env.plugin.transfers.rule_path and (not failures or rollback_fails):
            failures.append(True)
            raise OSError("fixture write failure")
        return original(path, value)

    monkeypatch.setattr(module, "write_json", fail_rule)
    job = service.start("admin", preview["id"], ["0"], True, True)
    await env.plugin.transfers.task
    result = env.plugin.transfers.get_job(job["id"], "admin")
    assert result["status"] == ("rollback_failed" if rollback_fails else "failed")
    assert env.plugin.sources.path.read_bytes() == before_sources
    assert env.plugin.transfers.rule_path.read_bytes() == before_rule
    assert (
        env.plugin.transfers.job_directory(job["id"]) / "before-manager-data.json"
    ).is_file()
    assert not (await env.plugin.sources.detail(target.root_dir_name))["bindings"]
