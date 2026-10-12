"""Strict, bounded plugin bundles without importing any plugin code."""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
import keyword
import math
import os
import shutil
import sqlite3
import stat
import time
import unicodedata
import zipfile
import zlib
from contextlib import ExitStack, closing
from pathlib import Path

import yaml
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from .source_bundle import validate_sources

FORMAT = "astrbot-plugin-manager"
FORMAT_VERSION = 1
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_CONTENT = 1024 * 1024 * 1024
MAX_FILE = 128 * 1024 * 1024
MAX_FILES = 20000
MAX_JSON = 8 * 1024 * 1024
IGNORED = {".git", ".venv", "venv", "__pycache__", "node_modules"}
DEVICES = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
}


def exact_keys(value, expected: set[str]) -> None:
    """Reject missing, unknown or incorrectly shaped fields."""
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("包或规则字段缺失、未知或格式不正确，请检查格式版本。")


def check_bundle_format(manifest: dict) -> None:
    """Accept only the current bundle schema, with mandatory source snapshots."""
    if (
        manifest["format"] != FORMAT
        or type(manifest["format_version"]) is not int
        or manifest["format_version"] != FORMAT_VERSION
    ):
        raise ValueError("插件包格式或版本不受支持；仅支持当前 v1 格式，请重新导出。")


def check_source_snapshot(root: Path, plugin: dict, files) -> str:
    """Require and validate the source snapshot in fresh and retained previews."""
    relative = f"payload/{plugin['directory']}/sources.json"
    path = safe_path(root, relative)
    if relative not in files or not path.is_file():
        raise ValueError(
            "插件包缺少来源快照，不支持不含来源信息的旧 v1 包，请重新导出。"
        )
    validate_sources(load_json(path), plugin)
    return relative


def portable_path(value: str) -> str:
    """Validate a relative path consistently on Windows and POSIX."""
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError("文件路径为空或过长。")
    parts = value.split("/")
    for part in parts:
        if (
            part in {"", ".", ".."}
            or part.endswith((".", " "))
            or any(ord(char) < 32 or char in '\\:*?"<>|' for char in part)
            or part.split(".")[0].upper() in DEVICES
        ):
            raise ValueError("文件路径不安全或无法跨平台使用。")
    return value


def component(value: str, *, module: bool = False) -> str:
    """Validate a single filesystem or Python module identifier."""
    portable_path(value)
    if "/" in value or (
        module and (not value.isidentifier() or keyword.iskeyword(value))
    ):
        raise ValueError("插件名称或目录标识不合法。")
    return value


def safe_path(root: Path, relative: str) -> Path:
    """Keep filesystem operations inside a checked root without links."""
    portable_path(relative)
    root = root.absolute()
    candidate = root.joinpath(*relative.split("/"))
    if root.resolve() != root or candidate.resolve() != candidate:
        raise ValueError("路径包含符号链接、目录联接或越界路径，已拒绝操作。")
    candidate.relative_to(root)
    return candidate


def remove_path(root: Path, relative: str) -> None:
    """Remove only a verified workspace child, never a computed external path."""
    path = safe_path(root, relative)
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 含重复字段。")
        result[key] = value
    return result


def _invalid_number(value):
    raise ValueError("JSON 不允许非有限数值。")


def load_json(path: Path):
    """Accept UTF-8 BOMs while rejecting oversized or ambiguous JSON."""
    if path.stat().st_size > MAX_JSON:
        raise ValueError("JSON 文件超过 8 MiB 限制。")
    return json.loads(
        path.read_text(encoding="utf-8-sig"),
        object_pairs_hook=_json_pairs,
        parse_constant=_invalid_number,
    )


def write_json(path: Path, value) -> None:
    """Persist JSON with atomic replacement and private file permissions."""
    temporary = safe_path(path.parent, path.name + ".tmp")
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    if len(text.encode("utf-8")) > MAX_JSON:
        raise ValueError("JSON 文件超过 8 MiB 限制。")
    temporary.write_text(text, encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(safe_path(path.parent, path.name))


def digest(path: Path) -> str:
    """Hash a file without loading it into memory."""
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _walk_error(error: OSError) -> None:
    raise ValueError("无法完整读取插件目录，请检查文件权限后重试。") from error


def _tree(root: Path, *, code: bool = False) -> tuple[list[Path], list[Path]]:
    """Enumerate a complete bounded tree without silently ignoring unreadable paths."""
    if not root.is_dir() or root.resolve() != root:
        raise ValueError("插件目录不存在或包含链接。")
    files = []
    folders = []
    seen = set()
    total = 0
    for parent, directories, names in os.walk(
        root, followlinks=False, onerror=_walk_error
    ):
        if code:
            directories[:] = [name for name in directories if name not in IGNORED]
        for name in directories + names:
            relative = (Path(parent) / name).relative_to(root).as_posix()
            path = safe_path(root, relative)
            folded = unicodedata.normalize("NFC", relative).casefold()
            if folded in seen:
                raise ValueError("文件路径存在大小写或 Unicode 冲突。")
            seen.add(folded)
            if len(seen) > MAX_FILES:
                raise ValueError("文件和目录数量超过插件包限制。")
            if name in directories:
                folders.append(path)
                continue
            if code and path.suffix in {".pyc", ".pyo"}:
                continue
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                raise ValueError("不支持特殊文件、硬链接或符号链接。")
            if info.st_size > MAX_FILE:
                raise ValueError("单文件超过 128 MiB 限制。")
            total += info.st_size
            files.append(path)
            if len(files) > MAX_FILES or total > MAX_CONTENT:
                raise ValueError("文件数量或总大小超过插件包限制。")
    return sorted(files), sorted(folders)


def tree_files(root: Path, *, code: bool = False) -> list[Path]:
    """Enumerate bounded regular files, rejecting links and portable collisions."""
    return _tree(root, code=code)[0]


def tree_directories(root: Path, *, code: bool = False) -> list[Path]:
    """Enumerate validated directories, including empty directories."""
    return _tree(root, code=code)[1]


def stamp(root: Path, *, code: bool = False) -> list[tuple]:
    """Capture file identities to reject changes during copy or preflight."""
    if not root.exists():
        return []
    files, directories = _tree(root, code=code)
    result = [("", "directory", root.stat().st_ino)]
    for path in directories:
        result.append(
            (path.relative_to(root).as_posix(), "directory", path.stat().st_ino)
        )
    for path in files:
        info = path.stat()
        result.append(
            (
                path.relative_to(root).as_posix(),
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
                info.st_ino,
            )
        )
    return result


def _describe_changes(
    before: list[tuple], after: list[tuple], database_changes: set[str]
) -> str:
    """Report bounded paths with filesystem or SQLite content changes."""
    previous = {entry[0]: entry for entry in before}
    current = {entry[0]: entry for entry in after}
    changes = []
    for relative in sorted(previous.keys() | current.keys()):
        old, new = previous.get(relative), current.get(relative)
        if old == new and relative not in database_changes:
            continue
        entry = new if new is not None else old
        path = relative or "."
        if entry[1] == "directory":
            path += "/"
        action = "新增" if old is None else "删除" if new is None else "修改"
        changes.append(f"{action}：{path}")
    details = "；".join(changes[:10])
    if len(changes) > 10:
        details += f"；另有 {len(changes) - 10} 项变化（共 {len(changes)} 项）"
    return details


def copy_tree(source: Path, destination: Path, *, code: bool = False) -> None:
    """Copy a stable tree and identify source paths that change or disappear."""
    try:
        _copy_tree(source, destination, code=code)
    except (FileNotFoundError, NotADirectoryError) as exc:
        if exc.filename is None:
            raise
        try:
            relative = Path(exc.filename).absolute().relative_to(source.absolute())
        except ValueError:
            raise exc
        raise ValueError(
            "快照期间路径消失或不再是目录，未生成备份；"
            f"变化路径（相对于当前目录）：{relative.as_posix()}；"
            "请停止外部写入后重试。"
        ) from exc


def _copy_tree(source: Path, destination: Path, *, code: bool) -> None:
    """Copy a stable tree, using online SQLite backups for data databases."""
    before = stamp(source, code=code)
    files = tree_files(source, code=code)
    databases = set()
    sidecars = set()
    wal_paths = set()
    if not code:
        for path in files:
            if path in sidecars:
                continue
            with path.open("rb") as source_file:
                if source_file.read(16) == b"SQLite format 3\x00":
                    databases.add(path)
                    sidecars.update(
                        Path(str(path) + suffix)
                        for suffix in ("-wal", "-shm", "-journal")
                    )
                    wal_paths.update(
                        path.relative_to(source).as_posix() + suffix
                        for suffix in ("-wal", "-shm")
                    )
    # Reading a WAL database can itself create or alter its WAL/SHM files.
    # Keep the full tree validation, but compare logical database changes below.
    before = [
        entry
        for entry in before
        if entry[1] == "directory" or entry[0] not in wal_paths
    ]
    destination.mkdir(parents=True, exist_ok=True)
    for path in tree_directories(source, code=code):
        relative = path.relative_to(source).as_posix()
        safe_path(destination, relative).mkdir(parents=True, exist_ok=True)
    with ExitStack() as connections:
        monitors = {}
        for path in sorted(databases):
            input_db = connections.enter_context(
                closing(
                    sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
                )
            )
            # data_version is comparable only on the same open connection.
            version = input_db.execute("PRAGMA data_version").fetchone()[0]
            monitors[path] = (input_db, version)
        for path in files:
            if path in sidecars:
                continue
            target = safe_path(destination, path.relative_to(source).as_posix())
            target.parent.mkdir(parents=True, exist_ok=True)
            if path in databases:
                started = time.monotonic()

                def progress(status, remaining, total):
                    if time.monotonic() - started > 15:
                        raise ValueError("SQLite 快照超时，请停止外部写入后重试。")

                input_db, _ = monitors[path]
                with closing(sqlite3.connect(target)) as output_db:
                    input_db.backup(output_db, pages=256, progress=progress, sleep=0.05)
                    output_db.execute("PRAGMA journal_mode=DELETE")
            else:
                shutil.copyfile(path, target)
            os.chmod(target, 0o600)
        after = [
            entry
            for entry in stamp(source, code=code)
            if entry[1] == "directory" or entry[0] not in wal_paths
        ]
        database_changes = {
            path.relative_to(source).as_posix()
            for path, (input_db, version) in monitors.items()
            if input_db.execute("PRAGMA data_version").fetchone()[0] != version
        }
        if before != after or database_changes:
            raise ValueError(
                "快照期间文件发生变化，未生成备份；"
                "变化路径（相对于当前目录）："
                f"{_describe_changes(before, after, database_changes)}；"
                "请停止外部写入后重试。"
            )


def encode_value(value, depth: int = 0):
    """Encode every KV node explicitly, including bytes without marker collisions."""
    if depth > 32:
        raise ValueError("KV 数据嵌套超过限制。")
    if value is None:
        return {"type": "null", "value": None}
    kind = type(value).__name__
    if kind == "bytes":
        payload = base64.b64encode(value).decode("ascii")
    elif kind == "dict":
        if not all(isinstance(key, str) for key in value):
            raise ValueError("KV 字典键必须是字符串。")
        payload = {key: encode_value(item, depth + 1) for key, item in value.items()}
    elif kind == "list":
        payload = [encode_value(item, depth + 1) for item in value]
    elif kind in {"bool", "int", "float", "str"}:
        if kind == "float" and not math.isfinite(value):
            raise ValueError("KV 数值必须有限。")
        payload = value
    else:
        raise ValueError("KV 包含不支持的类型。")
    return {"type": kind, "value": payload}


def decode_value(node, depth: int = 0):
    """Decode only explicitly supported KV types, never pickle or executable data."""
    exact_keys(node, {"type", "value"})
    if depth > 32:
        raise ValueError("KV 数据嵌套超过限制。")
    kind, value = node["type"], node["value"]
    if kind == "null" and value is None:
        return None
    if kind == "bytes" and isinstance(value, str):
        return base64.b64decode(value, validate=True)
    if kind == "dict" and isinstance(value, dict):
        return {key: decode_value(item, depth + 1) for key, item in value.items()}
    if kind == "list" and isinstance(value, list):
        return [decode_value(item, depth + 1) for item in value]
    if kind in {"str", "bool", "int", "float"} and type(value).__name__ == kind:
        if kind == "float" and not math.isfinite(value):
            raise ValueError("KV 数值必须有限。")
        return value
    raise ValueError("KV 数据的类型与内容不匹配。")


def read_metadata(code: Path) -> dict:
    """Read canonical metadata.yaml without imports or legacy name inference."""
    path = safe_path(code, "metadata.yaml")
    if not path.is_file() or path.stat().st_size > MAX_JSON:
        raise ValueError("缺少有效的 metadata.yaml，无法确认插件身份。")
    try:
        metadata = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeError) as exc:
        raise ValueError("metadata.yaml 不是有效的 UTF-8 YAML。") from exc
    if not isinstance(metadata, dict):
        raise ValueError("插件元数据必须是映射。")
    for field in ("name", "author", "version", "desc"):
        if not isinstance(metadata.get(field), str) or not metadata[field]:
            raise ValueError(f"插件元数据缺少有效字段 {field}。")
    component(metadata["name"])
    Version(metadata["version"])
    if metadata.get("display_name") is not None and not isinstance(
        metadata["display_name"], str
    ):
        raise ValueError("display_name 必须是字符串。")
    if "astrbot_version" in metadata:
        if (
            not isinstance(metadata["astrbot_version"], str)
            or not metadata["astrbot_version"]
        ):
            raise ValueError("astrbot_version 必须是非空版本范围字符串。")
        SpecifierSet(metadata["astrbot_version"])
    if not safe_path(code, "main.py").is_file():
        raise ValueError("插件缺少 main.py。")
    metadata["plugin_id"] = (
        f"{metadata['author'].lower().replace('/', '_')}/{metadata['name'].lower().replace('/', '_')}"
    )
    return metadata


def check_environment(code: Path, astrbot_version: str) -> None:
    """Reject incompatible versions and dependencies without running pip or code."""
    metadata = read_metadata(code)
    if metadata.get("astrbot_version") and Version(astrbot_version) not in SpecifierSet(
        metadata["astrbot_version"]
    ):
        raise ValueError("插件声明的 AstrBot 版本范围不适用于当前版本。")
    requirements = safe_path(code, "requirements.txt")
    if not requirements.exists():
        return
    if requirements.stat().st_size > MAX_JSON:
        raise ValueError("依赖声明文件超过限制。")
    for line in requirements.read_text(encoding="utf-8").splitlines():
        line = line.strip().split(" #", 1)[0]
        if not line or line.startswith("#"):
            continue
        try:
            required = Requirement(line)
        except InvalidRequirement as exc:
            raise ValueError(
                "只支持标准 PEP 508 依赖声明，不支持 pip 参数或外部依赖文件。"
            ) from exc
        if required.url or required.extras:
            raise ValueError("URL 或 extras 依赖需人工审核，第一版不自动导入此类依赖。")
        if required.marker and not required.marker.evaluate():
            continue
        try:
            installed = importlib.metadata.version(required.name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise ValueError(f"缺少依赖 {required.name}，请先手动安装。") from exc
        if required.specifier and Version(installed) not in required.specifier:
            raise ValueError(f"依赖 {required.name} 版本不符合要求，请人工处理冲突。")


def check_config(data, schema, path: str = "配置") -> None:
    """Validate exact schema shapes without dropping fields, casting or defaults."""
    if (
        not isinstance(data, dict)
        or not isinstance(schema, dict)
        or set(data) != set(schema)
    ):
        raise ValueError(f"{path}字段与配置结构不一致，拒绝补默认值或丢弃字段。")
    types = {
        "bool": bool,
        "int": int,
        "float": (int, float),
        "string": str,
        "text": str,
        "list": list,
        "file": list,
        "object": dict,
        "dict": dict,
        "template_list": list,
    }
    for key, value in data.items():
        meta = schema[key]
        kind = meta.get("type") if isinstance(meta, dict) else None
        if kind not in types:
            raise ValueError(f"{path}.{key} 的配置类型不受支持。")
        if not isinstance(value, types[kind]) or (
            kind in {"int", "float"} and isinstance(value, bool)
        ):
            raise ValueError(f"{path}.{key} 的配置类型不匹配。")
        if kind in {"object", "dict"} and "items" in meta:
            check_config(value, meta["items"], f"{path}.{key}")
        if kind == "list" and "items" in meta:
            for index, item in enumerate(value):
                check_config(item, meta["items"], f"{path}.{key}[{index}]")
        if kind == "template_list":
            for index, item in enumerate(value):
                if not isinstance(item, dict) or not isinstance(
                    item.get("__template_key"), str
                ):
                    raise ValueError(f"{path}.{key}[{index}] 缺少 __template_key。")
                template = meta.get("templates", {}).get(item["__template_key"])
                if not isinstance(template, dict):
                    raise ValueError(f"{path}.{key}[{index}] 模板不存在。")
                check_config(
                    {
                        field: entry
                        for field, entry in item.items()
                        if field != "__template_key"
                    },
                    template["items"],
                    f"{path}.{key}[{index}]",
                )
        if kind == "file" and any(
            not isinstance(item, str) or not portable_path(item).startswith("files/")
            for item in value
        ):
            raise ValueError(f"{path}.{key} 包含外部文件路径。")
        if (
            kind in {"string", "int", "float", "bool"}
            and "options" in meta
            and value not in meta["options"]
        ):
            raise ValueError(f"{path}.{key} 不在允许的选项范围内。")


def pack(stage: Path, output: Path, manifest: dict) -> dict:
    """Write a checksum manifest and a bounded ZIP from an isolated snapshot."""
    files = tree_files(stage)
    manifest["directories"] = sorted(
        path.relative_to(stage).as_posix() for path in tree_directories(stage)
    )
    manifest["files"] = {
        path.relative_to(stage).as_posix(): {
            "size": path.stat().st_size,
            "sha256": digest(path),
        }
        for path in files
    }
    write_json(stage / "manifest.json", manifest)
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as archive:
        for path in tree_files(stage):
            archive.write(path, path.relative_to(stage).as_posix())
    if output.stat().st_size > MAX_ARCHIVE:
        output.unlink()
        raise ValueError("压缩包超过 256 MiB 限制。")
    os.chmod(output, 0o600)
    return manifest


def unpack(archive_path: Path, destination: Path) -> dict:
    """Report malformed or damaged ZIP input as an explicit validation failure."""
    try:
        return _unpack(archive_path, destination)
    except (zipfile.BadZipFile, EOFError, zlib.error) as exc:
        raise ValueError("文件不是有效的 ZIP 插件包，或压缩内容已损坏。") from exc


def _unpack(archive_path: Path, destination: Path) -> dict:
    """Fully validate a closed bundle format before touching live plugin paths."""
    if archive_path.stat().st_size > MAX_ARCHIVE:
        raise ValueError("压缩包超过 256 MiB 限制。")
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if (
            len(entries) > MAX_FILES
            or sum(entry.file_size for entry in entries) > MAX_CONTENT
        ):
            raise ValueError("解压大小或文件数量超过限制。")
        seen = set()
        for entry in entries:
            portable_path(entry.filename)
            folded = unicodedata.normalize("NFC", entry.filename).casefold()
            file_type = stat.S_IFMT(entry.external_attr >> 16)
            if (
                folded in seen
                or entry.is_dir()
                or file_type not in {0, stat.S_IFREG}
                or entry.flag_bits & 1
            ):
                raise ValueError("包含重复路径、目录条目、链接、特殊文件或加密条目。")
            seen.add(folded)
            if entry.file_size > MAX_FILE or entry.compress_type not in {
                zipfile.ZIP_STORED,
                zipfile.ZIP_DEFLATED,
            }:
                raise ValueError("文件大小或压缩方式不受支持。")
            target = safe_path(destination, entry.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            with archive.open(entry) as source, target.open("xb") as output:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    size += len(chunk)
                    if size > MAX_FILE or size > entry.file_size:
                        raise ValueError("解压内容超出声明大小。")
                    output.write(chunk)
            os.chmod(target, 0o600)
    manifest_path = safe_path(destination, "manifest.json")
    if not manifest_path.is_file():
        raise ValueError("缺少 manifest.json，不是插件管家迁移包。")
    manifest = load_json(manifest_path)
    exact_keys(
        manifest,
        {
            "format",
            "format_version",
            "created_at",
            "astrbot_version",
            "platform",
            "plugins",
            "files",
            "directories",
        },
    )
    check_bundle_format(manifest)
    if (
        not isinstance(manifest["files"], dict)
        or not isinstance(manifest["plugins"], list)
        or not 1 <= len(manifest["plugins"]) <= 256
    ):
        raise ValueError("插件包清单无效。")
    if (
        type(manifest["created_at"]) not in {int, float}
        or not math.isfinite(manifest["created_at"])
        or manifest["created_at"] <= 0
        or not isinstance(manifest["astrbot_version"], str)
        or not isinstance(manifest["platform"], str)
        or not manifest["platform"]
    ):
        raise ValueError("插件包来源版本、平台或创建时间无效。")
    Version(manifest["astrbot_version"])
    actual = {
        path.relative_to(destination).as_posix() for path in tree_files(destination)
    } - {"manifest.json"}
    if actual != set(manifest["files"]):
        raise ValueError("文件清单与包内容不一致。")
    allowed = set()
    directories = set()
    names = set()
    for plugin in manifest["plugins"]:
        exact_keys(
            plugin,
            {
                "name",
                "directory",
                "display_name",
                "author",
                "version",
                "plugin_id",
                "was_enabled",
                "config",
                "data",
                "file_data",
            },
        )
        component(plugin["directory"], module=True)
        component(plugin["name"])
        if (
            plugin["directory"].casefold() in directories
            or plugin["name"].casefold() in names
        ):
            raise ValueError("包内插件身份重复。")
        directories.add(plugin["directory"].casefold())
        names.add(plugin["name"].casefold())
        for key in ("was_enabled", "config", "data", "file_data"):
            if type(plugin[key]) is not bool:
                raise ValueError("组件标记必须是布尔值。")
        prefix = f"payload/{plugin['directory']}"
        allowed.add(check_source_snapshot(destination, plugin, actual))
        metadata = read_metadata(safe_path(destination, prefix + "/code"))
        for field in ("name", "author", "version", "plugin_id"):
            if metadata[field] != plugin[field]:
                raise ValueError("包清单与插件元数据身份不一致。")
        if (metadata.get("display_name") or "") != plugin["display_name"]:
            raise ValueError("包清单与显示名称不一致。")
        for relative in actual:
            if relative.startswith(prefix + "/code/"):
                allowed.add(relative)
            elif relative == prefix + "/config.json" and plugin["config"]:
                allowed.add(relative)
            elif relative == prefix + "/kv.json" and plugin["data"]:
                allowed.add(relative)
            elif (
                relative.startswith(prefix + "/data/")
                and plugin["data"]
                and plugin["file_data"]
            ):
                allowed.add(relative)
        if plugin["config"] and prefix + "/config.json" not in actual:
            raise ValueError("包缺少已声明的配置。")
        if plugin["file_data"] and not plugin["data"]:
            raise ValueError("文件数据标记与组件声明冲突。")
        if plugin["data"]:
            entries = load_json(safe_path(destination, prefix + "/kv.json"))
            if not isinstance(entries, dict):
                raise ValueError("KV 快照必须是映射。")
            for value in entries.values():
                decode_value(value)
    if allowed != actual:
        raise ValueError("包包含未声明或越权的文件。")
    declared_directories = manifest["directories"]
    if (
        not isinstance(declared_directories, list)
        or len(declared_directories) > MAX_FILES
    ):
        raise ValueError("目录清单超过限制或格式不正确。")
    seen_directories = set()
    folded_files = {unicodedata.normalize("NFC", name).casefold() for name in actual}
    for relative in declared_directories:
        portable_path(relative)
        folded = unicodedata.normalize("NFC", relative).casefold()
        if folded in seen_directories or folded in folded_files:
            raise ValueError("目录和文件路径冲突或重复。")
        seen_directories.add(folded)
        valid = relative == "payload"
        for plugin in manifest["plugins"]:
            prefix = f"payload/{plugin['directory']}"
            valid = (
                valid
                or relative in {prefix, prefix + "/code"}
                or relative.startswith(prefix + "/code/")
            )
            if plugin["data"] and plugin["file_data"]:
                valid = (
                    valid
                    or relative == prefix + "/data"
                    or relative.startswith(prefix + "/data/")
                )
        if not valid:
            raise ValueError("包包含未声明的数据目录。")
        safe_path(destination, relative).mkdir(parents=True, exist_ok=True)
    for relative, record in manifest["files"].items():
        exact_keys(record, {"size", "sha256"})
        path = safe_path(destination, relative)
        if (
            type(record["size"]) is not int
            or record["size"] != path.stat().st_size
            or record["sha256"] != digest(path)
        ):
            raise ValueError("插件包校验失败，内容损坏或被修改。")
    actual_directories = {
        path.relative_to(destination).as_posix()
        for path in tree_directories(destination)
    }
    if actual_directories != set(declared_directories):
        raise ValueError("目录清单与包内容不一致。")
    return manifest
