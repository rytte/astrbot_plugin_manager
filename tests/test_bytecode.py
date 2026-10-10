"""Verify source-backed cache cleanup and path confinement."""

import importlib.util
import os
import py_compile

import pytest
from astrbot_plugin_manager.bytecode import clear_plugin_bytecode


def test_only_source_backed_bytecode_is_removed(tmp_path):
    directory = tmp_path / "demo"
    directory.mkdir()
    source = directory / "main.py"
    source.write_text("REVISION = 1\n", encoding="utf-8")
    py_compile.compile(str(source), doraise=True)
    cache = directory / "__pycache__"
    orphan = cache / "orphan.cpython-312.pyc"
    orphan.write_bytes(b"keep")
    sibling = tmp_path / "other"
    sibling.mkdir()
    other = sibling / "main.py"
    other.write_text("VALUE = 2\n", encoding="utf-8")
    py_compile.compile(str(other), doraise=True)
    clear_plugin_bytecode(str(tmp_path), "demo")
    assert not os.path.exists(importlib.util.cache_from_source(str(source)))
    assert orphan.exists()
    assert os.path.exists(importlib.util.cache_from_source(str(other)))


@pytest.mark.parametrize("name", [".", "..", "../other"])
def test_root_or_external_plugin_directory_is_rejected(tmp_path, name):
    store = tmp_path / "store"
    store.mkdir()
    (tmp_path / "other").mkdir()
    with pytest.raises(RuntimeError, match="直接子目录"):
        clear_plugin_bytecode(str(store), name)


@pytest.mark.parametrize("linked", ["root", "cache", "source"])
def test_linked_external_paths_are_rejected_before_deleting_any_cache(tmp_path, linked):
    store = tmp_path / "store"
    store.mkdir()
    plugin = store / "demo"
    plugin.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    source = plugin / "main.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    cache = plugin / "__pycache__"
    cache.mkdir()
    keep = cache / "main.cpython-312.pyc"
    keep.write_bytes(b"keep")
    try:
        if linked == "root":
            os.symlink(outside, store / "linked", target_is_directory=True)
            name = "linked"
        elif linked == "cache":
            external_source = plugin / "child" / "part.py"
            external_source.parent.mkdir()
            external_source.write_text("VALUE = 2\n", encoding="utf-8")
            os.symlink(
                outside,
                external_source.parent / "__pycache__",
                target_is_directory=True,
            )
            name = "demo"
        else:
            other = outside / "part.py"
            other.write_text("VALUE = 2\n", encoding="utf-8")
            os.symlink(other, plugin / "part.py")
            name = "demo"
    except OSError as exc:
        pytest.skip(f"Creating symbolic links is unavailable: {exc}")
    with pytest.raises(RuntimeError):
        clear_plugin_bytecode(str(store), name)
    assert keep.read_bytes() == b"keep"
