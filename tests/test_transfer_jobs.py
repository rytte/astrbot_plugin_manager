"""Manual job retention, deletion boundaries and concurrent lifecycle protection."""

import asyncio
import threading
import time
from unittest.mock import Mock

import pytest
from astrbot_plugin_manager import transfers as transfer_module
from astrbot_plugin_manager.transfers import TransferService


def retained_job(transfers, kind="export", status="completed"):
    job = transfers._new_job("admin", kind)
    job.update(status=status, created_at=time.time() - 365 * 86400)
    transfers._save(job)
    directory = transfers.job_directory(job["id"])
    (directory / "package.zip").write_bytes(b"package")
    (directory / "rollback.zip").write_bytes(b"recovery")
    (directory / "plugins.astrbot-plugin-manifest.json").write_bytes(b"{}")
    (directory / "checked").mkdir()
    (directory / "checked" / "config.json").write_bytes(b'{"private": true}')
    return job, directory


@pytest.mark.parametrize(
    "kind,status",
    [
        ("export", "completed"),
        ("export", "failed"),
        ("import", "completed_with_warning"),
        ("import", "preview"),
        ("import", "rollback_failed"),
        ("manifest_export", "completed"),
        ("manifest_install", "completed"),
    ],
)
async def test_files_survive_restart_and_new_jobs_regardless_of_age(
    transfer_env, kind, status
):
    env = transfer_env
    job, directory = retained_job(env.plugin.transfers, kind, status)
    original = {
        path.relative_to(directory): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }
    restarted = TransferService(env.plugin.service, env.plugin._operations)
    await restarted.initialize()
    restarted.new_upload("admin")
    assert restarted.get_job(job["id"], "admin")["status"] == status
    assert not restarted.history("admin")[-1]["expired"]
    assert {
        path.relative_to(directory): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    } == original


@pytest.mark.parametrize(
    "kind", ["export", "import", "manifest_export", "manifest_install"]
)
async def test_delete_removes_the_entire_owned_job_without_touching_plugins(
    transfer_env, kind
):
    env = transfer_env
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    job, directory = retained_job(transfers, kind)
    other, other_directory = retained_job(transfers)
    code = transfers.code_root / target.root_dir_name / "main.py"
    original = code.read_bytes()
    assert await transfers.delete_job(job["id"], "admin", True) == {
        "id": job["id"],
        "deleted": True,
    }
    assert not directory.exists()
    assert job["id"] not in transfers.jobs
    assert other_directory.is_dir() and transfers.get_job(other["id"], "admin")
    assert code.read_bytes() == original
    with pytest.raises(ValueError, match="不存在"):
        transfers.download(job["id"], "admin", "package")
    restarted = TransferService(env.plugin.service, env.plugin._operations)
    await restarted.initialize()
    assert job["id"] not in restarted.jobs
    assert other["id"] in restarted.jobs
    env.manager._terminate_plugin.assert_not_awaited()
    env.manager.load.assert_not_awaited()


@pytest.mark.parametrize("status", ["preparing", "queued", "running"])
async def test_in_progress_jobs_cannot_be_deleted(transfer_env, status):
    transfers = transfer_env.plugin.transfers
    job, directory = retained_job(transfers, status=status)
    with pytest.raises(ValueError, match="结束后才能删除"):
        await transfers.delete_job(job["id"], "admin", True)
    assert directory.is_dir() and job["id"] in transfers.jobs


async def test_delete_requires_ownership_confirmation_and_valid_identifier(
    transfer_env,
):
    transfers = transfer_env.plugin.transfers
    job, directory = retained_job(transfers)
    for identifier, owner, acknowledged, reason in [
        (job["id"], "other", True, "无权访问"),
        (job["id"], "admin", False, "请确认"),
        ("../outside", "admin", True, "标识无效"),
        ("f" * 32, "admin", True, "不存在"),
    ]:
        with pytest.raises(ValueError, match=reason):
            await transfers.delete_job(identifier, owner, acknowledged)
        assert directory.is_dir() and job["id"] in transfers.jobs


async def test_delete_rejects_job_directory_replaced_with_a_link(transfer_env):
    transfers = transfer_env.plugin.transfers
    job, directory = retained_job(transfers)
    outside = transfers.root / "original-job"
    directory.rename(outside)
    try:
        directory.symlink_to(outside, target_is_directory=True)
    except OSError:
        outside.rename(directory)
        pytest.skip("Directory symlinks are unavailable")
    try:
        with pytest.raises(ValueError, match="链接"):
            await transfers.delete_job(job["id"], "admin", True)
        assert (outside / "package.zip").read_bytes() == b"package"
        assert job["id"] in transfers.jobs
    finally:
        directory.unlink()
        outside.rename(directory)


async def test_failed_precheck_cannot_be_deleted_during_file_cleanup(
    transfer_env, monkeypatch
):
    transfers = transfer_env.plugin.transfers
    job = transfers.new_upload("admin")
    entered = asyncio.Event()
    released = threading.Event()
    loop = asyncio.get_running_loop()
    saved = transfer_module.remove_path

    def delayed_remove(root, relative):
        loop.call_soon_threadsafe(entered.set)
        assert released.wait(10)
        saved(root, relative)

    monkeypatch.setattr(transfer_module, "remove_path", delayed_remove)
    preparing = asyncio.create_task(transfers.prepare_import(job))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(ValueError, match="结束后才能删除"):
            await transfers.delete_job(job["id"], "admin", True)
    finally:
        released.set()
    with pytest.raises(FileNotFoundError):
        await preparing
    assert job["status"] == "failed"
    await transfers.delete_job(job["id"], "admin", True)


@pytest.mark.parametrize("cancel_request", [False, True])
async def test_deletion_is_serialized_and_finishes_if_request_is_cancelled(
    transfer_env, monkeypatch, cancel_request
):
    transfers = transfer_env.plugin.transfers
    job, directory = retained_job(transfers)
    other, _ = retained_job(transfers)
    entered = asyncio.Event()
    released = threading.Event()
    loop = asyncio.get_running_loop()
    saved = transfers._delete_job_files

    def delayed_delete(value):
        loop.call_soon_threadsafe(entered.set)
        assert released.wait(10)
        saved(value)

    monkeypatch.setattr(transfers, "_delete_job_files", delayed_delete)
    deletion = asyncio.create_task(transfers.delete_job(job["id"], "admin", True))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(ValueError, match="正在删除"):
            transfers.get_job(job["id"], "admin")
        with pytest.raises(ValueError, match="已有"):
            await transfers.delete_job(other["id"], "admin", True)
        with pytest.raises(ValueError, match="已有"):
            transfers.new_upload("admin")
        if cancel_request:
            deletion.cancel()
            await asyncio.sleep(0)
    finally:
        released.set()
    if cancel_request:
        with pytest.raises(asyncio.CancelledError):
            await deletion
    else:
        await deletion
    assert not directory.exists() and job["id"] not in transfers.jobs
    assert not transfers.deleting_jobs
    with pytest.raises(ValueError, match="已删除"):
        transfers._start(job, Mock())


async def test_partial_delete_failure_keeps_a_readable_record_for_retry(
    transfer_env, monkeypatch
):
    env = transfer_env
    transfers = env.plugin.transfers
    job, directory = retained_job(transfers)
    saved = transfer_module.remove_path

    def failed_remove(root, relative):
        (directory / "job.json").unlink()
        (directory / "package.zip").unlink()
        raise PermissionError("injected deletion failure")

    monkeypatch.setattr(transfer_module, "remove_path", failed_remove)
    with pytest.raises(ValueError, match="记录已保留"):
        await transfers.delete_job(job["id"], "admin", True)
    assert job["id"] in transfers.jobs and (directory / "job.json").is_file()
    restarted = TransferService(env.plugin.service, env.plugin._operations)
    await restarted.initialize()
    assert restarted.get_job(job["id"], "admin")
    monkeypatch.setattr(transfer_module, "remove_path", saved)
    await restarted.delete_job(job["id"], "admin", True)
    assert not directory.exists()


async def test_all_history_remains_accessible_for_manual_deletion(transfer_env):
    transfers = transfer_env.plugin.transfers
    identifiers = set()
    for _ in range(105):
        job = transfers._new_job("admin", "export")
        job["status"] = "completed"
        identifiers.add(job["id"])
    assert {job["id"] for job in transfers.history("admin")} == identifiers
    assert not transfers.history("other")
