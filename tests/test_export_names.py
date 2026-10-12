"""User export labels remain safe, persistent and separate from storage paths."""

import pytest
from astrbot_plugin_manager.export_names import export_filename, export_name
from astrbot_plugin_manager.transfers import TransferService


@pytest.mark.parametrize(
    "value",
    [
        None,
        123,
        "../outside",
        "a/b",
        "a\\b",
        "a:b",
        'a"b',
        "a*b",
        "a?b",
        "a<b",
        "a>b",
        "a|b",
        "CON",
        "nul.txt",
        ".",
        "..",
        "name.",
        "name\n",
        "name\x7f",
        "备" * 81,
    ],
)
def test_invalid_export_names_fail_explicitly(value):
    with pytest.raises(ValueError, match="导出名称"):
        export_name(value)


def test_export_names_allow_chinese_and_optional_whitespace():
    assert export_name(" 生产环境备份 ") == "生产环境备份"
    assert export_name("  ") == ""
    assert export_name("备" * 80) == "备" * 80
    job = {"id": "a" * 32, "name": "生产环境备份"}
    assert export_filename(job, "package") == "生产环境备份.zip"
    assert export_filename(job, "manifest") == "生产环境备份.json"
    assert (
        export_filename({**job, "name": ""}, "package")
        == "astrbot-package-aaaaaaaa.zip"
    )
    assert export_filename({**job, "name": ""}, "manifest") == "plugins.json"


@pytest.mark.parametrize("name", ["", "生产环境备份"])
async def test_package_name_survives_restart_and_keeps_private_storage_path(
    transfer_env, name
):
    env = transfer_env
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    rule = await transfers.save_rule(
        [{"directory": target.root_dir_name, "config": False, "data": False}]
    )
    started = await transfers.start_export("admin", rule["updated_at"], True, name=name)
    await transfers.task
    restarted = TransferService(env.plugin.service, env.plugin._operations)
    await restarted.initialize()
    job = restarted.public_job(restarted.get_job(started["id"], "admin"))
    expected_name = name or "astrbot-package-" + started["id"][:8]
    assert job["name"] == expected_name
    assert job["download_filename"] == expected_name + ".zip"
    assert restarted.download(job["id"], "admin", "package").name == "package.zip"
