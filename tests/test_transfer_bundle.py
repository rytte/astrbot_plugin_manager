"""Adversarial bundles, portable paths, typed KV and database snapshots."""

import json
import sqlite3
import stat
import zipfile

import pytest
from astrbot_plugin_manager import transfer_bundle as bundle


@pytest.mark.parametrize(
    "value",
    [
        "../escape",
        "/absolute",
        "C:/Windows/test",
        "a\\b",
        "a//b",
        "a/./b",
        "CON",
        "nul.txt",
        "aux.txt",
        "name.",
        "name ",
        "a:b",
        "a\x00b",
        "",
    ],
)
def test_portable_paths_reject_escape_devices_and_ambiguous_names(value):
    with pytest.raises(ValueError):
        bundle.portable_path(value)


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        False,
        1,
        -2,
        1.25,
        "中文",
        b"\x00\xff",
        [False, None, b"bytes"],
        {"type": "bytes", "value": "literal", "nested": {"array": [1, 2]}},
    ],
)
def test_kv_types_round_trip_without_marker_collisions(value):
    assert bundle.decode_value(bundle.encode_value(value)) == value


@pytest.mark.parametrize(
    "value",
    [
        {"type": "int", "value": True},
        {"type": "pickle", "value": "payload"},
        {"type": "bytes", "value": "invalid-base64!"},
        {"type": "str", "value": "text", "legacy": True},
    ],
)
def test_unknown_or_mismatched_kv_types_fail_fast(value):
    with pytest.raises(ValueError):
        bundle.decode_value(value)


@pytest.mark.parametrize(
    "text", ['{"same":1,"same":2}', '{"value":NaN}', '{"value":Infinity}']
)
def test_json_rejects_duplicates_and_nonfinite_values(tmp_path, text):
    path = tmp_path / "value.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        bundle.load_json(path)


def test_database_snapshot_includes_committed_wal_data_and_empty_directories(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "empty").mkdir()
    database = sqlite3.connect(source / "state.db")
    try:
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("CREATE TABLE records(value TEXT)")
        database.execute("INSERT INTO records VALUES ('committed')")
        database.commit()
        bundle.copy_tree(source, tmp_path / "snapshot")
        with sqlite3.connect(tmp_path / "snapshot" / "state.db") as snapshot:
            assert snapshot.execute("SELECT value FROM records").fetchone() == (
                "committed",
            )
        assert (tmp_path / "snapshot" / "empty").is_dir()
        assert not (tmp_path / "snapshot" / "state.db-wal").exists()
    finally:
        database.close()


@pytest.mark.parametrize(
    "names",
    [
        ["../outside.txt"],
        ["/outside.txt"],
        ["same", "SAME"],
        ["CON"],
        ["a/", "a/file"],
        ["folder\\escape"],
    ],
)
def test_untrusted_archive_paths_are_rejected_before_live_changes(tmp_path, names):
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(name, "not a bundle")
    with pytest.raises(ValueError):
        bundle.unpack(path, tmp_path / "staging")
    assert not (tmp_path / "outside.txt").exists()


def test_zip_symlink_and_oversized_members_are_rejected(tmp_path, monkeypatch):
    path = tmp_path / "link.zip"
    with zipfile.ZipFile(path, "w") as archive:
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "/etc/passwd")
    with pytest.raises(ValueError, match="链接"):
        bundle.unpack(path, tmp_path / "link-staging")
    monkeypatch.setattr(bundle, "MAX_FILE", 2)
    path = tmp_path / "big.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("large", "123")
    with pytest.raises(ValueError, match="大小"):
        bundle.unpack(path, tmp_path / "big-staging")


async def test_bundle_checksums_extra_files_and_versions_are_rejected(
    transfer_env, tmp_path
):
    env = transfer_env
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    rule = await transfers.save_rule(
        [{"directory": target.root_dir_name, "config": False, "data": False}]
    )
    result = await transfers.start_export("admin", rule["updated_at"], True)
    await transfers.task
    original = transfers.download(result["id"], "admin", "package")
    with zipfile.ZipFile(original) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    for mode in ("checksum", "extra", "version", "directory"):
        changed = dict(contents)
        manifest = json.loads(changed["manifest.json"])
        if mode == "checksum":
            changed[f"payload/{target.root_dir_name}/code/main.py"] = b"tampered"
        elif mode == "extra":
            changed["unexpected.txt"] = b"outside allowlist"
        elif mode == "version":
            manifest["format_version"] = 0
            changed["manifest.json"] = json.dumps(manifest).encode()
        else:
            manifest["directories"].append("forbidden/directory")
            changed["manifest.json"] = json.dumps(manifest).encode()
        path = tmp_path / (mode + ".zip")
        with zipfile.ZipFile(path, "w") as archive:
            for name, content in changed.items():
                archive.writestr(name, content)
        with pytest.raises(ValueError):
            bundle.unpack(path, tmp_path / (mode + "-staging"))


@pytest.mark.parametrize(
    "value",
    [{"value": "1"}, {"value": True}, {"value": None}, {"value": 1, "legacy": 2}, {}],
)
def test_config_is_never_cast_defaulted_or_stripped(value):
    with pytest.raises(ValueError):
        bundle.check_config(value, {"value": {"type": "int", "default": 0}})
    assert value != {"value": 0}


def test_undeclared_executable_dependencies_are_rejected(tmp_path):
    code = tmp_path / "code"
    code.mkdir()
    (code / "metadata.yaml").write_text(
        "name: example\nauthor: test\nversion: 1.0.0\ndesc: example\nastrbot_version: '>=4.27,<5'\n",
        encoding="utf-8",
    )
    (code / "main.py").write_text(
        "raise AssertionError('must not import')", encoding="utf-8"
    )
    for requirement in (
        "-r outside.txt",
        "example @ https://example.invalid/package.whl",
        "example[extra]",
        "definitely-missing-transfer-package>=1",
    ):
        (code / "requirements.txt").write_text(requirement, encoding="utf-8")
        with pytest.raises(ValueError):
            bundle.check_environment(code, "4.27.4")


def test_symlinks_and_hardlinks_cannot_expand_backup_scope(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    external = tmp_path / "external"
    external.write_text("private", encoding="utf-8")
    try:
        (source / "link").symlink_to(external)
    except OSError:
        pytest.skip("Symlink creation is not permitted on this Windows account")
    with pytest.raises(ValueError, match="链接"):
        bundle.tree_files(source)


def test_unreadable_subdirectories_are_not_silently_skipped(tmp_path, monkeypatch):
    def failed_walk(root, *, followlinks, onerror):
        onerror(PermissionError("unreadable"))
        return iter(())

    monkeypatch.setattr(bundle.os, "walk", failed_walk)
    with pytest.raises(ValueError, match="完整读取"):
        bundle.tree_files(tmp_path)


def test_tree_stamp_detects_empty_directory_changes(tmp_path):
    root = tmp_path / "tree"
    absent = bundle.stamp(root)
    root.mkdir()
    empty = bundle.stamp(root)
    assert absent != empty
    (root / "assets").mkdir()
    assert bundle.stamp(root) != empty


def test_invalid_zip_and_yaml_are_explicit_input_errors(tmp_path):
    invalid = tmp_path / "invalid.zip"
    invalid.write_bytes(b"not a ZIP")
    with pytest.raises(ValueError, match="ZIP"):
        bundle.unpack(invalid, tmp_path / "checked")
    code = tmp_path / "code"
    code.mkdir()
    (code / "metadata.yaml").write_text("name: [unfinished", encoding="utf-8")
    with pytest.raises(ValueError, match="YAML"):
        bundle.read_metadata(code)


def test_directory_count_and_total_size_limits_are_enforced(tmp_path, monkeypatch):
    (tmp_path / "empty1").mkdir()
    (tmp_path / "empty2").mkdir()
    monkeypatch.setattr(bundle, "MAX_FILES", 1)
    with pytest.raises(ValueError, match="数量"):
        bundle.tree_directories(tmp_path)
    monkeypatch.setattr(bundle, "MAX_FILES", 100)
    monkeypatch.setattr(bundle, "MAX_CONTENT", 2)
    (tmp_path / "file").write_bytes(b"123")
    with pytest.raises(ValueError, match="总大小"):
        bundle.tree_files(tmp_path)
