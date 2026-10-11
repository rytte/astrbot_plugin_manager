"""Official-market-only sharing manifests and persistent, disabled installs."""

from __future__ import annotations

import asyncio
import copy
import json
import time

from astrbot.core.config.default import VERSION

from .market_archive import download_market_archive, unpack_market_archive
from .redaction import redact
from .service import is_market_id
from .sources import fingerprint, now, repository_url
from .transfer_bundle import (
    MAX_CONTENT,
    MAX_JSON,
    check_environment,
    component,
    copy_tree,
    digest,
    exact_keys,
    read_metadata,
    remove_path,
    safe_path,
    write_json,
)
from .transfers import PREVIEW_TTL

MANIFEST_FORMAT = "astrbot-plugin-manager-manifest"


def validate_manifest(value) -> dict:
    exact_keys(value, {"format", "schema_version", "created_at", "plugins"})
    if (
        value["format"] != MANIFEST_FORMAT
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
    ):
        raise ValueError("插件清单格式或版本不受支持。")
    if not isinstance(value["created_at"], str) or len(value["created_at"]) > 64:
        raise ValueError("插件清单创建时间无效。")
    if not isinstance(value["plugins"], list) or not 1 <= len(value["plugins"]) <= 256:
        raise ValueError("插件清单须包含 1～256 个插件。")
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_JSON:
        raise ValueError("插件清单超过 8 MiB 限制。")
    seen = set()
    for entry in value["plugins"]:
        exact_keys(
            entry,
            {
                "market_plugin_id",
                "name",
                "author",
                "observed_version",
                "version_policy",
                "registry",
            },
        )
        if any(
            not isinstance(entry[field], str) or len(entry[field]) > 512
            for field in entry
        ):
            raise ValueError("插件清单字段须是有限长度字符串。")
        if entry["registry"] != "official" or entry["version_policy"] != "latest":
            raise ValueError("第一版清单只支持官方市场和 latest 版本策略。")
        identifier = entry["market_plugin_id"]
        if (
            not is_market_id(identifier)
            or identifier != f"{entry['author']}/{entry['name']}"
            or identifier.casefold() in seen
        ):
            raise ValueError("市场 ID 与插件身份不一致或重复。")
        component(entry["name"], module=True)
        seen.add(identifier.casefold())
    return copy.deepcopy(value)


class ManifestService:
    def __init__(self, sources, transfers) -> None:
        self.sources = sources
        self.transfers = transfers
        self.operations = sources.operations

    def _remote(self, data: dict, entry: dict) -> dict:
        metadata = self.operations.web.resolve_market_plugin_entry_by_id(
            data, entry["market_plugin_id"]
        )
        if not metadata or any(
            metadata.get(field) != entry[field] for field in ("name", "author")
        ):
            raise ValueError("官方市场中缺少该 ID，或作者/插件名已变化。")
        component(metadata["name"], module=True)
        remote = {
            field: str(metadata.get(field) or "")
            for field in (
                "name",
                "author",
                "version",
                "repo",
                "download_url",
                "commit_sha",
                "astrbot_version",
            )
        }
        remote["repo"] = repository_url(remote["repo"])
        if not remote["download_url"]:
            raise ValueError("官方市场未提供安装包，第一版不回退到仓库下载。")
        return remote

    async def export(self, directories: list[str]) -> dict:
        if (
            not isinstance(directories, list)
            or not 1 <= len(directories) <= 256
            or len(set(directories)) != len(directories)
        ):
            raise ValueError("请选择 1～256 个不重复插件。")
        records = {
            record["local"]["directory"]: record
            for record in await self.sources.inventory()
        }
        data, notice = await self.sources.official_market()
        entries = []
        for directory in directories:
            record = records.get(directory)
            if not record:
                raise ValueError("所选插件已不存在，请刷新。")
            self.sources._editable(record)
            binding = next(
                (
                    binding
                    for binding in record["bindings"]
                    if binding["kind"] == "market"
                    and binding["registry_url"] is None
                    and binding.get("verification") != "metadata_changed"
                ),
                None,
            )
            if not binding:
                raise ValueError(
                    f"{directory} 尚未关联有效的官方市场 ID，请先校验并绑定。"
                )
            local = record["local"]
            entry = {
                "market_plugin_id": binding["market_plugin_id"],
                "name": local["name"],
                "author": local["author"],
                "observed_version": local["version"],
                "version_policy": "latest",
                "registry": "official",
            }
            self._remote(data, entry)
            entries.append(entry)
        manifest = validate_manifest(
            {
                "format": MANIFEST_FORMAT,
                "schema_version": 1,
                "created_at": now(),
                "plugins": entries,
            }
        )
        return {"manifest": manifest, "notice": notice}

    async def _plan(self, manifest: dict, data: dict) -> list[dict]:
        locals_ = [record["local"] for record in await self.sources.inventory()]
        plan = []
        for entry in manifest["plugins"]:
            item = {
                **entry,
                "status": "install",
                "problem": "",
                "directory": entry["name"],
            }
            try:
                remote = self._remote(data, entry)
                item.update(remote=remote, version=remote["version"])
                matches = [
                    local
                    for local in locals_
                    if local["name"].casefold() == entry["name"].casefold()
                    or local["directory"].casefold() == entry["name"].casefold()
                ]
                if matches:
                    if (
                        len(matches) == 1
                        and all(
                            matches[0][field] == entry[field]
                            for field in ("name", "author")
                        )
                        and not matches[0]["problem"]
                    ):
                        item.update(
                            status="skip",
                            directory=matches[0]["directory"],
                            problem="本机已有同身份插件，将跳过，不修改版本、来源或启停状态。",
                        )
                    else:
                        raise ValueError("本机存在同名或同目录的不同身份插件。")
                elif safe_path(self.sources.root, entry["name"]).exists():
                    raise ValueError("安装目标目录已存在，拒绝覆盖。")
            except ValueError as exc:
                item.update(status="blocked", problem=redact(str(exc)))
            plan.append(item)
        return plan

    async def preview(self, owner: str, value: dict) -> dict:
        manifest = validate_manifest(value)
        self.operations.ensure_writable()
        self.transfers._ensure_idle()
        data, notice = await self.sources.official_market(fresh=True)
        job = self.transfers._new_job(owner, "manifest_install")
        job.update(
            status="preview", manifest=manifest, plan=await self._plan(manifest, data)
        )
        if notice:
            job["warnings"].append(notice)
        self.transfers._phase(job, "清单预检完成，等待确认")
        return self.preview_result(job)

    def preview_result(self, job: dict) -> dict:
        return {
            "job": self.transfers.public_job(job),
            "plugins": [
                {key: value for key, value in item.items() if key != "remote"}
                for item in job["plan"]
            ],
            "expires_at": job["created_at"] + PREVIEW_TTL,
        }

    async def start(
        self, owner: str, identifier: str, ids: list[str], acknowledged: bool
    ) -> dict:
        self.operations.ensure_writable()
        job = self.transfers.get_job(identifier, owner)
        if (
            job["kind"] != "manifest_install"
            or job["status"] != "preview"
            or time.time() > job["created_at"] + PREVIEW_TTL
        ):
            raise ValueError("清单预检已过期或已执行，请重新预检。")
        allowed = {
            item["market_plugin_id"]
            for item in job["plan"]
            if item["status"] == "install"
        }
        if (
            acknowledged is not True
            or not isinstance(ids, list)
            or not ids
            or any(not isinstance(item, str) for item in ids)
            or len(set(ids)) != len(ids)
            or not set(ids) <= allowed
        ):
            raise ValueError("请确认来源与安装范围，只能选择通过预检的新插件。")
        job["selected"] = ids
        return self.transfers._start(job, self._install)

    async def _install(self, job: dict) -> None:
        # Fetch again after acquiring the shared task lock: previews do not authorize changed sources.
        data, notice = await self.sources.official_market(fresh=True)
        if notice:
            raise ValueError("当前只能取得市场缓存，清单安装已停止，请联网后重新预检。")
        current = await self._plan(job["manifest"], data)
        previous = {item["market_plugin_id"]: item for item in job["plan"]}
        for item in current:
            identifier = item["market_plugin_id"]
            if identifier not in job["selected"]:
                continue
            result = {"directory": item["directory"], "market_plugin_id": identifier}
            try:
                if item["status"] != "install" or fingerprint(
                    item.get("remote")
                ) != fingerprint(previous[identifier].get("remote")):
                    raise ValueError(
                        "目标插件或市场条目已变化，请重新预检；未覆盖现有插件。"
                    )
                self.transfers._phase(job, "下载并校验：" + identifier)
                await self._install_one(job, item)
                result.update(
                    status="installed",
                    message="已安装到磁盘并保持停用；重启扫描后手动启用。",
                )
                job["reload_required"] = True
            except Exception as exc:
                result.update(
                    status="failed",
                    message=redact(str(exc))
                    if isinstance(exc, ValueError)
                    else "安装失败，请检查日志；本项可能留下部分变更，请勿直接重试。",
                )
                job["warnings"].append(identifier + "：" + result["message"])
            job["results"].append(result)
            self.transfers._save(job)
            if job["status"] == "rollback_failed":
                raise ValueError(
                    "安装项自动恢复失败，后续插件未执行；请检查任务恢复记录。"
                )

    async def _install_one(self, job: dict, item: dict) -> None:
        directory = safe_path(
            self.transfers.job_directory(job["id"]),
            "market-" + fingerprint(item["market_plugin_id"])[:24],
        )
        directory.mkdir()
        try:
            await self.transfers._require_space(MAX_CONTENT + 256 * 1024 * 1024)
            archive = safe_path(directory, "download.zip")
            await download_market_archive(item["remote"]["download_url"], archive)
            code = await asyncio.to_thread(
                unpack_market_archive, archive, safe_path(directory, "checked")
            )
            metadata = await asyncio.to_thread(read_metadata, code)
            if any(
                metadata[field] != item["remote"][field]
                for field in ("name", "author", "version")
            ):
                raise ValueError("下载包中的作者、名称或版本与官方市场不一致。")
            await asyncio.to_thread(check_environment, code, VERSION)
            # Copy only sanitized source files, discarding .git, virtualenvs and bytecode.
            stage = safe_path(directory, "stage")
            await asyncio.to_thread(copy_tree, code, stage, code=True)
            module = f"data.plugins.{metadata['name']}.main"
            artifact = {
                "directory": metadata["name"],
                "sha256": await asyncio.to_thread(digest, archive),
                "market_plugin_id": item["market_plugin_id"],
            }
            async with self.operations.manager._pm_lock:
                self.transfers.in_critical = True
                installed = False
                snapshot = None
                try:
                    if any(
                        local["name"].casefold() == metadata["name"].casefold()
                        or local["directory"].casefold() == metadata["name"].casefold()
                        for local in await asyncio.to_thread(self.sources._locals)
                    ):
                        raise ValueError("安装期间本机新增了同名插件，拒绝覆盖。")
                    target = safe_path(self.sources.root, metadata["name"])
                    if target.exists():
                        raise ValueError("目标目录已出现，拒绝覆盖。")
                    inactive = await self.transfers.preferences.global_get(
                        "inactivated_plugins", []
                    )
                    snapshot = await self.sources.source_snapshot(
                        metadata["name"], metadata["name"]
                    )
                    job["pending_install"] = {
                        **artifact,
                        "module_path": module,
                        "was_inactive": module in inactive,
                        "source_state": snapshot,
                    }
                    self.transfers._save(job)
                    await self.transfers.preferences.global_put(
                        "inactivated_plugins", list(dict.fromkeys([*inactive, module]))
                    )
                    # rename is same-volume and does not overwrite an existing directory.
                    await asyncio.to_thread(stage.rename, target)
                    installed = True
                    records = await self.operations.web.get_plugin_install_sources()
                    records[metadata["name"]] = {
                        "schema_version": 1,
                        "root_dir_name": metadata["name"],
                        "name": metadata["name"],
                        "author": metadata["author"],
                        "install_method": "market",
                        "registry_url": None,
                        "registry_name": "Default",
                        "market_plugin_id": item["market_plugin_id"],
                        "repo": item["remote"]["repo"],
                        "download_url": item["remote"]["download_url"],
                        "installed_at": now(),
                    }
                    await self.operations.web.save_plugin_install_sources(records)
                    await self.sources.observe_install(
                        metadata["name"], "market", job["owner"]
                    )
                    job.setdefault("installed_artifacts", []).append(artifact)
                    job.pop("pending_install", None)
                except Exception as exc:
                    if snapshot is not None:
                        try:
                            if installed:
                                await asyncio.to_thread(target.rename, stage)
                            await self.sources.restore_sources(snapshot)
                            current = await self.transfers.preferences.global_get(
                                "inactivated_plugins", []
                            )
                            restored = [value for value in current if value != module]
                            if module in inactive:
                                restored.append(module)
                            await self.transfers.preferences.global_put(
                                "inactivated_plugins", restored
                            )
                            job.pop("pending_install", None)
                        except Exception:
                            job["status"] = "rollback_failed"
                            job["reload_required"] = True
                            raise ValueError(
                                "安装失败且自动恢复未完成；已停止后续安装，请检查任务恢复记录。"
                            ) from exc
                        raise ValueError(
                            "本项安装失败，已恢复目录、来源和停用偏好。"
                        ) from exc
                    raise
                finally:
                    self.transfers.in_critical = False
        finally:
            await asyncio.to_thread(
                remove_path, self.transfers.job_directory(job["id"]), directory.name
            )

    @staticmethod
    async def download_export(path, manifest: dict) -> None:
        await asyncio.to_thread(write_json, path, manifest)
