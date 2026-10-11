"""Stage HTTPS repository sources without checking out or executing plugin code."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
from pathlib import Path
from urllib.parse import quote

from astrbot.core.repository import parse_repository_url

from .market_archive import download_market_archive
from .sources import repository_url
from .transfer_bundle import MAX_ARCHIVE, MAX_CONTENT, MAX_FILES, safe_path

REPOSITORY_TIMEOUT_SECONDS = 60


async def _stop_process_tree(process) -> None:
    """Git can spawn HTTPS helpers; stop them before joining its output pipe."""
    if os.name == "nt":
        terminator = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        await terminator.wait()
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _check_size(directory: Path, output: Path) -> None:
    total = count = 0
    for parent, _, files in os.walk(directory):
        for filename in files:
            try:
                size = (Path(parent) / filename).stat().st_size
            except FileNotFoundError:
                continue  # Git renames temporary pack files while downloading.
            count += 1
            total += size
            if total > MAX_CONTENT or count > MAX_FILES:
                raise ValueError("仓库下载大小或文件数超过限制。")
    if output.exists() and output.stat().st_size > MAX_ARCHIVE:
        raise ValueError("仓库安装包超过 256 MiB 限制。")


async def _git_command(
    executable: str, arguments: list[str], directory: Path, output: Path
) -> None:
    environment = os.environ.copy()
    environment.update(
        GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull
    )
    process = await asyncio.create_subprocess_exec(
        executable,
        "-c",
        "core.hooksPath=" + os.devnull,
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.https.allow=always",
        *arguments,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
        **(
            {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW
            }
            if os.name == "nt"
            else {"start_new_session": True}
        ),
    )
    communication = asyncio.create_task(process.communicate())
    try:
        while not communication.done():
            await asyncio.wait({communication}, timeout=0.25)
            await asyncio.to_thread(_check_size, directory, output)
        await communication
        if process.returncode:
            raise ValueError("仓库下载失败，请检查仓库地址、访问权限及服务器日志。")
    finally:
        if process.returncode is None:
            await _stop_process_tree(process)
        await communication


async def download_repository_archive(url: str, output: Path) -> None:
    url = repository_url(url)
    repository = parse_repository_url(url)
    if repository.provider == "github" and repository.transport == "archive":
        revision = "refs/heads/" + repository.branch if repository.branch else "HEAD"
        archive_url = (
            "https://github.com/"
            + quote(repository.owner, safe="")
            + "/"
            + quote(repository.name, safe="")
            + "/archive/"
            + quote(revision, safe="/")
            + ".zip"
        )
        await download_market_archive(archive_url, output)
        return
    executable = shutil.which("git")
    if not executable:
        raise ValueError("安装此仓库需要 Git，请先在 AstrBot 运行环境中安装 Git。")
    checkout = safe_path(output.parent, "repository.git")

    async def download():
        await _git_command(
            executable,
            [
                "clone",
                "--quiet",
                "--depth",
                "1",
                "--single-branch",
                "--no-tags",
                "--no-checkout",
                "--",
                url,
                str(checkout),
            ],
            output.parent,
            output,
        )
        await _git_command(
            executable,
            [
                "-C",
                str(checkout),
                "archive",
                "--format=zip",
                "--output=" + str(output),
                "HEAD",
            ],
            output.parent,
            output,
        )
        await asyncio.to_thread(_check_size, output.parent, output)

    try:
        await asyncio.wait_for(download(), timeout=REPOSITORY_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        raise ValueError("仓库下载超时，请稍后重试。") from exc
