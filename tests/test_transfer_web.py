"""Exercise real FastAPI extension dispatch and dashboard authentication."""

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from astrbot.dashboard.api.files import legacy_router as file_router
from astrbot.dashboard.api.plugins import router
from astrbot.dashboard.responses import ApiError
from astrbot.dashboard.services.file_service import FileService
from fastapi import FastAPI
from fastapi.responses import JSONResponse


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
            json={"csrf": csrf, "selections": snapshot, "acknowledged": acknowledged},
        )
        assert response.status_code == expected
        assert not transfers.jobs
    response = await client.post(
        endpoint("export"),
        json={"csrf": state["csrf"], "selections": snapshot, "acknowledged": True},
    )
    assert response.status_code == 200, response.text
    await transfers.task
    assert transfers.get_job(response.json()["data"]["id"], "admin")["status"] == (
        "completed"
    )
    assert not transfers.rule_path.exists()


@pytest.mark.parametrize("action", ["export-preview", "export"])
@pytest.mark.parametrize(
    "selections", [None, {}, [], [{"directory": "../outside"}]]
)
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
async def test_upload_preflight_and_private_downloads_are_owner_bound(
    web_client, token_in_path
):
    env, client = web_client
    target = env.make_transfer_plugin()
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
    uploaded = await client.post(
        endpoint("upload/" + state["csrf"] if token_in_path else "upload"),
        params={} if token_in_path else {"csrf": state["csrf"]},
        files={"file": ("plugins.zip", downloaded.content, "application/zip")},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["data"]["plugins"][0]["name"] == target.name
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
        json={"csrf": state["csrf"], "directories": [target.root_dir_name]},
    )
    assert exported.status_code == 200, exported.text
    job = exported.json()["data"]
    downloaded = await client.get(
        management_endpoint("manifest-download"), params={"id": job["id"]}
    )
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"].startswith("application/json")
    assert downloaded.headers["cache-control"] == "no-store"
    manifest = downloaded.json()
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
    assert uploaded.json()["data"]["plugins"][0]["status"] == "skip"
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
