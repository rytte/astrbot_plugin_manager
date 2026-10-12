"""Source and manifest endpoints using the same dashboard-only auth boundary."""

from __future__ import annotations

import asyncio
import functools
import json
import logging

from astrbot.api.web import error_response, file_response, json_response, request
from astrbot.dashboard.services.plugin_service import (
    PluginServiceError,
    PluginServiceWarning,
)

from .export_names import export_filename
from .redaction import redact
from .transfer_bundle import (
    MAX_JSON,
    _invalid_number,
    _json_pairs,
    exact_keys,
    safe_path,
)
from .transfer_web import TransferWeb


class ManagementWeb(TransferWeb):
    def __init__(
        self, context, transfers, sources, manifests, updates, loading
    ) -> None:
        super().__init__(context, transfers)
        self.sources = sources
        self.manifests = manifests
        self.updates = updates
        self.loading = loading

    def register(self) -> None:
        for action, method in {
            "state": "GET",
            "source-preview": "POST",
            "source-bind": "POST",
            "source-revalidate": "POST",
            "metadata-sync": "POST",
            "check-updates": "POST",
            "updates": "GET",
            "update-plugin": "POST",
            "load-plugin": "POST",
            "manifest-export": "POST",
            "manifest-download": "GET",
            "manifest-upload": "POST",
            "manifest-upload/<csrf_token>": "POST",
            "manifest-preview": "GET",
            "manifest-draft": "POST",
            "manifest-recheck": "POST",
            "manifest-install": "POST",
        }.items():
            handler = functools.partial(self.dispatch, action.split("/")[0])
            self.handlers.append(handler)
            self.context.register_web_api(
                f"/astrbot_plugin_manager/manage/{action}",
                handler,
                [method],
                "插件来源与清单（WebUI 管理员）",
            )

    async def dispatch(self, action: str, csrf_token: str | None = None):
        try:
            owner = self._authorize()
            if action == "state":
                data = {
                    "csrf": self._nonce(owner),
                    "plugins": redact(await self.sources.inventory()),
                    "jobs": self.transfers.history(owner),
                    "busy": self.transfers.busy,
                    "updates": self.updates.state(owner),
                }
            elif action == "updates":
                data = self.updates.state(owner)
            elif action == "manifest-preview":
                data = await self.manifests.preview_with_logos(
                    owner, request.query.get("id")
                )
            elif action == "manifest-download":
                job = self.transfers.get_job(request.query.get("id"), owner)
                if (
                    job["kind"] != "manifest_export"
                    or job["status"] != "completed"
                    or self.transfers._expired(job)
                ):
                    raise ValueError("清单文件未就绪或已被旧版清理，请重新导出。")
                return file_response(
                    safe_path(
                        self.transfers.job_directory(job["id"]),
                        "plugins.astrbot-plugin-manifest.json",
                    ),
                    filename=export_filename(job, "manifest"),
                    content_type="application/json",
                    headers={
                        "Cache-Control": "no-store",
                        "X-Content-Type-Options": "nosniff",
                    },
                )
            elif action == "manifest-upload":
                self._check_nonce(
                    owner,
                    csrf_token if csrf_token is not None else request.query.get("csrf"),
                )
                length = request.headers.get("Content-Length")
                if length and (not length.isdigit() or int(length) > MAX_JSON + 65536):
                    raise ValueError("上传请求超过 8 MiB 限制。")
                files = await request.files()
                if set(files.keys()) != {"file"} or len(files.getlist("file")) != 1:
                    raise ValueError("只允许上传一个清单 JSON 文件。")
                uploaded = files["file"]
                try:
                    raw = await uploaded.read(MAX_JSON + 1)
                    if len(raw) > MAX_JSON:
                        raise ValueError("插件清单超过 8 MiB 限制。")
                    value = json.loads(
                        raw.decode("utf-8-sig"),
                        object_pairs_hook=_json_pairs,
                        parse_constant=_invalid_number,
                    )
                    data = self.manifests.begin_preview(owner, value)
                finally:
                    await uploaded.close()
            else:
                body = await request.json()
                if not isinstance(body, dict):
                    raise ValueError("请求必须是 JSON 对象。")
                self._check_nonce(owner, body.get("csrf"))
                if action == "check-updates":
                    exact_keys(body, {"csrf"})
                    data = await self.updates.start_check(owner)
                elif action == "load-plugin":
                    exact_keys(body, {"csrf", "directory", "revision", "acknowledged"})
                    data = await self.loading.load(
                        body["directory"], body["revision"], body["acknowledged"]
                    )
                elif action == "update-plugin":
                    exact_keys(
                        body,
                        {"csrf", "check_id", "directory", "binding_id", "acknowledged"},
                    )
                    data = await self.updates.start_update(
                        owner,
                        body["check_id"],
                        body["directory"],
                        body["binding_id"],
                        body["acknowledged"],
                    )
                elif action == "metadata-sync":
                    exact_keys(body, {"csrf", "directory", "revision"})
                    data = redact(
                        await self.sources.sync_metadata(
                            owner, body["directory"], body["revision"]
                        )
                    )
                elif action == "source-preview":
                    exact_keys(body, {"csrf", "directory", "kind", "value"})
                    data = await self.sources.preview_binding(
                        owner, body["directory"], body["kind"], body["value"]
                    )
                elif action == "source-bind":
                    exact_keys(body, {"csrf", "id", "acknowledged"})
                    data = redact(
                        await self.sources.bind(owner, body["id"], body["acknowledged"])
                    )
                elif action == "source-revalidate":
                    exact_keys(body, {"csrf", "directory", "revision"})
                    data = await self.sources.preview_existing(
                        owner, body["directory"], body["revision"]
                    )
                elif action == "manifest-export":
                    exact_keys(body, {"csrf", "directories", "name"})
                    self.transfers._ensure_idle()
                    exported = await self.manifests.export(
                        body["directories"], name=body["name"]
                    )
                    async with self.transfers.operation_lock:
                        job = self.transfers._new_job(owner, "manifest_export")
                        job["name"] = exported["manifest"]["name"]
                        path = safe_path(
                            self.transfers.job_directory(job["id"]),
                            "plugins.astrbot-plugin-manifest.json",
                        )
                        await self.manifests.download_export(path, exported["manifest"])
                        job.update(status="completed", phase="清单已导出")
                        if exported["notice"]:
                            job["warnings"].append(exported["notice"])
                        self.transfers._save(job)
                        data = self.transfers.public_job(job)
                elif action == "manifest-install":
                    exact_keys(body, {"csrf", "id", "selections", "acknowledged"})
                    data = await self.manifests.start(
                        owner, body["id"], body["selections"], body["acknowledged"]
                    )
                elif action == "manifest-draft":
                    exact_keys(body, {"csrf", "id", "draft"})
                    data = self.manifests.save_draft(owner, body["id"], body["draft"])
                elif action == "manifest-recheck":
                    exact_keys(body, {"csrf", "id"})
                    data = self.manifests.recheck(owner, body["id"])
                else:
                    raise ValueError("未知插件管理接口。")
            return json_response(
                {"status": "ok", "data": data}, headers={"Cache-Control": "no-store"}
            )
        except PermissionError as exc:
            return error_response(str(exc), status_code=403)
        except (
            ValueError,
            TypeError,
            KeyError,
            OSError,
            UnicodeError,
            RecursionError,
        ) as exc:
            return error_response(
                redact(str(exc))
                if isinstance(exc, ValueError)
                else "请求格式或存储无效，请检查输入及日志。",
                status_code=400,
            )
        except (
            PluginServiceError,
            PluginServiceWarning,
        ) as exc:
            return error_response(redact(exc.public_message), status_code=400)
        except asyncio.TimeoutError:
            return error_response("来源校验超时，请稍后重试。", status_code=400)
        except Exception:
            logging.getLogger("astrbot").error(
                "Plugin management request failed: %s", action
            )
            return error_response(
                "插件管理请求失败，请检查服务端日志。", status_code=500
            )

    async def close(self) -> None:
        self.closed = True
        await self.loading.close()
        await self.updates.close()
        self.context.registered_web_apis[:] = [
            entry
            for entry in self.context.registered_web_apis
            if entry[1] not in self.handlers
        ]
        self.nonces.clear()
        self.sources.previews.clear()
