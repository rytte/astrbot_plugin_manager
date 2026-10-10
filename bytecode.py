"""Clear source-backed bytecode within one verified plugin directory."""

import importlib
from pathlib import Path


def clear_plugin_bytecode(store_path: str, root_dir_name: str) -> None:
    """Discard bytecode without following external files or cache directories.

    Args:
        store_path: The user or reserved plugin storage directory.
        root_dir_name: The registered directory name of exactly one plugin.

    Raises:
        RuntimeError: A plugin, source or cache path escapes its expected location.
        OSError: A source directory or bytecode file cannot be accessed.
    """
    store = Path(store_path).resolve(strict=True)
    candidate = store / root_dir_name
    root = candidate.resolve(strict=True)
    if root.parent != store or root != candidate or candidate.name != root_dir_name:
        raise RuntimeError("插件目录不是存储目录中的直接子目录，拒绝清理缓存。")
    caches = set()
    for source in root.rglob("*.py"):
        if not source.resolve(strict=True).is_relative_to(root):
            raise RuntimeError("插件源文件指向插件目录之外，拒绝清理缓存。")
        cache_dir = source.parent / "__pycache__"
        if not cache_dir.exists():
            continue
        if not cache_dir.resolve(strict=True).is_relative_to(root):
            raise RuntimeError("插件字节码缓存目录指向插件目录之外，拒绝清理缓存。")
        for cache in cache_dir.iterdir():
            if cache.name.startswith(source.stem + ".") and cache.suffix == ".pyc":
                if not cache.resolve(strict=True).is_relative_to(root):
                    raise RuntimeError("插件字节码缓存指向插件目录之外，拒绝清理缓存。")
                caches.add(cache)
    for cache in caches:
        cache.unlink()
    importlib.invalidate_caches()
