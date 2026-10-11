"""Verify observed provenance, identities, switching, and persistence offline."""

import copy
from unittest.mock import AsyncMock

import pytest
import yaml
from astrbot_plugin_manager.sources import SourceService, same_repository


def core_source(target, method="market"):
    return {
        "root_dir_name": target.root_dir_name,
        "name": target.name,
        "author": target.author,
        "install_method": method,
        "registry_url": None,
        "market_plugin_id": f"{target.author}/{target.name}",
        "repo": "https://github.com/test/demo",
        "installed_at": "2025-01-01T00:00:00Z",
    }


def registry(target, **changes):
    return {
        f"{target.author}/{target.name}": {
            "name": target.name,
            "author": target.author,
            "repo": "https://github.com/test/demo",
            **changes,
        }
    }


async def test_unknown_and_implicit_sources_are_never_reported_as_market(env):
    target = env.add_target()
    env.prefs["plugin_install_sources"] = {
        target.root_dir_name: {"install_method": "market", "implicit": True}
    }
    record = await env.plugin.sources.detail(target.root_dir_name)
    assert record["provenance"]["method"] == "unknown"
    assert not record["bindings"] and record["active_binding_id"] is None
    env.preferences.global_put.assert_not_awaited()
    assert "缺少真实安装记录" in await env.plugin.sources.describe(target.name)


async def test_observation_does_not_invent_original_install_fact_and_survives_restart(
    env,
):
    target = env.add_target()
    env.prefs["plugin_install_sources"] = {target.root_dir_name: core_source(target)}
    record = await env.plugin.sources.detail(target.root_dir_name)
    assert record["provenance"]["evidence"] == "core_record_observed"
    assert record["provenance"]["installed_at"] is None
    assert record["provenance"]["reported_installed_at"].startswith("2025")
    restarted = SourceService(env.plugin.service, env.plugin.transfers)
    assert (await restarted.detail(target.root_dir_name))["record_id"] == record[
        "record_id"
    ]


async def test_repo_binding_and_switch_preserve_market_and_initial_observation(
    env, monkeypatch
):
    target = env.add_target()
    env.prefs["plugin_install_sources"] = {target.root_dir_name: core_source(target)}
    inspect = AsyncMock(return_value={"name": target.name, "author": target.author})
    monkeypatch.setattr(
        env.manager, "inspect_plugin_repository", inspect, raising=False
    )
    sources = env.plugin.sources
    first = await sources.detail(target.root_dir_name)
    preview = await sources.preview_binding(
        "admin", target.root_dir_name, "repository", "https://github.com/test/demo"
    )
    saved = await sources.bind("admin", preview["id"], False, True)
    assert len(saved["bindings"]) == 2
    assert saved["active_binding_id"] == first["active_binding_id"]
    assert (
        env.prefs["plugin_install_sources"][target.root_dir_name]["install_method"]
        == "market"
    )
    repo = next(item for item in saved["bindings"] if item["kind"] == "repository")
    changed = await sources.select(
        "admin", target.root_dir_name, repo["id"], saved["revision"]
    )
    assert changed["active_binding_id"] == repo["id"]
    assert changed["provenance"] == first["provenance"]
    assert (
        env.prefs["plugin_install_sources"][target.root_dir_name]["install_method"]
        == "repository"
    )
    assert inspect.await_count == 2  # Every switch rechecks the remote identity.
    market = next(item for item in changed["bindings"] if item["kind"] == "market")
    monkeypatch.setattr(
        sources.operations.web,
        "get_online_plugins",
        AsyncMock(return_value=(registry(target), None)),
    )
    restored = await sources.select(
        "admin", target.root_dir_name, market["id"], changed["revision"]
    )
    assert restored["active_binding_id"] == market["id"]
    assert len(restored["bindings"]) == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"author": "impostor"},
        {"name": "other"},
        {"repo": "https://evil.example/test/demo"},
    ],
)
async def test_market_mismatch_cannot_be_bound(env, monkeypatch, changes):
    target = env.add_target()
    env.prefs["plugin_install_sources"] = {target.root_dir_name: core_source(target)}
    monkeypatch.setattr(
        env.plugin.service.web,
        "get_online_plugins",
        AsyncMock(return_value=(registry(target, **changes), None)),
    )
    with pytest.raises(ValueError):
        await env.plugin.sources.preview_binding(
            "admin", target.root_dir_name, "market", f"{target.author}/{target.name}"
        )
    env.preferences.global_put.assert_not_awaited()


async def test_preview_owner_replay_expiry_and_changed_core_are_rejected(
    env, monkeypatch
):
    target = env.add_target()
    sources = env.plugin.sources
    monkeypatch.setattr(
        env.manager,
        "inspect_plugin_repository",
        AsyncMock(return_value={"name": target.name, "author": target.author}),
        raising=False,
    )
    preview = await sources.preview_binding(
        "admin", target.root_dir_name, "repository", "https://github.com/test/demo"
    )
    with pytest.raises(ValueError, match="无权"):
        await sources.bind("other", preview["id"], True, True)
    env.prefs["plugin_install_sources"] = {target.root_dir_name: core_source(target)}
    with pytest.raises(ValueError, match="变化"):
        await sources.bind("admin", preview["id"], True, True)
    preview = await sources.preview_binding(
        "admin", target.root_dir_name, "repository", "https://github.com/test/demo"
    )
    await sources.bind("admin", preview["id"], True, True)
    with pytest.raises(ValueError, match="过期"):
        await sources.bind("admin", preview["id"], True, True)
    preview = await sources.preview_binding(
        "admin", target.root_dir_name, "repository", "https://github.com/test/demo"
    )
    sources.previews[preview["id"]]["expires_at"] = 0
    with pytest.raises(ValueError, match="过期"):
        await sources.bind("admin", preview["id"], True, True)


async def test_core_external_changes_keep_old_associations_and_history(env):
    target = env.add_target()
    env.prefs["plugin_install_sources"] = {target.root_dir_name: core_source(target)}
    first = await env.plugin.sources.detail(target.root_dir_name)
    env.prefs["plugin_install_sources"][target.root_dir_name] = core_source(
        target, "repository"
    )
    updated = await env.plugin.sources.detail(target.root_dir_name)
    assert len(updated["bindings"]) == 2
    assert updated["provenance"] == first["provenance"]
    assert updated["history"][-1]["action"] == "core_observed"
    assert updated["revision"] != first["revision"]


async def test_directory_reuse_never_inherits_trust(env):
    target = env.add_target()
    env.prefs["plugin_install_sources"] = {target.root_dir_name: core_source(target)}
    first = await env.plugin.sources.detail(target.root_dir_name)
    target.name = "another_plugin"
    changed = await env.plugin.sources.detail(target.root_dir_name)
    assert changed["identity_changed"]
    assert changed["active_binding_id"] is None
    assert changed["provenance"]["method"] == "unknown"
    with pytest.raises(ValueError, match="复用"):
        env.plugin.sources._editable(changed)
    target.name = first["identity"]["name"]
    assert (await env.plugin.sources.detail(target.root_dir_name))[
        "record_id"
    ] == first["record_id"]


@pytest.mark.parametrize("first_seen_after_edit", [False, True])
async def test_author_edit_can_sync_records_without_reloading_or_losing_sources(
    transfer_env, first_seen_after_edit, monkeypatch
):
    env = transfer_env
    target = env.make_transfer_plugin(active=True)
    old_author = target.author
    other = env.make_transfer_plugin(name="other", directory="other_dir")
    sources, transfers = env.plugin.sources, env.plugin.transfers
    env.prefs["plugin_install_sources"] = {target.root_dir_name: core_source(target)}
    core_before = copy.deepcopy(env.prefs)
    rule = await transfers.save_rule(
        [
            {"directory": target.root_dir_name, "config": False, "data": False},
            {"directory": other.root_dir_name, "config": False, "data": False},
        ]
    )
    if not first_seen_after_edit:
        await sources.detail(target.root_dir_name)
    path = transfers.code_root / target.root_dir_name / "metadata.yaml"
    metadata = yaml.safe_load(path.read_text(encoding="utf-8"))
    metadata["author"] = "Edited Author"
    path.write_text(yaml.safe_dump(metadata), encoding="utf-8")
    disk_before = path.read_bytes()
    before = await sources.detail(target.root_dir_name)
    assert before["metadata_changed"] and not before["identity_changed"]
    assert not before["local"]["problem"]
    assert before["local"]["status"] == "enabled"
    assert before["active_binding_id"]
    assert before["bindings"][0]["verification"] == "metadata_changed"
    sources._editable(before)
    with pytest.raises(ValueError, match="更新信息"):
        await sources.preview_binding(
            "admin", target.root_dir_name, "repository", "https://github.com/test/demo"
        )
    synced = await sources.sync_metadata(
        "admin", target.root_dir_name, before["revision"]
    )
    assert not synced["metadata_changed"] and not synced["identity_changed"]
    assert synced["local"]["author"] == "Edited Author"
    assert synced["record_id"] == before["record_id"]
    assert synced["provenance"] == before["provenance"]
    assert synced["bindings"] == before["bindings"]
    assert synced["active_binding_id"] == before["active_binding_id"]
    assert synced["history"][-1]["action"] == "metadata_synced"
    assert (
        transfers.rule()["selections"][0]["plugin_id"] == f"edited author/{target.name}"
    )
    assert transfers.rule()["selections"][1] == rule["selections"][1]
    restarted = SourceService(env.plugin.service, transfers)
    assert not (await restarted.detail(target.root_dir_name))["metadata_changed"]
    assert target.author == old_author
    assert target.activated
    assert env.prefs == core_before and path.read_bytes() == disk_before
    env.manager.load.assert_not_awaited()
    env.manager._terminate_plugin.assert_not_awaited()
    env.preferences.global_put.assert_not_awaited()
    monkeypatch.setattr(
        env.manager,
        "inspect_plugin_repository",
        AsyncMock(return_value={"name": target.name, "author": "Edited Author"}),
        raising=False,
    )
    preview = await sources.preview_binding(
        "admin", target.root_dir_name, "repository", "https://github.com/test/demo"
    )
    rebound = await sources.bind("admin", preview["id"], False, True)
    assert any(
        binding["verification"] == "identity_matched" for binding in rebound["bindings"]
    )
    assert not rebound["metadata_changed"]


async def test_metadata_sync_rejects_changes_since_display(transfer_env):
    env = transfer_env
    target = env.make_transfer_plugin()
    sources = env.plugin.sources
    await sources.detail(target.root_dir_name)
    path = env.plugin.transfers.code_root / target.root_dir_name / "metadata.yaml"
    metadata = yaml.safe_load(path.read_text(encoding="utf-8"))
    metadata["author"] = "First Edit"
    path.write_text(yaml.safe_dump(metadata), encoding="utf-8")
    shown = await sources.detail(target.root_dir_name)
    metadata["author"] = "Second Edit"
    path.write_text(yaml.safe_dump(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="信息已变化"):
        await sources.sync_metadata("admin", target.root_dir_name, shown["revision"])
    assert (await sources.detail(target.root_dir_name))["metadata_changed"]
    assert not env.plugin.transfers.rule_path.exists()
    env.manager.load.assert_not_awaited()


async def test_bundle_import_invalidates_bindings_and_can_restore_source_snapshot(env):
    target = env.add_target()
    env.prefs["plugin_install_sources"] = {target.root_dir_name: core_source(target)}
    original = await env.plugin.sources.detail(target.root_dir_name)
    snapshot = await env.plugin.sources.source_snapshot(
        target.root_dir_name, target.name
    )
    original_core = copy.deepcopy(env.prefs["plugin_install_sources"])
    await env.plugin.sources.note_bundle(target.root_dir_name, "admin")
    modified = await env.plugin.sources.detail(target.root_dir_name)
    assert not modified["bindings"] and not modified["active_binding_id"]
    assert target.root_dir_name not in env.prefs["plugin_install_sources"]
    await env.plugin.sources.restore_sources(snapshot)
    assert env.prefs["plugin_install_sources"] == original_core
    assert (await env.plugin.sources.detail(target.root_dir_name))[
        "bindings"
    ] == original["bindings"]


@pytest.mark.parametrize(
    "first,second,expected",
    [
        ("https://github.com/Test/Demo.git", "https://github.com/test/demo", True),
        ("https://github.com/test/demo", "https://evil.example/test/demo", False),
        ("https://example.com/test/demo", "https://example.com:444/test/demo", False),
        ("https://example.com/Test/demo", "https://example.com/test/demo", False),
    ],
)
def test_repository_identity_includes_host_and_port(first, second, expected):
    assert same_repository(first, second) is expected


async def test_custom_market_binding_never_silently_switches_to_official(env):
    target = env.add_target()
    core = core_source(target)
    core["registry_url"] = "https://custom.example/registry.json"
    env.prefs["plugin_install_sources"] = {target.root_dir_name: core}
    record = await env.plugin.sources.detail(target.root_dir_name)
    with pytest.raises(ValueError, match="自定义市场"):
        await env.plugin.sources.select(
            "admin",
            target.root_dir_name,
            record["active_binding_id"],
            record["revision"],
        )
    assert env.prefs["plugin_install_sources"][target.root_dir_name] == core


async def test_confirmed_uninstall_archives_source_and_new_record_does_not_inherit_it(
    env,
):
    target = env.add_target()
    env.prefs["plugin_install_sources"] = {target.root_dir_name: core_source(target)}
    first = await env.plugin.sources.detail(target.root_dir_name)
    await env.plugin.sources.retire(target.root_dir_name, "admin")
    env.prefs["plugin_install_sources"] = {}
    current = await env.plugin.sources.detail(target.root_dir_name)
    assert current["record_id"] != first["record_id"]
    assert current["provenance"]["method"] == "unknown"
    assert not current["bindings"]
    assert (
        env.plugin.sources._store()["archived"][-1]["history"][-1]["action"]
        == "uninstalled"
    )
