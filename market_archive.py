"""Bounded market ZIP staging; never execute code, pip, or archive instructions."""

from __future__ import annotations

import asyncio
import os
import stat
import unicodedata
import zipfile
import zlib
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp

from .transfer_bundle import (
    MAX_ARCHIVE,
    MAX_CONTENT,
    MAX_FILE,
    MAX_FILES,
    portable_path,
    safe_path,
)


async def download_market_archive(url: str, output: Path) -> None:
    await asyncio.wait_for(_download_market_archive(url, output), timeout=60)


async def _download_market_archive(url: str, output: Path) -> None:
    timeout = aiohttp.ClientTimeout(total=60, connect=15)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
        # Check each redirect before issuing its request, including credential changes.
        for _ in range(11):
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("插件安装包的下载地址必须是无凭据 HTTPS URL。")
            async with session.get(url, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise ValueError("安装包重定向缺少目标地址。")
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise ValueError(f"插件安装包下载失败：HTTP {response.status}。")
                if response.content_length and response.content_length > MAX_ARCHIVE:
                    raise ValueError("插件安装包超过 256 MiB 限制。")
                size = 0
                with output.open("xb") as file:
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_ARCHIVE:
                            raise ValueError("插件安装包超过 256 MiB 限制。")
                        file.write(chunk)
                return
        raise ValueError("安装包重定向次数过多。")


def unpack_market_archive(archive_path: Path, destination: Path) -> Path:
    """Accept root metadata or one archive wrapper; reject ambiguous layouts."""
    if archive_path.stat().st_size > MAX_ARCHIVE:
        raise ValueError("插件安装包超过 256 MiB 限制。")
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if (
                len(entries) > MAX_FILES
                or sum(item.file_size for item in entries) > MAX_CONTENT
            ):
                raise ValueError("插件安装包的解压大小或文件数超过限制。")
            seen = {}
            explicit = set()
            for item in entries:
                relative = item.filename.rstrip("/") if item.is_dir() else item.filename
                portable_path(relative)
                mode = stat.S_IFMT(item.external_attr >> 16)
                if (
                    mode
                    not in ({0, stat.S_IFDIR} if item.is_dir() else {0, stat.S_IFREG})
                    or item.flag_bits & 1
                ):
                    raise ValueError("插件安装包包含链接、特殊文件或加密条目。")
                if item.file_size > MAX_FILE or item.compress_type not in {
                    zipfile.ZIP_STORED,
                    zipfile.ZIP_DEFLATED,
                }:
                    raise ValueError("插件安装包的文件大小或压缩方式不受支持。")
                folded = unicodedata.normalize("NFC", relative).casefold()
                if folded in explicit:
                    raise ValueError("插件安装包包含重复路径。")
                explicit.add(folded)
                parts = relative.split("/")
                for index in range(1, len(parts) + 1):
                    path = "/".join(parts[:index])
                    key = unicodedata.normalize("NFC", path).casefold()
                    kind = (
                        "directory" if index < len(parts) or item.is_dir() else "file"
                    )
                    if key in seen and seen[key] != (path, kind):
                        raise ValueError(
                            "插件安装包包含大小写、Unicode 或文件目录冲突。"
                        )
                    seen[key] = (path, kind)
                    if len(seen) > MAX_FILES:
                        raise ValueError("插件安装包目录数量超过限制。")
                target = safe_path(destination, relative)
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                size = 0
                with archive.open(item) as source, target.open("xb") as output:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        size += len(chunk)
                        if size > item.file_size or size > MAX_FILE:
                            raise ValueError("安装包实际解压大小超出声明。")
                        output.write(chunk)
                os.chmod(target, 0o600)
    except (zipfile.BadZipFile, EOFError, zlib.error) as exc:
        raise ValueError("插件安装包 ZIP 无效或已损坏。") from exc
    if (destination / "metadata.yaml").is_file():
        return destination
    children = list(destination.iterdir())
    if (
        len(children) == 1
        and children[0].is_dir()
        and (children[0] / "metadata.yaml").is_file()
    ):
        return children[0]
    raise ValueError("插件安装包必须在根目录或唯一外层目录包含 metadata.yaml。")
