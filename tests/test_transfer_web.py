"""Exercise real FastAPI extension dispatch and dashboard authentication."""

import time
from types import SimpleNamespace

import httpx
import jwt
import pytest
from astrbot.dashboard.api.plugins import router
from astrbot.dashboard.responses import ApiError
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
    app.include_router(router, prefix="/api/v1")
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


@pytest.mark.parametrize(
    "claims",
    [
        {"username": "admin", "token_type": "plugin_page_asset"},
        {"username": "api_key:test"},
        {"username": "admin", "exp": 1},
    ],
)
async def test_asset_api_key_and_expired_credentials_cannot_access_files(
    web_client, claims
):
    env, client = web_client
    payload = {"exp": time.time() + 3600, **claims}
    token = jwt.encode(
        payload, env.context.get_config()["dashboard"]["jwt_secret"], algorithm="HS256"
    )
    response = await client.get(
        endpoint("state"), headers={"Authorization": "Bearer " + token}
    )
    assert response.status_code in {401, 403}, response.text


async def test_upload_preflight_and_private_downloads_are_owner_bound(web_client):
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
        endpoint("upload"),
        params={"csrf": state["csrf"]},
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
