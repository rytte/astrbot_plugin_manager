"""Compact sharing manifests with explicit market or repository installs."""

from __future__ import annotations

import asyncio
import copy
import json
import time

from astrbot.dashboard.services.plugin_service import (
    PluginServiceError,
    PluginServiceWarning,
)

from .identity import authors_match
from .market_archive import unpack_market_archive
from .redaction import redact
from .repository_archive import download_repository_archive
from .service import is_market_id
from .sources import fingerprint, repository_url, same_repository
from .transfer_bundle import (
    MAX_CONTENT,
    MAX_JSON,
    component,
    exact_keys,
    read_metadata,
    safe_path,
    write_json,
)
from .transfers import PREVIEW_TTL

MANIFEST_FORMAT = "astrbot-plugin-manager-manifest"
MANIFEST_VERSION = 1


async def _file_work(function, *arguments, **keywords):
    """Join file workers before cancellation permits cleanup or job deletion."""
    worker = asyncio.create_task(asyncio.to_thread(function, *arguments, **keywords))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as exc:
        try:
            await worker
        finally:
            raise exc


def validate_manifest(value) -> dict:
    if (
        not isinstance(value, dict)
        or value.get("format") != MANIFEST_FORMAT
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != MANIFEST_VERSION
    ):
        raise ValueError(f"仅支持分享清单（v{MANIFEST_VERSION}），请重新导出清单。")
    exact_keys(value, {"format", "schema_version", "plugins"})
    value = copy.deepcopy(value)
    if not isinstance(value["plugins"], list) or not 1 <= len(value["plugins"]) <= 256:
        raise ValueError("插件清单须包含 1～256 个插件。")
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_JSON:
        raise ValueError("插件清单超过 8 MiB 限制。")
    seen = set()
    repositories = []
    for entry in value["plugins"]:
        if isinstance(entry, dict):
            entry.setdefault("market_plugin_id", "")
            entry.setdefault("repo_url", "")
        exact_keys(
            entry,
            {
                "market_plugin_id",
                "display_name",
                "repo_url",
            },
        )
        if any(
            not isinstance(entry[field], str)
            or len(entry[field]) > (2048 if field == "repo_url" else 512)
            for field in entry
        ):
            raise ValueError("插件清单字段须是有限长度字符串。")
        identifier = entry["market_plugin_id"]
        if identifier:
            if not is_market_id(identifier) or identifier.casefold() in seen:
                raise ValueError("市场 ID 无效或重复。")
            component(identifier.split("/", 1)[1], module=True)
            seen.add(identifier.casefold())
        if entry["repo_url"]:
            url = repository_url(entry["repo_url"])
            if any(same_repository(url, previous) for previous in repositories):
                raise ValueError("清单包含重复仓库。")
            repositories.append(url)
        if not identifier and not entry["repo_url"]:
            raise ValueError("每个插件至少需要市场 ID 或仓库地址，请先补全来源。")
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
        market_author, market_name = entry["market_plugin_id"].split("/", 1)
        if (
            not metadata
            or metadata.get("name") != market_name
            or not authors_match(metadata.get("author"), market_author, market=True)
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
            )
        }
        remote["repo"] = repository_url(remote["repo"])
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
        entries = []
        for directory in directories:
            record = records.get(directory)
            if not record:
                raise ValueError("所选插件已不存在，请刷新。")
            self.sources._editable(record)
            bindings = sorted(
                (
                    binding
                    for binding in record["bindings"]
                    if binding.get("verification")
                    not in {"metadata_changed", "imported"}
                ),
                key=lambda binding: binding["id"] != record["active_binding_id"],
            )
            market = next(
                (
                    binding
                    for binding in bindings
                    if binding["kind"] == "market" and binding["registry_url"] is None
                ),
                None,
            )
            repository = next(
                (
                    binding
                    for binding in bindings
                    if binding["kind"] == "repository" and binding.get("repo_url")
                ),
                market,
            )
            repo_url = ""
            if repository and repository.get("repo_url"):
                try:
                    repo_url = repository_url(repository["repo_url"])
                except ValueError:
                    pass
            local = record["local"]
            entry = {
                "display_name": local["display_name"] or local["name"],
                "market_plugin_id": market["market_plugin_id"] if market else "",
                "repo_url": repo_url,
            }
            if not entry["market_plugin_id"] and not repo_url:
                raise ValueError(
                    f"{directory} 缺少可分享的来源，请先校验并关联官方市场或 HTTPS 仓库。"
                )
            entries.append(entry)
        manifest = validate_manifest(
            {
                "format": MANIFEST_FORMAT,
                "schema_version": MANIFEST_VERSION,
                "plugins": entries,
            }
        )
        return {"manifest": manifest, "notice": None}

    def _check_local(self, remote: dict, locals_: list[dict], *, kind: str) -> dict:
        name = component(remote["name"], module=True)
        matches = [
            local
            for local in locals_
            if local["name"].casefold() == name.casefold()
            or local["directory"].casefold() == name.casefold()
        ]
        if matches:
            if (
                len(matches) == 1
                and matches[0]["name"] == remote["name"]
                and authors_match(
                    matches[0]["author"], remote["author"], market=kind == "market"
                )
                and not matches[0]["problem"]
            ):
                return {
                    "status": "skip",
                    "directory": matches[0]["directory"],
                    "problem": "本机已有同身份插件，将跳过，不修改版本、来源或启停状态。",
                }
            raise ValueError("本机存在同名或同目录的不同身份插件。")
        if safe_path(self.sources.root, name).exists():
            raise ValueError("安装目标目录已存在，拒绝覆盖。")
        return {"status": "install", "directory": name, "problem": ""}

    async def _repository(self, job: dict, item: dict) -> dict:
        directory = safe_path(
            self.transfers.job_directory(job["id"]), "repository-" + item["id"]
        )
        directory.mkdir()
        await self.transfers._require_space(MAX_CONTENT + 256 * 1024 * 1024)
        url = repository_url(item["repo_url"])
        archive = safe_path(directory, "download.zip")
        await download_repository_archive(url, archive)
        code = await _file_work(
            unpack_market_archive, archive, safe_path(directory, "checked")
        )
        metadata = await _file_work(read_metadata, code)
        if item["market_plugin_id"]:
            market_author, market_name = item["market_plugin_id"].split("/", 1)
            if metadata["name"] != market_name or not authors_match(
                metadata["author"], market_author, market=True
            ):
                raise ValueError("仓库中的作者或名称与清单市场 ID 不一致。")
        return {
            **{field: metadata[field] for field in ("name", "author", "version")},
            "repo": url,
            "download_url": "",
        }

    async def _plan(self, job: dict, data: dict, market_problem: str) -> list[dict]:
        locals_ = [record["local"] for record in await self.sources.inventory()]
        plan = []
        for index, entry in enumerate(job["manifest"]["plugins"]):
            item = {
                **entry,
                "id": str(index),
                "default_source": "market"
                if entry["market_plugin_id"]
                else "repository",
                "sources": [],
            }
            for kind, exists in (
                ("market", entry["market_plugin_id"]),
                ("repository", entry["repo_url"]),
            ):
                if not exists:
                    continue
                option = {
                    "kind": kind,
                    "status": "blocked",
                    "problem": "",
                    "name": "",
                    "author": "",
                    "version": "",
                    "directory": "",
                }
                try:
                    if kind == "market" and market_problem:
                        raise ValueError(market_problem)
                    remote = (
                        self._remote(data, entry)
                        if kind == "market"
                        else await self._repository(job, item)
                    )
                    option.update(
                        remote=remote,
                        **{
                            field: remote[field]
                            for field in ("name", "author", "version")
                        },
                    )
                    option.update(self._check_local(remote, locals_, kind=kind))
                except Exception as exc:
                    option["problem"] = (
                        redact(str(exc))
                        if isinstance(exc, ValueError)
                        else "来源预检失败，请检查网络、仓库地址或安装包格式。"
                    )
                item["sources"].append(option)
            plan.append(item)
            self.transfers._phase(
                job, f"正在解析来源：{index + 1}/{len(job['manifest']['plugins'])}"
            )
        return plan

    def begin_preview(self, owner: str, value: dict) -> dict:
        manifest = validate_manifest(value)
        self.operations.ensure_writable()
        self.transfers._ensure_idle()
        job = self.transfers._new_job(owner, "manifest_install")
        job["manifest"] = manifest
        self.transfers._start(job, self._prepare, preview=True)
        return self.read_preview(owner, job["id"])

    async def _prepare(self, job: dict) -> None:
        data, market_problem = await self._market_data(job["manifest"]["plugins"])
        job["plan"] = await self._plan(job, data, market_problem)
        job["preview_expires_at"] = time.time() + PREVIEW_TTL

    @staticmethod
    def _deadline(job: dict) -> float:
        return job.get("preview_expires_at", job["created_at"] + PREVIEW_TTL)

    def _ready_job(self, owner: str, identifier: str) -> dict:
        job = self.transfers.get_job(identifier, owner)
        if (
            job["kind"] != "manifest_install"
            or job["status"] != "preview"
            or time.time() > self._deadline(job)
        ):
            raise ValueError("清单预检已过期或已执行，请重新预检。")
        validate_manifest(job.get("manifest"))
        return job

    def recheck(self, owner: str, identifier: str) -> dict:
        job = self.transfers.get_job(identifier, owner)
        if (
            job["kind"] != "manifest_install"
            or job.get("selected")
            or job["status"] not in {"preview", "failed", "interrupted"}
        ):
            raise ValueError("此任务不能重新预检，请重新上传清单。")
        return self.begin_preview(owner, job.get("manifest"))

    def save_draft(self, owner: str, identifier: str, draft: list[dict]) -> dict:
        self.operations.ensure_writable()
        job = self._ready_job(owner, identifier)
        if not isinstance(draft, list) or len(draft) != len(job["plan"]):
            raise ValueError("清单选择草稿不完整，请重新打开预检。")
        options = {
            item["id"]: {source["kind"]: source for source in item["sources"]}
            for item in job["plan"]
        }
        seen = set()
        for choice in draft:
            exact_keys(choice, {"id", "source", "selected"})
            if (
                not isinstance(choice["id"], str)
                or not isinstance(choice["source"], str)
                or type(choice["selected"]) is not bool
                or choice["id"] in seen
                or choice["source"] not in options.get(choice["id"], {})
                or (
                    choice["selected"]
                    and options[choice["id"]][choice["source"]]["status"] != "install"
                )
            ):
                raise ValueError("清单选择草稿中的插件或来源无效。")
            seen.add(choice["id"])
        job["draft"] = copy.deepcopy(draft)
        self.transfers._save(job)
        return {"saved": True}

    async def preview(self, owner: str, value: dict) -> dict:
        """Await the background preview for local callers; HTTP uses polling."""
        pending = self.begin_preview(owner, value)
        await self.transfers.task
        return self.read_preview(owner, pending["job"]["id"])

    def read_preview(self, owner: str, identifier: str) -> dict:
        job = self.transfers.get_job(identifier, owner)
        if job["kind"] != "manifest_install":
            raise ValueError("任务不是清单安装预检。")
        validate_manifest(job.get("manifest"))
        if job["status"] in {"queued", "running"}:
            return {"job": self.transfers.public_job(job), "ready": False}
        if job["status"] != "preview":
            raise ValueError(job["error"] or "清单预检已执行或未完成，请重新上传。")
        self._ready_job(owner, identifier)
        return self.preview_result(job)

    async def _market_data(self, entries: list[dict]) -> tuple[dict, str]:
        if not any(entry.get("market_plugin_id") for entry in entries):
            return {}, ""
        try:
            data, notice = await self.sources.official_market(fresh=True)
            return data, "当前只能取得市场缓存，请联网后重新预检。" if notice else ""
        except Exception as exc:
            return {}, redact(str(exc)) if isinstance(
                exc, ValueError
            ) else "官方市场读取失败，请稍后重试或明确选择仓库来源。"

    def preview_result(self, job: dict) -> dict:
        return {
            "ready": True,
            "job": self.transfers.public_job(job),
            "plugins": [
                {
                    **{key: value for key, value in item.items() if key != "sources"},
                    "sources": [
                        {key: value for key, value in source.items() if key != "remote"}
                        for source in item["sources"]
                    ],
                }
                for item in job["plan"]
            ],
            "expires_at": self._deadline(job),
            "draft": copy.deepcopy(job.get("draft")),
        }

    async def start(
        self, owner: str, identifier: str, selections: list[dict], acknowledged: bool
    ) -> dict:
        self.operations.ensure_writable()
        job = self._ready_job(owner, identifier)
        allowed = {
            (item["id"], source["kind"])
            for item in job["plan"]
            for source in item["sources"]
            if source["status"] == "install"
        }
        if (
            acknowledged is not True
            or not isinstance(selections, list)
            or not selections
        ):
            raise ValueError("请确认来源与安装范围，只能选择通过预检的新插件。")
        seen = set()
        names = set()
        for selection in selections:
            exact_keys(selection, {"id", "source"})
            if (
                not isinstance(selection["id"], str)
                or not isinstance(selection["source"], str)
                or (selection["id"], selection["source"]) not in allowed
                or selection["id"] in seen
            ):
                raise ValueError("插件或安装来源选择无效、重复或未通过预检。")
            seen.add(selection["id"])
            source = next(
                source
                for item in job["plan"]
                if item["id"] == selection["id"]
                for source in item["sources"]
                if source["kind"] == selection["source"]
            )
            name = source["name"].casefold()
            if name in names:
                raise ValueError("所选来源解析到同名插件，请仅选择其中一项。")
            names.add(name)
        job["selected"] = copy.deepcopy(selections)
        return self.transfers._start(job, self._install)

    async def _install(self, job: dict) -> None:
        entries = {item["id"]: item for item in job["plan"]}
        data, market_problem = await self._market_data(
            [
                entries[selected["id"]]
                for selected in job["selected"]
                if selected["source"] == "market"
            ]
        )
        for selected in job["selected"]:
            entry = entries[selected["id"]]
            option = next(
                source
                for source in entry["sources"]
                if source["kind"] == selected["source"]
            )
            item = {**entry, **option}
            identifier = (
                entry["market_plugin_id"]
                if selected["source"] == "market"
                else entry["repo_url"]
            )
            result = {
                "directory": option["directory"],
                "market_plugin_id": entry["market_plugin_id"],
                "source": selected["source"],
            }
            try:
                if selected["source"] == "market":
                    if market_problem:
                        raise ValueError(market_problem)
                    if fingerprint(self._remote(data, entry)) != fingerprint(
                        option["remote"]
                    ):
                        raise ValueError("市场条目已变化，请重新预检。")
                locals_ = [record["local"] for record in await self.sources.inventory()]
                if (
                    self._check_local(item["remote"], locals_, kind=selected["source"])[
                        "status"
                    ]
                    != "install"
                ):
                    raise ValueError(
                        "目标插件或市场条目已变化，请重新预检；未覆盖现有插件。"
                    )
                self.transfers._phase(job, "正在安装并加载：" + identifier)
                job["pending_install"] = {
                    **result,
                    "name": item["remote"]["name"],
                    "author": item["remote"]["author"],
                    "repo_url": item["remote"]["repo"],
                    "installer": "astrbot",
                }
                self.transfers._save(job)
                self.transfers.in_critical = True
                try:
                    installed = await self.operations.install_target(
                        identifier,
                        actor=job["owner"],
                        expected_identity=(
                            item["remote"]["name"],
                            item["remote"]["author"],
                        ),
                    )
                finally:
                    self.transfers.in_critical = False
                target = installed.target
                result.update(
                    directory=target.directory,
                    status="installed",
                    message=installed.message,
                    version=target.plugin.version,
                    enabled=target.plugin.activated,
                )
                job.setdefault("installed_artifacts", []).append(
                    {
                        "directory": target.directory,
                        "name": target.name,
                        "author": target.plugin.author,
                        "version": target.plugin.version,
                        "market_plugin_id": entry["market_plugin_id"]
                        if selected["source"] == "market"
                        else "",
                        "source": selected["source"],
                        "repo_url": item["remote"]["repo"],
                    }
                )
                if installed.warning:
                    job["warnings"].append(identifier + "：" + installed.warning)
            except Exception as exc:
                result.update(
                    status="failed",
                    message=redact(exc.public_message)
                    if isinstance(exc, PluginServiceError | PluginServiceWarning)
                    else redact(str(exc))
                    if isinstance(exc, ValueError)
                    else "安装流程未完成，请在 WebUI 检查插件及日志；本项可能留下部分变更，请勿直接重试。",
                )
                job["warnings"].append(identifier + "：" + result["message"])
            job["results"].append(result)
            job.pop("pending_install", None)
            self.transfers._save(job)

    @staticmethod
    async def download_export(path, manifest: dict) -> None:
        await _file_work(write_json, path, manifest)
