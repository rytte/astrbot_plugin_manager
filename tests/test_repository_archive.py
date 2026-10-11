"""Repository download routing, bounds and cancellation without network access."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_manager import repository_archive as repository


@pytest.mark.parametrize(
    "url, archive",
    [
        (
            "https://github.com/alice/demo.git",
            "https://github.com/alice/demo/archive/HEAD.zip",
        ),
        (
            "https://github.com/alice/demo/tree/feature/test",
            "https://github.com/alice/demo/archive/refs/heads/feature/test.zip",
        ),
    ],
)
async def test_github_uses_bounded_archive_downloader(
    monkeypatch, tmp_path, url, archive
):
    download = AsyncMock()
    monkeypatch.setattr(repository, "download_market_archive", download)
    output = tmp_path / "download.zip"
    await repository.download_repository_archive(url, output)
    download.assert_awaited_once_with(archive, output)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/alice/demo",
        "file:///repo",
        "ssh://example.com/alice/demo",
        "https://user:password@example.com/alice/demo",
        "https://github.com/alice/demo?token=secret",
    ],
)
async def test_unsafe_repository_never_starts_download(monkeypatch, tmp_path, url):
    download = AsyncMock()
    command = AsyncMock()
    monkeypatch.setattr(repository, "download_market_archive", download)
    monkeypatch.setattr(repository, "_git_command", command)
    with pytest.raises(ValueError):
        await repository.download_repository_archive(url, tmp_path / "download.zip")
    download.assert_not_awaited()
    command.assert_not_awaited()


async def test_other_repository_requires_git(monkeypatch, tmp_path):
    monkeypatch.setattr(repository.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="需要 Git"):
        await repository.download_repository_archive(
            "https://example.com/alice/demo.git", tmp_path / "download.zip"
        )


async def test_git_route_clones_without_checkout_and_archives_head(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(repository.shutil, "which", lambda name: "git")
    command = AsyncMock()
    monkeypatch.setattr(repository, "_git_command", command)
    output = tmp_path / "download.zip"
    url = "https://git.example.com/group/demo.git"
    await repository.download_repository_archive(url, output)
    assert command.await_count == 2
    clone, archive = command.await_args_list
    assert clone.args[1] == [
        "clone",
        "--quiet",
        "--depth",
        "1",
        "--single-branch",
        "--no-tags",
        "--no-checkout",
        "--",
        url,
        str(tmp_path / "repository.git"),
    ]
    assert archive.args[1] == [
        "-C",
        str(tmp_path / "repository.git"),
        "archive",
        "--format=zip",
        "--output=" + str(output),
        "HEAD",
    ]


def test_download_limits_include_git_files_and_zip(monkeypatch, tmp_path):
    (tmp_path / "pack.tmp").write_bytes(b"0123456789")
    output = tmp_path / "download.zip"
    monkeypatch.setattr(repository, "MAX_CONTENT", 5)
    with pytest.raises(ValueError, match="大小"):
        repository._check_size(tmp_path, output)
    monkeypatch.setattr(repository, "MAX_CONTENT", 100)
    monkeypatch.setattr(repository, "MAX_FILES", 0)
    with pytest.raises(ValueError, match="文件数"):
        repository._check_size(tmp_path, output)
    monkeypatch.setattr(repository, "MAX_FILES", 100)
    monkeypatch.setattr(repository, "MAX_ARCHIVE", 5)
    output.write_bytes(b"0123456789")
    with pytest.raises(ValueError, match="256 MiB"):
        repository._check_size(tmp_path, output)


async def test_git_timeout_stops_process_tree_and_joins_pipe(monkeypatch, tmp_path):
    stopped = asyncio.Event()

    async def communicate():
        await stopped.wait()
        return None, b""

    process = SimpleNamespace(returncode=None, communicate=communicate)

    async def stop(value):
        assert value is process
        process.returncode = -9
        stopped.set()

    create = AsyncMock(return_value=process)
    monkeypatch.setattr(repository.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(repository, "_stop_process_tree", stop)
    monkeypatch.setattr(repository.shutil, "which", lambda name: "git")
    monkeypatch.setattr(repository, "REPOSITORY_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(ValueError, match="超时"):
        await repository.download_repository_archive(
            "https://example.com/alice/demo.git", tmp_path / "download.zip"
        )
    assert stopped.is_set()
    arguments = create.await_args.args
    assert "protocol.allow=never" in arguments
    assert "protocol.https.allow=always" in arguments
    assert any(argument.startswith("core.hooksPath=") for argument in arguments)
    assert create.await_args.kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
