"""Dashboard-JWT-only plugin APIs with user-bound CSRF and private downloads."""

from __future__ import annotations

import asyncio
import functools
import hmac
import secrets
import time

import jwt
from astrbot.api.web import error_response, file_response, json_response, request
from astrbot.core.workspace import API_KEY_USERNAME_PREFIX

from .export_names import export_filename
from .transfer_bundle import MAX_ARCHIVE, exact_keys, remove_path, safe_path


class TransferWeb:
    def __init__(self, context, transfers) -> None:
        self.context = context
        self.transfers = transfers
        self.nonces = {}
        self.handlers = []
        self.closed = False

    def register(self) -> None:
        routes = {
            "state": "GET",
            "statistics": "GET",
            "jobs": "GET",
            "job": "GET",
            "download": "GET",
            "rule": "POST",
            "export-preview": "POST",
            "export": "POST",
            "upload": "POST",
            "upload/<csrf_token>": "POST",
            "import": "POST",
            "delete-job": "POST",
            "rollback-preview": "POST",
            "rollback-plan": "GET",
            "rollback": "POST",
        }
        for action, method in routes.items():
            handler = functools.partial(self.dispatch, action.split("/")[0])
            self.handlers.append(handler)
            self.context.register_web_api(
                f"/astrbot_plugin_manager/transfer/{action}",
                handler,
                [method],
                "插件备份与迁移（WebUI 管理员）",
            )

    def _authorize(self) -> str:
        if self.closed:
            raise PermissionError("插件页面正在关闭。")
        username = request.username
        if (
            not isinstance(username, str)
            or not username
            or username.startswith(API_KEY_USERNAME_PREFIX)
        ):
            raise PermissionError("仅限已登录的 WebUI 管理员，不接受 API Key。")
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise PermissionError("请通过已登录的 WebUI 插件页面访问。")
        secret = self.context.get_config().get("dashboard", {}).get("jwt_secret")
        if not isinstance(secret, str) or not secret:
            raise PermissionError("WebUI JWT 密钥未配置，拒绝访问。")
        try:
            payload = jwt.decode(
                authorization.removeprefix("Bearer ").strip(),
                secret,
                algorithms=["HS256"],
                options={"require": ["exp", "username"]},
            )
        except jwt.InvalidTokenError as exc:
            raise PermissionError("WebUI 登录凭据无效或已过期。") from exc
        if payload["username"] != username or payload.get("token_type") is not None:
            raise PermissionError("页面资源凭据或身份不匹配，不能执行插件迁移。")
        return username

    def _nonce(self, owner: str) -> str:
        current = self.nonces.get(owner)
        if not current or current[1] <= time.time():
            current = (secrets.token_urlsafe(32), time.time() + 3600)
            self.nonces[owner] = current
        return current[0]

    def _check_nonce(self, owner: str, token) -> None:
        current = self.nonces.get(owner)
        if (
            not current
            or current[1] <= time.time()
            or not isinstance(token, str)
            or not hmac.compare_digest(current[0], token)
        ):
            raise PermissionError("操作确认凭据已过期或无效，请刷新页面后重试。")

    async def dispatch(self, action: str, csrf_token: str | None = None):
        """Authorize every handler independently of frontend or framework routing."""
        try:
            owner = self._authorize()
            if action == "state":
                plugins = await self.transfers.inventory()
                for plugin in plugins:
                    plugin["logo"] = None
                    if plugin["blocked"]:
                        continue
                    try:
                        path = safe_path(
                            self.transfers.code_root, f"{plugin['directory']}/logo.png"
                        )
                        if path.is_file() and path.stat().st_nlink == 1:
                            token = await self.transfers.operations.web.get_plugin_logo_token(
                                str(path)
                            )
                            if token:
                                plugin["logo"] = f"/api/file/{token}"
                    except (ValueError, OSError):
                        pass
                data = {
                    "csrf": self._nonce(owner),
                    "plugins": plugins,
                    "rule": self.transfers.rule(),
                    "jobs": self.transfers.history(owner),
                    "limits": self.transfers.limits(),
                    "busy": self.transfers.busy,
                }
            elif action == "statistics":
                data = {"plugins": await self.transfers.inventory(statistics=True)}
            elif action == "jobs":
                data = {
                    "jobs": self.transfers.history(owner),
                    "busy": self.transfers.busy,
                }
            elif action == "job":
                data = self.transfers.public_job(
                    self.transfers.get_job(request.query.get("id"), owner)
                )
            elif action == "rollback-plan":
                data = self.transfers.recovery.plan(owner, request.query.get("id"))
            elif action == "download":
                kind = request.query.get("kind")
                path = self.transfers.download(request.query.get("id"), owner, kind)
                return file_response(
                    path,
                    filename=export_filename(
                        self.transfers.get_job(request.query.get("id"), owner), kind
                    ),
                    content_type="application/zip",
                    headers={
                        "Cache-Control": "no-store",
                        "X-Content-Type-Options": "nosniff",
                    },
                )
            elif action == "upload":
                self._check_nonce(
                    owner,
                    csrf_token if csrf_token is not None else request.query.get("csrf"),
                )
                data = await self._upload(owner)
            else:
                body = await request.json()
                if not isinstance(body, dict):
                    raise ValueError("请求必须是 JSON 对象。")
                self._check_nonce(owner, body.get("csrf"))
                if action == "rule":
                    exact_keys(body, {"csrf", "selections"})
                    data = await self.transfers.save_rule(body["selections"])
                elif action == "delete-job":
                    exact_keys(body, {"csrf", "id", "acknowledged"})
                    data = await self.transfers.delete_job(
                        body["id"], owner, body["acknowledged"]
                    )
                elif action == "rollback-preview":
                    exact_keys(body, {"csrf", "id"})
                    data = await self.transfers.recovery.preview(owner, body["id"])
                elif action == "rollback":
                    exact_keys(body, {"csrf", "id", "selections", "acknowledged"})
                    data = await self.transfers.recovery.start(
                        owner, body["id"], body["selections"], body["acknowledged"]
                    )
                elif action == "export-preview":
                    if "selections" in body:
                        exact_keys(body, {"csrf", "selections"})
                        if not isinstance(body["selections"], list):
                            raise ValueError("插件选择必须为列表。")
                        data = await self.transfers.export_preview(body["selections"])
                    else:
                        exact_keys(body, {"csrf"})
                        data = await self.transfers.export_preview()
                elif action == "export":
                    if "selections" in body:
                        exact_keys(body, {"csrf", "selections", "acknowledged", "name"})
                        if not isinstance(body["selections"], list):
                            raise ValueError("插件选择必须为列表。")
                        data = await self.transfers.start_export(
                            owner,
                            None,
                            body["acknowledged"],
                            selections=body["selections"],
                            name=body["name"],
                        )
                    else:
                        exact_keys(
                            body, {"csrf", "rule_updated_at", "acknowledged", "name"}
                        )
                        data = await self.transfers.start_export(
                            owner,
                            body["rule_updated_at"],
                            body["acknowledged"],
                            name=body["name"],
                        )
                elif action == "import":
                    exact_keys(body, {"csrf", "id", "selections", "acknowledged"})
                    data = await self.transfers.start_import(
                        body["id"], owner, body["selections"], body["acknowledged"]
                    )
                else:
                    raise ValueError("未知插件迁移接口。")
            return json_response(
                {"status": "ok", "data": data}, headers={"Cache-Control": "no-store"}
            )
        except PermissionError as exc:
            return error_response(str(exc), status_code=403)
        except (
            ValueError,
            OSError,
            KeyError,
            RecursionError,
            jwt.InvalidTokenError,
        ) as exc:
            message = (
                str(exc)
                if isinstance(exc, ValueError)
                else "请求或存储格式无效，请检查文件和服务端日志。"
            )
            return error_response(message, status_code=400)
        except Exception:
            return error_response(
                "任务请求失败，请检查 AstrBot 日志。", status_code=500
            )

    async def _upload(self, owner: str) -> dict:
        length = request.headers.get("Content-Length")
        if length and (not length.isdigit() or int(length) > MAX_ARCHIVE + 65536):
            raise ValueError("上传请求超过 256 MiB 限制。")
        files = await request.files()
        if set(files.keys()) != {"file"} or len(files.getlist("file")) != 1:
            raise ValueError("只允许上传一个 file 文件。")
        uploaded = files["file"]
        job = self.transfers.new_upload(owner)
        directory = self.transfers.job_directory(job["id"])
        try:
            size = 0
            with (directory / "upload.zip").open("xb") as output:
                while True:
                    chunk = await uploaded.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_ARCHIVE:
                        raise ValueError("上传文件超过 256 MiB 限制。")
                    await asyncio.to_thread(output.write, chunk)
            data = await self.transfers.prepare_import(job)
            data["plugins"] = [dict(plugin, logo=None) for plugin in data["plugins"]]
            for plugin in data["plugins"]:
                try:
                    path = safe_path(
                        directory / "checked",
                        f"payload/{plugin['directory']}/code/logo.png",
                    )
                    if path.is_file() and path.stat().st_nlink == 1:
                        token = await self.transfers.operations.web.get_plugin_logo_token(
                            str(path)
                        )
                        if token:
                            plugin["logo"] = f"/api/file/{token}"
                except (ValueError, OSError):
                    pass
            return data
        except Exception:
            job["status"] = "preparing"
            try:
                await asyncio.to_thread(remove_path, directory, "upload.zip")
            finally:
                job.update(status="failed", error="上传或预检失败，正式目录未修改。")
                self.transfers._save(job)
            raise
        finally:
            await uploaded.close()

    async def close(self) -> None:
        self.closed = True
        self.context.registered_web_apis[:] = [
            entry
            for entry in self.context.registered_web_apis
            if entry[1] not in self.handlers
        ]
        self.nonces.clear()
        await self.transfers.close()
