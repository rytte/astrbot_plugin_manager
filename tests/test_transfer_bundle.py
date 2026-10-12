"""Adversarial bundles, portable paths, typed KV and database snapshots."""

import json
import sqlite3
import stat
import zipfile
from contextlib import closing

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
@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_json_rejects_duplicates_and_nonfinite_values(tmp_path, text, encoding):
    path = tmp_path / "value.json"
    path.write_text(text, encoding=encoding)
    with pytest.raises(ValueError):
        bundle.load_json(path)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_json_accepts_utf8_with_or_without_bom(tmp_path, encoding):
    path = tmp_path / "value.json"
    path.write_text('{"text":"中文","enabled":true}', encoding=encoding)
    assert bundle.load_json(path) == {"text": "中文", "enabled": True}


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


def test_closed_wal_database_backup_can_create_source_sidecars(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    path = source / "plans.sqlite3"
    with closing(sqlite3.connect(path)) as database:
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("CREATE TABLE plans(value TEXT)")
        database.execute("INSERT INTO plans VALUES ('saved plan')")
        database.commit()
    assert {file.name for file in source.iterdir()} == {"plans.sqlite3"}
    (source / "media").mkdir()
    (source / "media" / "image.png").write_bytes(b"saved media")

    destination = tmp_path / "snapshot"
    bundle.copy_tree(source, destination)

    with closing(sqlite3.connect(destination / path.name)) as snapshot:
        assert snapshot.execute("SELECT value FROM plans").fetchone() == ("saved plan",)
        assert snapshot.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert (destination / "media" / "image.png").read_bytes() == b"saved media"
    assert not (destination / "plans.sqlite3-wal").exists()
    assert not (destination / "plans.sqlite3-shm").exists()


@pytest.mark.parametrize("media_name", ["a_media.txt", "z_media.txt"])
def test_wal_commit_during_snapshot_is_rejected_even_when_main_file_is_unchanged(
    tmp_path, monkeypatch, media_name
):
    source = tmp_path / "source"
    source.mkdir()
    path = source / "plans.sqlite3"
    (source / media_name).write_text("unchanged", encoding="utf-8")
    original_copy = bundle.shutil.copyfile
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE plans(value TEXT)")
        writer.execute("INSERT INTO plans VALUES ('before')")
        writer.commit()
        initial_stamp = next(
            entry for entry in bundle.stamp(source) if entry[0] == path.name
        )

        def copy_then_commit(file, target):
            original_copy(file, target)
            writer.execute("INSERT INTO plans VALUES ('concurrent write')")
            writer.commit()

        monkeypatch.setattr(bundle.shutil, "copyfile", copy_then_commit)
        with pytest.raises(ValueError, match="修改：plans.sqlite3"):
            bundle.copy_tree(source, tmp_path / "snapshot")
        final_stamp = next(
            entry for entry in bundle.stamp(source) if entry[0] == path.name
        )
        assert initial_stamp == final_stamp


@pytest.mark.parametrize("relative", ["media/image.png", "orphan.sqlite3-wal"])
def test_ordinary_files_and_unassociated_wal_files_still_reject_changes(
    tmp_path, monkeypatch, relative
):
    source = tmp_path / "source"
    source.mkdir()
    with closing(sqlite3.connect(source / "plans.sqlite3")) as database:
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("CREATE TABLE plans(value TEXT)")
        database.commit()
    changed = source / relative
    changed.parent.mkdir(exist_ok=True)
    changed.write_bytes(b"before")
    original_copy = bundle.shutil.copyfile

    def copy_then_change(path, target):
        original_copy(path, target)
        if path == changed:
            changed.write_bytes(b"after modification")

    monkeypatch.setattr(bundle.shutil, "copyfile", copy_then_change)
    with pytest.raises(ValueError) as caught:
        bundle.copy_tree(source, tmp_path / "snapshot")
    assert f"修改：{relative}" in str(caught.value)


def test_snapshot_reports_added_deleted_and_modified_relative_paths(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "cache").mkdir()
    (source / "cache" / "old.json").write_text("old", encoding="utf-8")
    (source / "empty").mkdir()
    (source / "state.json").write_text("before", encoding="utf-8")
    (source / "z_keep.txt").write_text("unchanged", encoding="utf-8")
    original_copy = bundle.shutil.copyfile

    def copy_then_change(path, target):
        original_copy(path, target)
        if path.name == "z_keep.txt":
            (source / "state.json").write_text("after modification", encoding="utf-8")
            (source / "cache" / "old.json").unlink()
            (source / "cache" / "new.json").write_text("new", encoding="utf-8")
            (source / "empty").rmdir()
            (source / "new_empty").mkdir()

    monkeypatch.setattr(bundle.shutil, "copyfile", copy_then_change)
    with pytest.raises(ValueError) as caught:
        bundle.copy_tree(source, tmp_path / "snapshot", code=True)
    message = str(caught.value)
    for detail in (
        "新增：cache/new.json",
        "删除：cache/old.json",
        "修改：state.json",
        "删除：empty/",
        "新增：new_empty/",
        "未生成备份",
    ):
        assert detail in message
    assert "z_keep.txt" not in message
    assert str(source) not in message


def test_snapshot_change_details_are_bounded_and_report_total(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "trigger.txt").write_text("unchanged", encoding="utf-8")
    original_copy = bundle.shutil.copyfile

    def copy_then_add(path, target):
        original_copy(path, target)
        for number in range(12):
            (source / f"added_{number:02}.txt").write_text("new", encoding="utf-8")

    monkeypatch.setattr(bundle.shutil, "copyfile", copy_then_add)
    with pytest.raises(ValueError) as caught:
        bundle.copy_tree(source, tmp_path / "snapshot")
    message = str(caught.value)
    assert message.count("新增：") == 10
    assert "新增：added_09.txt" in message
    assert "added_10.txt" not in message and "added_11.txt" not in message
    assert "另有 2 项变化（共 12 项）" in message


def test_source_disappearing_mid_copy_reports_relative_path(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "cache.txt").write_text("cached", encoding="utf-8")
    original_copy = bundle.shutil.copyfile

    def delete_then_copy(path, target):
        path.unlink()
        original_copy(path, target)

    monkeypatch.setattr(bundle.shutil, "copyfile", delete_then_copy)
    with pytest.raises(ValueError, match="路径消失.*cache.txt"):
        bundle.copy_tree(source, tmp_path / "snapshot")


def test_missing_destination_is_not_reported_as_a_source_change(tmp_path, monkeypatch):
    source, destination = tmp_path / "source", tmp_path / "snapshot"
    source.mkdir()
    (source / "file.txt").write_text("unchanged", encoding="utf-8")

    def destination_missing(path, target):
        raise FileNotFoundError(2, "missing destination", str(target))

    monkeypatch.setattr(bundle.shutil, "copyfile", destination_missing)
    with pytest.raises(FileNotFoundError):
        bundle.copy_tree(source, destination)


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
    for mode in (
        "checksum",
        "extra",
        "version",
        "directory",
        "sources-checksum",
        "sources-identity",
        "sources-validation",
        "sources-missing",
    ):
        changed = dict(contents)
        manifest = json.loads(changed["manifest.json"])
        if mode == "checksum":
            changed[f"payload/{target.root_dir_name}/code/main.py"] = b"tampered"
        elif mode == "extra":
            changed["unexpected.txt"] = b"outside allowlist"
        elif mode == "version":
            manifest["format_version"] = 0
            changed["manifest.json"] = json.dumps(manifest).encode()
        elif mode == "directory":
            manifest["directories"].append("forbidden/directory")
            changed["manifest.json"] = json.dumps(manifest).encode()
        else:
            relative = f"payload/{target.root_dir_name}/sources.json"
            sources = json.loads(changed[relative])
            if mode == "sources-checksum":
                sources["provenance"]["observed_at"] = "2025-01-01T00:00:00Z"
            elif mode == "sources-identity":
                sources["identity"]["author"] = "Somebody else"
            elif mode == "sources-validation":
                sources["verification"] = "identity_matched"
            if mode == "sources-missing":
                changed.pop(relative)
                manifest["files"].pop(relative)
                changed["manifest.json"] = json.dumps(manifest).encode()
            else:
                changed[relative] = json.dumps(sources).encode()
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
