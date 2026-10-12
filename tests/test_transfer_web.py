"""Exercise real FastAPI extension dispatch and dashboard authentication."""

import json
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import unquote

import httpx
import jwt
import pytest
import yaml
from astrbot.dashboard.api.files import legacy_router as file_router
from astrbot.dashboard.api.plugins import router
from astrbot.dashboard.responses import ApiError
from astrbot.dashboard.services.file_service import FileService
from fastapi import FastAPI
from fastapi.responses import JSONResponse


async def test_manifest_repository_upload_poll_and_selective_install(
    web_client, monkeypatch, core_installs
):
    env, client = web_client
    name = "astrbot_plugin_repository_share"
    url = "https://github.com/alice/repository_share"
    icon = b"\x89PNG\r\n\x1a\nrepository icon fixture"
    core_installs[url] = {"name": name, "author": "Alice", "version": "1.0"}

    async def download(source, path):
        assert source == url
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "metadata.yaml",
                yaml.safe_dump(
                    {
                        "name": name,
                        "author": "Alice",
                        "version": "1.0",
                        "desc": "Shared",
                    }
                ),
            )
            archive.writestr("main.py", "raise RuntimeError('must not execute')")
            archive.writestr("logo.png", icon)

    monkeypatch.setattr(
        "astrbot_plugin_manager.manifests.download_repository_archive", download
    )
    state = (await client.get(management_endpoint("state"))).json()["data"]
    manifest = {
        "format": "astrbot-plugin-manager-manifest",
        "schema_version": 1,
        "name": "分享清单",
        "plugins": [{"display_name": "分享插件", "repo_url": url}],
    }
    response = await client.post(
        management_endpoint("manifest-upload/" + state["csrf"]),
        files={
            "file": ("plugins.json", json.dumps(manifest).encode(), "application/json")
        },
    )
    assert response.status_code == 200, response.text
    identifier = response.json()["data"]["job"]["id"]
    await env.plugin.transfers.task
    preview = await client.get(
        management_endpoint("manifest-preview"), params={"id": identifier}
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["data"]["plugins"][0]["sources"][0]["status"] == "install"
    source = preview.json()["data"]["plugins"][0]["sources"][0]
    assert "logo_path" not in source
    assert "remote" not in source
    assert str(env.plugin.transfers.job_directory(identifier)) not in preview.text
    assert source["logo"].startswith("/api/file/")
    downloaded_icon = await client.get(source["logo"], headers={"Authorization": ""})
    assert downloaded_icon.status_code == 200
    assert downloaded_icon.content == icon
    assert downloaded_icon.headers["content-type"].startswith("image/png")
    assert (await client.get(source["logo"])).status_code == 404
    other = jwt.encode(
        {"username": "other", "exp": time.time() + 3600},
        env.context.get_config()["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    denied = await client.get(
        management_endpoint("manifest-preview"),
        params={"id": identifier},
        headers={"Authorization": "Bearer " + other},
    )
    assert denied.status_code == 400
    draft = [{"id": "0", "source": "repository", "selected": False}]
    saved = await client.post(
        management_endpoint("manifest-draft"),
        json={"csrf": state["csrf"], "id": identifier, "draft": draft},
    )
    assert saved.status_code == 200, saved.text
    restored = await client.get(
        management_endpoint("manifest-preview"), params={"id": identifier}
    )
    assert restored.json()["data"]["draft"] == draft
    refreshed_icon = restored.json()["data"]["plugins"][0]["sources"][0]["logo"]
    assert refreshed_icon != source["logo"]
    assert (await client.get(refreshed_icon)).content == icon
    assert not (Path(env.manager.plugin_store_path) / name).exists()
    other_state = (
        await client.get(
            management_endpoint("state"), headers={"Authorization": "Bearer " + other}
        )
    ).json()["data"]
    for action, extra in [
        ("manifest-draft", {"draft": draft}),
        ("manifest-recheck", {}),
    ]:
        wrong_nonce = await client.post(
            management_endpoint(action),
            json={"csrf": "wrong", "id": identifier, **extra},
        )
        assert wrong_nonce.status_code == 403
        wrong_owner = await client.post(
            management_endpoint(action),
            headers={"Authorization": "Bearer " + other},
            json={"csrf": other_state["csrf"], "id": identifier, **extra},
        )
        assert wrong_owner.status_code == 400
    body = {
        "csrf": state["csrf"],
        "id": identifier,
        "selections": [{"id": "0", "source": "repository"}],
        "acknowledged": False,
    }
    rejected = await client.post(management_endpoint("manifest-install"), json=body)
    assert rejected.status_code == 400
    body["acknowledged"] = True
    body["csrf"] = "wrong"
    rejected = await client.post(management_endpoint("manifest-install"), json=body)
    assert rejected.status_code == 403
    body["csrf"] = state["csrf"]
    installed = await client.post(management_endpoint("manifest-install"), json=body)
    assert installed.status_code == 200, installed.text
    await env.plugin.transfers.task
    assert env.plugin.transfers.get_job(identifier, "admin")["status"] == "completed"
    assert env.prefs["plugin_install_sources"][name]["install_method"] == "repository"
    replay = await client.post(
        management_endpoint("manifest-recheck"),
        json={"csrf": state["csrf"], "id": identifier},
    )
    assert replay.status_code == 400
    env.manager.install_plugin.assert_awaited_once_with(
        url, "", ignore_version_check=False, download_url=""
    )
    assert env.context.get_registered_star(name).activated
    assert not env.plugin.transfers.get_job(identifier, "admin").get("reload_required")
    manifest["schema_version"] = 2
    unsupported = await client.post(
        management_endpoint("manifest-upload/" + state["csrf"]),
        files={
            "file": (
                "unsupported.json",
                json.dumps(manifest).encode(),
                "application/json",
            )
        },
    )
    assert unsupported.status_code == 400
    assert "v1" in unsupported.json()["message"]


async def test_manifest_repository_icon_registration_failure_keeps_preview_ready(
    web_client, monkeypatch
):
    env, client = web_client

    async def download(source, path):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "metadata.yaml",
                yaml.safe_dump(
                    {
                        "name": "icon_demo",
                        "author": "Alice",
                        "version": "1.0",
                        "desc": "Icon test plugin",
                    }
                ),
            )
            archive.writestr("main.py", "")
            archive.writestr("logo.png", b"icon fixture")

    monkeypatch.setattr(
        "astrbot_plugin_manager.manifests.download_repository_archive", download
    )
    register = AsyncMock(return_value=None)
    monkeypatch.setattr(env.plugin.service.web, "get_plugin_logo_token", register)
    preview = await env.plugin.manifests.preview(
        "admin",
        {
            "format": "astrbot-plugin-manager-manifest",
            "schema_version": 1,
            "name": "图标缺失清单",
            "plugins": [
                {"display_name": "示例", "repo_url": "https://github.com/alice/demo"}
            ],
        },
    )
    response = await client.get(
        management_endpoint("manifest-preview"), params={"id": preview["job"]["id"]}
    )
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["ready"] is True
    assert result["plugins"][0]["sources"][0]["logo"] is None
    assert result["plugins"][0]["sources"][0]["status"] == "install"
    register.assert_awaited_once()


async def test_standalone_manager_data_routes_are_removed(web_client):
    env, client = web_client
    assert not any("/data-" in entry[0] for entry in env.context.registered_web_apis)
    for action in ("data-export", "data-import", "data-upload/token"):
        assert (await client.post(management_endpoint(action))).json()[
            "status"
        ] == "error"
    assert (await client.get(management_endpoint("data-download"))).json()[
        "status"
    ] == "error"


@pytest.fixture
async def web_client(transfer_env):
    env = transfer_env
    app = FastAPI()

    @app.exception_handler(ApiError)
    async def handle_error(request, exception):
        return JSONResponse(
            {"status": "error", "message": exception.message},
            status_code=exception.status_code,
        )

    app.state.jwt_secret = env.context.get_config()["dashboard"]["jwt_secret"]
    app.state.core_lifecycle = SimpleNamespace(star_context=env.context)
    app.state.services = SimpleNamespace(files=FileService())
    app.include_router(router, prefix="/api/v1")
    app.include_router(file_router)
    token = jwt.encode(
        {"username": "admin", "exp": time.time() + 3600},
        app.state.jwt_secret,
        algorithm="HS256",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer " + token},
    ) as client:
        yield env, client


def endpoint(action):
    return "/api/v1/plugins/extensions/astrbot_plugin_manager/transfer/" + action


async def test_recovery_routes_require_owned_jobs_csrf_and_confirmation(web_client):
    env, client = web_client
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    code = transfers.code_root / target.root_dir_name / "main.py"
    code.write_text("# imported\n", encoding="utf-8")
    rule = await transfers.save_rule(
        [{"directory": target.root_dir_name, "config": False, "data": False}]
    )
    exported = await transfers.start_export("admin", rule["updated_at"], True)
    await transfers.task
    package = transfers.download(exported["id"], "admin", "package").read_bytes()
    code.write_text("# private pre-import file\n", encoding="utf-8")
    state = (await client.get(endpoint("state"))).json()["data"]
    uploaded = await client.post(
        endpoint("upload/" + state["csrf"]),
        files={"file": ("plugins.zip", package, "application/zip")},
    )
    assert uploaded.status_code == 200, uploaded.text
    source_id = uploaded.json()["data"]["job"]["id"]
    selection = {
        "directory": target.root_dir_name,
        "code": True,
        "config": False,
        "data": False,
    }
    imported = await client.post(
        endpoint("import"),
        json={
            "csrf": state["csrf"],
            "id": source_id,
            "selections": [selection],
            "acknowledged": True,
        },
    )
    assert imported.status_code == 200, imported.text
    await transfers.task
    for identifier in (source_id, "../outside"):
        denied = await client.post(
            endpoint("rollback-preview"), json={"csrf": "wrong", "id": identifier}
        )
        assert denied.status_code == 403
    preview = await client.post(
        endpoint("rollback-preview"), json={"csrf": state["csrf"], "id": source_id}
    )
    assert preview.status_code == 200, preview.text
    assert "private pre-import file" not in preview.text
    preview_id = preview.json()["data"]["job"]["id"]
    assert (
        await client.get(endpoint("rollback-plan"), params={"id": preview_id})
    ).status_code == 200
    other_token = jwt.encode(
        {"username": "other", "exp": time.time() + 3600},
        env.context.get_config()["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    other_headers = {"Authorization": "Bearer " + other_token}
    other_state = (await client.get(endpoint("state"), headers=other_headers)).json()[
        "data"
    ]
    assert (
        await client.get(
            endpoint("rollback-plan"), params={"id": preview_id}, headers=other_headers
        )
    ).status_code == 400
    denied = await client.post(
        endpoint("rollback-preview"),
        headers=other_headers,
        json={"csrf": other_state["csrf"], "id": source_id},
    )
    assert denied.status_code == 400
    body = {
        "csrf": state["csrf"],
        "id": preview_id,
        "selections": [selection],
        "acknowledged": True,
    }
    assert (
        await client.post(endpoint("rollback"), json={**body, "csrf": "wrong"})
    ).status_code == 403
    assert (
        await client.post(endpoint("rollback"), json={**body, "acknowledged": False})
    ).status_code == 400
    assert (
        await client.post(
            endpoint("rollback"),
            json={**body, "selections": [{**selection, "config": True}]},
        )
    ).status_code == 400
    assert (
        await client.post(
            endpoint("rollback"), json={**body, "archive_path": "outside.zip"}
        )
    ).status_code == 400
    started = await client.post(endpoint("rollback"), json=body)
    assert started.status_code == 200, started.text
    await transfers.task
    assert transfers.jobs[preview_id]["status"] == "completed"
    assert code.read_text() == "# private pre-import file\n"
    assert (
        await client.get(endpoint("rollback-plan"), params={"id": preview_id})
    ).status_code == 400
    downloaded = await client.get(
        endpoint("download"), params={"id": preview_id, "kind": "rollback"}
    )
    assert downloaded.status_code == 200
    assert (
        await client.get(
            endpoint("download"),
            params={"id": preview_id, "kind": "rollback"},
            headers=other_headers,
        )
    ).status_code == 400


async def test_page_state_and_storage_statistics_are_separate(web_client):
    env, client = web_client
    target = env.make_transfer_plugin()
    env.kv_values[("plugin", target.plugin_id)] = {"secret": "private fixture"}
    response = await client.get(endpoint("state"))
    assert response.status_code == 200
    basic = next(
        item
        for item in response.json()["data"]["plugins"]
        if item["directory"] == target.root_dir_name
    )
    assert basic["statistics_status"] == "pending"
    assert basic["code_size"] is None and basic["kv_count"] is None
    env.preferences.range_get_async.assert_not_awaited()

    response = await client.get(endpoint("statistics"))
    assert response.status_code == 200, response.text
    detailed = next(
        item
        for item in response.json()["data"]["plugins"]
        if item["directory"] == target.root_dir_name
    )
    assert detailed["statistics_status"] == "ready"
    assert detailed["code_size"] > 0 and detailed["kv_count"] == 1
    assert "private fixture" not in response.text
    assert "logo" not in detailed


async def test_plugin_icons_use_native_file_tokens_and_refresh_after_consumption(
    web_client,
):
    env, client = web_client
    target = env.make_transfer_plugin()
    logo = Path(env.manager.plugin_store_path) / target.root_dir_name / "logo.png"
    content = b"\x89PNG\r\n\x1a\nicon fixture"
    logo.write_bytes(content)
    response = await client.get(endpoint("state"))
    assert response.status_code == 200, response.text
    plugins = response.json()["data"]["plugins"]
    entry = next(item for item in plugins if item["directory"] == target.root_dir_name)
    assert entry["logo"].startswith("/api/file/")
    assert str(logo) not in response.text
    assert any(item["logo"] is None for item in plugins)
    downloaded = await client.get(entry["logo"], headers={"Authorization": ""})
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"].startswith("image/png")
    assert downloaded.content == content
    assert (await client.get(entry["logo"])).status_code == 404
    refreshed = (await client.get(endpoint("state"))).json()["data"]["plugins"]
    updated = next(
        item for item in refreshed if item["directory"] == target.root_dir_name
    )
    assert updated["logo"] != entry["logo"]
    assert (await client.get(updated["logo"])).content == content


async def test_plugin_icon_failure_does_not_block_page_state(web_client, monkeypatch):
    env, client = web_client
    target = env.make_transfer_plugin()
    logo = Path(env.manager.plugin_store_path) / target.root_dir_name / "logo.png"
    logo.write_bytes(b"icon fixture")
    monkeypatch.setattr(
        env.plugin.service.web, "get_plugin_logo_token", AsyncMock(return_value=None)
    )
    response = await client.get(endpoint("state"))
    assert response.status_code == 200
    entry = next(
        item
        for item in response.json()["data"]["plugins"]
        if item["directory"] == target.root_dir_name
    )
    assert entry["logo"] is None
    assert not entry["blocked"]


async def test_plugin_icon_cannot_expose_linked_file_outside_plugin(
    web_client, monkeypatch
):
    env, client = web_client
    target = env.make_transfer_plugin()
    outside = env.root / "outside.png"
    outside.write_bytes(b"private fixture")
    logo = Path(env.manager.plugin_store_path) / target.root_dir_name / "logo.png"
    try:
        logo.symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation is unavailable")
    register = AsyncMock(return_value="must-not-be-issued")
    monkeypatch.setattr(env.plugin.service.web, "get_plugin_logo_token", register)
    response = await client.get(endpoint("state"))
    assert response.status_code == 200
    entry = next(
        item
        for item in response.json()["data"]["plugins"]
        if item["directory"] == target.root_dir_name
    )
    assert entry["logo"] is None
    register.assert_not_awaited()


async def test_state_rules_and_csrf_through_real_extension_router(web_client):
    env, client = web_client
    target = env.make_transfer_plugin()
    response = await client.get(endpoint("state"))
    assert response.status_code == 200, response.text
    state = response.json()["data"]
    assert any(
        plugin["directory"] == target.root_dir_name for plugin in state["plugins"]
    )
    selection = {"directory": target.root_dir_name, "config": False, "data": False}
    denied = await client.post(
        endpoint("rule"), json={"csrf": "wrong", "selections": [selection]}
    )
    assert denied.status_code == 403
    saved = await client.post(
        endpoint("rule"), json={"csrf": state["csrf"], "selections": [selection]}
    )
    assert saved.status_code == 200
    assert saved.json()["data"]["selections"][0]["name"] == target.name
    preview = await client.post(
        endpoint("export-preview"), json={"csrf": state["csrf"]}
    )
    assert preview.status_code == 200
    denied = await client.post(
        endpoint("export"),
        json={
            "csrf": state["csrf"],
            "rule_updated_at": preview.json()["data"]["rule_updated_at"],
            "name": "",
            "acknowledged": False,
        },
    )
    assert denied.status_code == 400


async def test_export_current_selection_without_saving_through_router(web_client):
    env, client = web_client
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    state = (await client.get(endpoint("state"))).json()["data"]
    selection = {"directory": target.root_dir_name, "config": False, "data": False}
    preview = await client.post(
        endpoint("export-preview"),
        json={"csrf": state["csrf"], "selections": [selection]},
    )
    assert preview.status_code == 200, preview.text
    snapshot = preview.json()["data"]["selections"]
    assert snapshot[0]["name"] == target.name
    assert snapshot[0]["plugin_id"] == target.plugin_id
    for csrf, acknowledged, expected in (
        ("wrong", True, 403),
        (state["csrf"], False, 400),
    ):
        response = await client.post(
            endpoint("export"),
            json={
                "csrf": csrf,
                "selections": snapshot,
                "acknowledged": acknowledged,
                "name": "",
            },
        )
        assert response.status_code == expected
        assert not transfers.jobs
    response = await client.post(
        endpoint("export"),
        json={
            "csrf": state["csrf"],
            "selections": snapshot,
            "acknowledged": True,
            "name": "生产环境备份",
        },
    )
    assert response.status_code == 200, response.text
    await transfers.task
    job = response.json()["data"]
    assert job["name"] == "生产环境备份"
    assert job["download_filename"] == "生产环境备份.zip"
    assert transfers.get_job(job["id"], "admin")["status"] == "completed"
    downloaded = await client.get(
        endpoint("download"), params={"id": job["id"], "kind": "package"}
    )
    assert downloaded.status_code == 200
    assert "生产环境备份.zip" in unquote(downloaded.headers["content-disposition"])
    assert transfers.download(job["id"], "admin", "package").name == "package.zip"
    assert not transfers.rule_path.exists()


@pytest.mark.parametrize("kind", ["package", "manifest"])
@pytest.mark.parametrize("name", [None, "../outside", "备" * 81])
async def test_export_rejects_invalid_names_before_creating_jobs(
    web_client, kind, name
):
    env, client = web_client
    target = env.make_transfer_plugin()
    state = (await client.get(endpoint("state"))).json()["data"]
    if kind == "manifest":
        management = (await client.get(management_endpoint("state"))).json()["data"]
        url = management_endpoint("manifest-export")
        body = {
            "csrf": management["csrf"],
            "directories": [target.root_dir_name],
            "name": name,
        }
    else:
        preview = await client.post(
            endpoint("export-preview"),
            json={
                "csrf": state["csrf"],
                "selections": [
                    {"directory": target.root_dir_name, "config": False, "data": False}
                ],
            },
        )
        url = endpoint("export")
        body = {
            "csrf": state["csrf"],
            "selections": preview.json()["data"]["selections"],
            "acknowledged": True,
            "name": name,
        }
    rejected = await client.post(url, json=body)
    assert rejected.status_code == 400
    assert "导出名称" in rejected.json()["message"]
    assert not env.plugin.transfers.jobs
    env.manager._terminate_plugin.assert_not_awaited()


async def test_manual_job_deletion_checks_owner_and_csrf_and_removes_downloads(
    web_client,
):
    env, client = web_client
    transfers = env.plugin.transfers
    job = transfers._new_job("admin", "export")
    job["status"] = "completed"
    transfers._save(job)
    directory = transfers.job_directory(job["id"])
    (directory / "package.zip").write_bytes(b"private backup")
    state = (await client.get(endpoint("state"))).json()["data"]
    for body, status in [
        ({"csrf": "wrong", "id": job["id"], "acknowledged": True}, 403),
        ({"csrf": state["csrf"], "id": job["id"], "acknowledged": False}, 400),
        ({"csrf": state["csrf"], "id": "../outside", "acknowledged": True}, 400),
    ]:
        response = await client.post(endpoint("delete-job"), json=body)
        assert response.status_code == status, response.text
        assert directory.is_dir()
    other_token = jwt.encode(
        {"username": "other", "exp": time.time() + 3600},
        env.context.get_config()["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    other_headers = {"Authorization": "Bearer " + other_token}
    other_state = (await client.get(endpoint("state"), headers=other_headers)).json()[
        "data"
    ]
    denied = await client.post(
        endpoint("delete-job"),
        headers=other_headers,
        json={"csrf": other_state["csrf"], "id": job["id"], "acknowledged": True},
    )
    assert denied.status_code == 400 and "无权访问" in denied.text
    assert directory.is_dir()
    deleted = await client.post(
        endpoint("delete-job"),
        json={"csrf": state["csrf"], "id": job["id"], "acknowledged": True},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"] == {"id": job["id"], "deleted": True}
    assert not directory.exists()
    assert not (await client.get(endpoint("jobs"))).json()["data"]["jobs"]
    assert (
        await client.get(
            endpoint("download"), params={"id": job["id"], "kind": "package"}
        )
    ).status_code == 400


@pytest.mark.parametrize(
    "claims",
    [
        {"username": "admin", "token_type": "plugin_page_asset"},
        {"username": "api_key:test"},
        {"username": "admin", "exp": 1},
    ],
)
async def test_manual_deletion_rejects_non_dashboard_credentials(web_client, claims):
    env, client = web_client
    transfers = env.plugin.transfers
    job = transfers._new_job("admin", "export")
    job["status"] = "completed"
    token = jwt.encode(
        {"exp": time.time() + 3600, **claims},
        env.context.get_config()["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    response = await client.post(
        endpoint("delete-job"),
        headers={"Authorization": "Bearer " + token},
        json={"csrf": "untrusted", "id": job["id"], "acknowledged": True},
    )
    assert response.status_code in {401, 403}, response.text
    assert job["id"] in transfers.jobs


@pytest.mark.parametrize("action", ["export-preview", "export"])
@pytest.mark.parametrize("selections", [None, {}, [], [{"directory": "../outside"}]])
async def test_invalid_current_export_selection_does_not_fall_back_to_rule(
    web_client, action, selections
):
    env, client = web_client
    target = env.make_transfer_plugin()
    transfers = env.plugin.transfers
    saved = await transfers.save_rule(
        [{"directory": target.root_dir_name, "config": False, "data": False}]
    )
    state = (await client.get(endpoint("state"))).json()["data"]
    body = {"csrf": state["csrf"], "selections": selections}
    if action == "export":
        body["acknowledged"] = True
    response = await client.post(endpoint(action), json=body)
    assert response.status_code == 400, response.text
    assert not transfers.jobs
    assert transfers.rule() == saved


@pytest.mark.parametrize(
    "claims",
    [
        {"username": "admin", "token_type": "plugin_page_asset"},
        {"username": "api_key:test"},
        {"username": "admin", "exp": 1},
    ],
)
@pytest.mark.parametrize("action", ["state", "statistics"])
async def test_asset_api_key_and_expired_credentials_cannot_access_files(
    web_client, claims, action
):
    env, client = web_client
    payload = {"exp": time.time() + 3600, **claims}
    token = jwt.encode(
        payload, env.context.get_config()["dashboard"]["jwt_secret"], algorithm="HS256"
    )
    response = await client.get(
        endpoint(action), headers={"Authorization": "Bearer " + token}
    )
    assert response.status_code in {401, 403}, response.text


@pytest.mark.parametrize("token_in_path", [False, True])
@pytest.mark.parametrize("icon_state", ["present", "missing", "unavailable"])
async def test_upload_preflight_and_private_downloads_are_owner_bound(
    web_client, token_in_path, icon_state, monkeypatch
):
    env, client = web_client
    target = env.make_transfer_plugin()
    logo = Path(env.manager.plugin_store_path) / target.root_dir_name / "logo.png"
    icon = b"\x89PNG\r\n\x1a\npackage icon fixture"
    if icon_state != "missing":
        logo.write_bytes(icon)
    transfers = env.plugin.transfers
    rule = await transfers.save_rule(
        [{"directory": target.root_dir_name, "config": False, "data": False}]
    )
    started = await transfers.start_export("admin", rule["updated_at"], True)
    await transfers.task
    state = (await client.get(endpoint("state"))).json()["data"]
    downloaded = await client.get(
        endpoint("download"), params={"id": started["id"], "kind": "package"}
    )
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"].startswith("application/zip")
    assert downloaded.headers["cache-control"] == "no-store"
    if icon_state == "unavailable":
        monkeypatch.setattr(
            env.plugin.service.web, "get_plugin_logo_token", AsyncMock(return_value=None)
        )
    elif icon_state == "present":
        logo.write_bytes(b"\x89PNG\r\n\x1a\ncurrent instance icon")
    uploaded = await client.post(
        endpoint("upload/" + state["csrf"] if token_in_path else "upload"),
        params={} if token_in_path else {"csrf": state["csrf"]},
        files={"file": ("plugins.zip", downloaded.content, "application/zip")},
    )
    assert uploaded.status_code == 200, uploaded.text
    preview = uploaded.json()["data"]
    entry = preview["plugins"][0]
    assert entry["name"] == target.name
    assert str(logo) not in uploaded.text
    assert "checked/payload" not in uploaded.text
    assert "logo" not in transfers.get_job(preview["job"]["id"], "admin")["plan"][0]
    if icon_state == "present":
        assert entry["logo"].startswith("/api/file/")
        image = await client.get(entry["logo"], headers={"Authorization": ""})
        assert image.status_code == 200 and image.content == icon
        assert image.headers["content-type"].startswith("image/png")
        assert (await client.get(entry["logo"])).status_code == 404
    else:
        assert entry["logo"] is None
    env.manager.load.assert_not_awaited()
    other = jwt.encode(
        {"username": "other", "exp": time.time() + 3600},
        env.context.get_config()["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    denied = await client.get(
        endpoint("download"),
        params={"id": started["id"], "kind": "package"},
        headers={"Authorization": "Bearer " + other},
    )
    assert denied.status_code == 400
    assert "无权访问" in denied.json()["message"]


async def test_upload_without_confirmation_does_not_create_jobs(web_client):
    env, client = web_client
    response = await client.post(
        endpoint("upload"), files={"file": ("unsafe.zip", b"unsafe", "application/zip")}
    )
    assert response.status_code == 403
    assert env.plugin.transfers.jobs == {}


async def test_malformed_upload_returns_400_and_removes_staging(web_client):
    env, client = web_client
    state = (await client.get(endpoint("state"))).json()["data"]
    response = await client.post(
        endpoint("upload"),
        params={"csrf": state["csrf"]},
        files={"file": ("bad.zip", b"invalid ZIP", "application/zip")},
    )
    assert response.status_code == 400
    assert "ZIP" in response.json()["message"]
    job = next(iter(env.plugin.transfers.jobs.values()))
    assert job["status"] == "failed"
    directory = env.plugin.transfers.job_directory(job["id"])
    assert not (directory / "upload.zip").exists()
    assert not (directory / "checked").exists()


def management_endpoint(action):
    return "/api/v1/plugins/extensions/astrbot_plugin_manager/manage/" + action


async def test_manual_update_routes_auth_csrf_owner_and_job(web_client, monkeypatch):
    env, client = web_client
    target = env.make_transfer_plugin(active=False)
    market_id = f"{target.author}/{target.name}"
    env.prefs["plugin_install_sources"] = {
        target.root_dir_name: {
            "root_dir_name": target.root_dir_name,
            "name": target.name,
            "author": target.author,
            "install_method": "market",
            "registry_url": None,
            "market_plugin_id": market_id,
            "repo": "https://github.com/test/demo",
        }
    }
    remote = {
        "name": target.name,
        "author": target.author,
        "version": "2.0.0",
        "repo": "https://github.com/test/demo",
    }
    monkeypatch.setattr(
        env.plugin.service.web,
        "get_online_plugins",
        AsyncMock(return_value=({market_id: remote}, None)),
    )
    initial = (await client.get(management_endpoint("state"))).json()["data"]
    assert initial["updates"]["status"] == "idle"
    token = initial["csrf"]
    for action in ("check-updates", "update-plugin"):
        denied = await client.post(management_endpoint(action), json={"csrf": "wrong"})
        assert denied.status_code == 403
    assert not env.plugin.updates.task
    invalid = await client.post(
        management_endpoint("check-updates"),
        json={"csrf": token, "directories": [target.root_dir_name]},
    )
    assert invalid.status_code == 400
    started = await client.post(
        management_endpoint("check-updates"), json={"csrf": token}
    )
    assert started.status_code == 200, started.text
    await env.plugin.updates.task
    checked = await client.get(management_endpoint("updates"))
    assert checked.status_code == 200, checked.text
    report = checked.json()["data"]
    assert report["status"] == "completed"
    assert report["results"][0]["can_update"], report["results"][0]["message"]
    other_token = jwt.encode(
        {"username": "other", "exp": time.time() + 3600},
        env.context.get_config()["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    other = await client.get(
        management_endpoint("updates"),
        headers={"Authorization": "Bearer " + other_token},
    )
    assert other.json()["data"]["status"] == "idle"
    entry = report["results"][0]
    body = {
        "csrf": token,
        "check_id": report["id"],
        "directory": entry["directory"],
        "binding_id": entry["binding_id"],
        "acknowledged": False,
    }
    unconfirmed = await client.post(management_endpoint("update-plugin"), json=body)
    assert unconfirmed.status_code == 400 and not env.plugin.transfers.jobs

    async def update(captured, **kwargs):
        target.version = "2.0.0"
        path = (
            Path(env.manager.plugin_store_path) / target.root_dir_name / "metadata.yaml"
        )
        metadata = yaml.safe_load(path.read_text(encoding="utf-8"))
        metadata["version"] = target.version
        path.write_text(yaml.safe_dump(metadata), encoding="utf-8")
        return target

    monkeypatch.setattr(
        env.plugin.service, "update_target", AsyncMock(side_effect=update)
    )
    body["acknowledged"] = True
    result = await client.post(management_endpoint("update-plugin"), json=body)
    assert result.status_code == 200, result.text
    assert result.json()["data"]["kind"] == "plugin_update"
    assert "update_plan" not in result.text
    await env.plugin.transfers.task
    final = (await client.get(management_endpoint("state"))).json()["data"]
    assert final["jobs"][0]["status"] == "completed"
    assert final["updates"]["results"][0]["status"] == "updated"
    assert not target.activated
    client.headers.pop("Authorization")
    for action in ("state", "updates"):
        assert (await client.get(management_endpoint(action))).status_code in (401, 403)


async def test_metadata_sync_uses_csrf_and_only_updates_manager_records(web_client):
    env, client = web_client
    target = env.make_transfer_plugin(active=True)
    old_author = target.author
    await env.plugin.sources.detail(target.root_dir_name)
    path = env.plugin.transfers.code_root / target.root_dir_name / "metadata.yaml"
    metadata = yaml.safe_load(path.read_text(encoding="utf-8"))
    metadata["author"] = "New Author"
    path.write_text(yaml.safe_dump(metadata), encoding="utf-8")
    state = (await client.get(management_endpoint("state"))).json()["data"]
    record = next(
        item
        for item in state["plugins"]
        if item["local"]["directory"] == target.root_dir_name
    )
    assert record["metadata_changed"]
    plugins = (await client.get(endpoint("state"))).json()["data"]["plugins"]
    plugin = next(item for item in plugins if item["directory"] == target.root_dir_name)
    assert plugin["status"] == "enabled" and not plugin["blocked"]
    body = {
        "csrf": "wrong",
        "directory": target.root_dir_name,
        "revision": record["revision"],
    }
    assert (
        await client.post(management_endpoint("metadata-sync"), json=body)
    ).status_code == 403
    body.update(csrf=state["csrf"], revision="stale")
    assert (
        await client.post(management_endpoint("metadata-sync"), json=body)
    ).status_code == 400
    body["revision"] = record["revision"]
    response = await client.post(management_endpoint("metadata-sync"), json=body)
    assert response.status_code == 200, response.text
    assert not response.json()["data"]["metadata_changed"]
    assert target.author == old_author and target.activated
    env.manager.load.assert_not_awaited()
    env.manager._terminate_plugin.assert_not_awaited()
    assert not env.plugin.transfers.jobs


async def test_management_binding_requires_csrf_confirmation_and_authentication(
    web_client, monkeypatch
):
    env, client = web_client
    target = env.make_transfer_plugin()
    state = (await client.get(management_endpoint("state"))).json()["data"]
    assert state["plugins"]
    inspect = AsyncMock(return_value={"name": target.name, "author": target.author})
    monkeypatch.setattr(
        env.manager, "inspect_plugin_repository", inspect, raising=False
    )
    denied = await client.post(
        management_endpoint("source-preview"),
        json={
            "csrf": "wrong",
            "directory": target.root_dir_name,
            "kind": "repository",
            "value": "https://github.com/test/demo",
        },
    )
    assert denied.status_code == 403
    inspect.assert_not_awaited()
    checked = await client.post(
        management_endpoint("source-preview"),
        json={
            "csrf": state["csrf"],
            "directory": target.root_dir_name,
            "kind": "repository",
            "value": "https://github.com/test/demo",
        },
    )
    assert checked.status_code == 200, checked.text
    identifier = checked.json()["data"]["id"]
    rejected = await client.post(
        management_endpoint("source-bind"),
        json={
            "csrf": state["csrf"],
            "id": identifier,
            "activate": True,
            "acknowledged": False,
        },
    )
    assert rejected.status_code == 400
    saved = await client.post(
        management_endpoint("source-bind"),
        json={
            "csrf": state["csrf"],
            "id": identifier,
            "activate": True,
            "acknowledged": True,
        },
    )
    assert saved.status_code == 200, saved.text
    assert (
        env.prefs["plugin_install_sources"][target.root_dir_name]["install_method"]
        == "repository"
    )
    for claims in (
        {"username": "admin", "token_type": "plugin_page_asset"},
        {"username": "api_key:test"},
        {"username": "admin", "exp": 1},
    ):
        token = jwt.encode(
            {"exp": time.time() + 3600, **claims},
            env.context.get_config()["dashboard"]["jwt_secret"],
            algorithm="HS256",
        )
        forbidden = await client.get(
            management_endpoint("state"), headers={"Authorization": "Bearer " + token}
        )
        assert forbidden.status_code in {401, 403}


@pytest.mark.parametrize("token_in_path", [False, True])
async def test_management_manifest_export_download_and_upload_are_private(
    web_client, monkeypatch, token_in_path
):
    env, client = web_client
    target = env.make_transfer_plugin()
    identifier = f"{target.author}/{target.name}"
    monkeypatch.setattr(
        "astrbot_plugin_manager.manifests.download_repository_archive",
        AsyncMock(side_effect=ValueError("仓库暂不可用")),
    )
    env.prefs["plugin_install_sources"] = {
        target.root_dir_name: {
            "install_method": "market",
            "market_plugin_id": identifier,
            "registry_url": None,
            "repo": "https://github.com/test/demo",
        }
    }
    monkeypatch.setattr(
        env.plugin.service.web,
        "get_online_plugins",
        AsyncMock(
            return_value=(
                {
                    identifier: {
                        "name": target.name,
                        "author": target.author,
                        "version": target.version,
                        "repo": "https://github.com/test/demo",
                        "download_url": "https://cloud.astrbot.app/demo.zip",
                    }
                },
                None,
            )
        ),
    )
    state = (await client.get(management_endpoint("state"))).json()["data"]
    exported = await client.post(
        management_endpoint("manifest-export"),
        json={
            "csrf": state["csrf"],
            "directories": [target.root_dir_name],
            "name": "生产环境备份",
        },
    )
    assert exported.status_code == 200, exported.text
    job = exported.json()["data"]
    assert job["name"] == "生产环境备份"
    assert job["download_filename"] == "生产环境备份.json"
    env.plugin.transfers.get_job(job["id"], "admin")["created_at"] = (
        time.time() - 365 * 86400
    )
    downloaded = await client.get(
        management_endpoint("manifest-download"), params={"id": job["id"]}
    )
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"].startswith("application/json")
    assert downloaded.headers["cache-control"] == "no-store"
    assert "生产环境备份.json" in unquote(downloaded.headers["content-disposition"])
    manifest = downloaded.json()
    assert manifest["name"] == "生产环境备份"
    assert manifest["schema_version"] == 1
    assert manifest["plugins"][0]["market_plugin_id"] == identifier
    assert "download_url" not in downloaded.text
    other = jwt.encode(
        {"username": "other", "exp": time.time() + 3600},
        env.context.get_config()["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    denied = await client.get(
        management_endpoint("manifest-download"),
        params={"id": job["id"]},
        headers={"Authorization": "Bearer " + other},
    )
    assert denied.status_code == 400
    uploaded = await client.post(
        management_endpoint(
            "manifest-upload/" + state["csrf"] if token_in_path else "manifest-upload"
        ),
        params={} if token_in_path else {"csrf": state["csrf"]},
        files={"file": ("manifest.json", downloaded.content, "application/json")},
    )
    assert uploaded.status_code == 200, uploaded.text
    pending = uploaded.json()["data"]
    assert pending["ready"] is False
    assert pending["job"]["name"] == "生产环境备份"
    await env.plugin.transfers.task
    checked = await client.get(
        management_endpoint("manifest-preview"), params={"id": pending["job"]["id"]}
    )
    assert checked.status_code == 200, checked.text
    assert checked.json()["data"]["job"]["name"] == "生产环境备份"
    assert checked.json()["data"]["plugins"][0]["sources"][0]["status"] == "skip"
    env.manager.load.assert_not_awaited()
    env.manager.install_plugin.assert_not_awaited()
    duplicated = json.dumps(manifest).replace(
        '"schema_version": 1', '"schema_version": 1, "schema_version": 1'
    )
    rejected = await client.post(
        management_endpoint("manifest-upload"),
        params={"csrf": state["csrf"]},
        files={"file": ("manifest.json", duplicated.encode(), "application/json")},
    )
    assert rejected.status_code == 400
    assert "重复" in rejected.json()["message"]


@pytest.mark.parametrize(
    "section,action", [("transfer", "upload"), ("manage", "manifest-upload")]
)
async def test_native_bridge_upload_path_keeps_nonce_and_auth_checks(
    web_client, section, action
):
    env, client = web_client
    base = f"/api/v1/plugins/extensions/astrbot_plugin_manager/{section}/"
    state = (await client.get(base + "state")).json()["data"]
    files = {"file": ("test.json", b"{}", "application/json")}
    denied = await client.post(
        base + action + "/wrong", params={"csrf": state["csrf"]}, files=files
    )
    assert denied.status_code == 403
    for claims in (
        {"username": "other"},
        {"username": "admin", "token_type": "plugin_page_asset"},
        {"username": "api_key:test"},
        {"username": "admin", "exp": 1},
    ):
        token = jwt.encode(
            {"exp": time.time() + 3600, **claims},
            env.context.get_config()["dashboard"]["jwt_secret"],
            algorithm="HS256",
        )
        denied = await client.post(
            base + action + "/" + state["csrf"],
            files=files,
            headers={"Authorization": "Bearer " + token},
        )
        assert denied.status_code in {401, 403}, denied.text
    assert not env.plugin.transfers.jobs
