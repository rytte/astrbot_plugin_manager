"""Check and update plugins using AstrBot's current source records."""

from __future__ import annotations

import asyncio
import copy
import logging
import uuid

from astrbot.dashboard.services.plugin_service import PluginServiceError
from packaging.version import InvalidVersion, Version

from .identity import authors_match
from .redaction import redact
from .service import MARKET_QUERY_TIMEOUT_SECONDS
from .sources import fingerprint, now, same_repository

logger = logging.getLogger("astrbot")


class UpdateService:
    def __init__(self, sources, transfers) -> None:
        self.sources = sources
        self.transfers = transfers
        self.operations = sources.operations
        self.task: asyncio.Task | None = None
        self.report: dict | None = None
        self.lock = asyncio.Lock()
        self.closing = False

    def state(self, owner: str) -> dict:
        """Return the caller's latest check without private update plans."""
        if not self.report or self.report["owner"] != owner:
            return {"id": None, "status": "idle", "results": [], "done": 0, "total": 0}
        return redact(
            {
                key: copy.deepcopy(value)
                for key, value in self.report.items()
                if key not in {"owner", "results"}
            }
            | {
                "results": [
                    {
                        key: value
                        for key, value in entry.items()
                        if not key.startswith("_")
                    }
                    for entry in self.report["results"]
                ]
            }
        )

    @staticmethod
    def _entry(record: dict, binding: dict) -> dict:
        local = record["local"]
        return {
            "directory": local["directory"],
            "revision": record["revision"],
            "binding_id": binding["id"],
            "kind": binding["kind"],
            "locator": binding["market_plugin_id"]
            if binding["kind"] == "market"
            else binding["repo_url"],
            "registry_url": binding.get("registry_url"),
            "local_version": local["version"],
            "remote_version": "",
            "status": "pending",
            "message": "等待检查",
            "can_update": False,
            "_record": copy.deepcopy(record),
            "_binding": copy.deepcopy(binding),
        }

    async def start_check(self, owner: str) -> dict:
        """Start one bounded background check of each plugin's current source."""
        async with self.lock:
            self.transfers._ensure_idle()
            if self.closing or (self.task and not self.task.done()):
                raise ValueError("已有来源检查正在运行或管家正在关闭，请稍后重试。")
            records = await self.sources.inventory()
            entries = [
                self._entry(record, record["current_source"])
                for record in records
                if record["current_source"]
            ]
            self.report = {
                "id": uuid.uuid4().hex,
                "owner": owner,
                "status": "running",
                "checked_at": now(),
                "done": 0,
                "total": len(entries),
                "results": entries,
                "error": "",
            }
            self.task = asyncio.create_task(self._check(self.report))
            return self.state(owner)

    async def check_all(self, owner: str) -> str:
        """Wait for this caller's check and summarize only actionable findings."""
        await self.start_check(owner)
        report, task = self.report, self.task
        await asyncio.shield(task)
        if report["status"] != "completed":
            raise ValueError(report["error"] or "更新检查未完成，请重新执行命令。")
        if not report["total"]:
            return "没有当前更新源可检查，请先在官方插件页面选择来源，或在「来源详情」中补充来源。"
        available, blocked, failed = [], [], []
        for entry in report["results"]:
            local = entry["_record"]["local"]
            label = (
                f"{local['display_name']}（{local['name']}）"
                if local["display_name"]
                else local["name"]
            )
            source = (
                ("市场" if entry["kind"] == "market" else "仓库")
                + "："
                + entry["locator"]
            )
            versions = f"{entry['local_version']} → {entry['remote_version']}"
            if entry["can_update"]:
                available.append(
                    f"- {label}：{versions}\n  /pm update {entry['directory']}"
                )
            elif entry["status"] == "update_available":
                blocked.append(f"- {label}：{versions}；{entry['message']}")
            elif entry["status"] == "error":
                failed.append(f"- {label}（当前更新源 · {source}）：{entry['message']}")
        unchanged = sum(entry["status"] == "no_update" for entry in report["results"])
        plugins = len({entry["directory"] for entry in report["results"]})
        lines = [
            f"更新检查完成：{plugins} 个插件。",
            f"可更新 {len(available)} 个插件 · 无版本更新 {unchanged} 个插件 · 检查失败 {len(failed)} 个插件",
        ]
        for title, entries in (
            ("可更新：", available),
            ("当前来源有新版本，暂无法更新：", blocked),
            ("检查失败：", failed),
        ):
            if entries:
                lines.extend(["", title, *entries])
        return redact("\n".join(lines))

    async def _fetch(self, binding: dict) -> object:
        """Fetch current metadata through AstrBot, rejecting offline market fallback."""
        if binding["kind"] == "market":
            data, notice = await asyncio.wait_for(
                self.operations.web.get_online_plugins(
                    custom_registry=binding.get("registry_url"), force_refresh=True
                ),
                timeout=MARKET_QUERY_TIMEOUT_SECONDS,
            )
            if notice:
                raise ValueError("市场联网检查失败，当前只能取得旧缓存，请稍后重试。")
            return data
        if binding["kind"] != "repository":
            raise ValueError("更新来源类型不受支持。")
        return await asyncio.wait_for(
            self.operations.manager.inspect_plugin_repository(binding["repo_url"]),
            timeout=MARKET_QUERY_TIMEOUT_SECONDS,
        )

    def _evaluate(self, record: dict, binding: dict, data: object) -> dict:
        """Validate source identity and compare declared versions, without modifying plugins."""
        local = record["local"]
        if local["problem"] or record["identity_changed"]:
            raise ValueError(local["problem"] or "插件目录身份已变化，请重新关联来源。")
        metadata = (
            self.operations.web.resolve_market_plugin_entry_by_id(
                data, binding["market_plugin_id"]
            )
            if binding["kind"] == "market"
            else data
        )
        if not isinstance(metadata, dict):
            raise ValueError("来源中未找到该插件。")
        if metadata.get("name") != local["name"] or not authors_match(
            local["author"], metadata.get("author"), market=binding["kind"] == "market"
        ):
            raise ValueError("来源中的插件名或作者与本机不一致。")
        if binding["kind"] == "market" and not metadata.get("repo"):
            raise ValueError("市场未提供插件仓库地址，请重新校验来源。")
        if (
            binding["kind"] == "market"
            and binding.get("repo_url")
            and not same_repository(binding["repo_url"], metadata.get("repo"))
        ):
            raise ValueError("市场仓库地址已变化，请重新校验来源。")
        remote = str(metadata.get("version") or "").strip()
        try:
            has_update = Version(remote) > Version(str(local["version"]).strip())
        except InvalidVersion as exc:
            raise ValueError("本机或远端版本号无法比较，请检查插件元数据。") from exc
        reason = ""
        plugin = self.operations.context.get_registered_star(local["name"])
        if local["reserved"]:
            reason = "内置插件请通过 AstrBot 更新。"
        elif local["status"] not in {"enabled", "disabled"}:
            reason = "插件未正常注册，请通过官方插件页面修复。"
        elif plugin and plugin.module_path == self.operations.owner_module:
            reason = "插件管家自身请通过官方插件页面更新。"
        elif record["metadata_changed"]:
            reason = "插件信息已变化，请先点击「更新信息」后重新检查。"
        return {
            "remote_version": remote,
            "status": "update_available" if has_update else "no_update",
            "message": ("发现新版本" if has_update else "无版本更新")
            + ("；" + reason if has_update and reason else ""),
            "can_update": has_update
            and binding["id"] == (record["current_source"] or {}).get("id")
            and not reason,
            "_metadata": {
                key: metadata.get(key, "")
                for key in ("name", "author", "version", "repo", "download_url")
            },
        }

    async def _check(self, report: dict) -> None:
        requests: dict[tuple, asyncio.Task] = {}
        semaphore = asyncio.Semaphore(3)

        async def fetch(binding):
            async with semaphore:
                return await self._fetch(binding)

        async def check(entry):
            try:
                binding = entry["_binding"]
                key = (
                    (binding["kind"], binding.get("registry_url") or "")
                    if binding["kind"] == "market"
                    else (binding["kind"], binding["repo_url"])
                )
                if key not in requests:
                    requests[key] = asyncio.create_task(fetch(binding))
                data = await requests[key]
                entry.update(self._evaluate(entry["_record"], binding, data))
            except asyncio.TimeoutError:
                entry.update(status="error", message="来源检查超时，请稍后重试。")
            except (ValueError, PluginServiceError) as exc:
                entry.update(
                    status="error",
                    message=redact(
                        exc.public_message
                        if isinstance(exc, PluginServiceError)
                        else str(exc)
                    ),
                )
            except Exception:
                logger.exception("Plugin source update check failed.")
                entry.update(
                    status="error", message="来源检查失败，请检查 AstrBot 日志。"
                )
            finally:
                report["done"] += 1

        try:
            await asyncio.gather(*(check(entry) for entry in report["results"]))
            report["status"] = "completed"
        except asyncio.CancelledError:
            report.update(
                status="interrupted", error="来源检查被插件关闭中断，请重新检查。"
            )
        finally:
            for task in requests.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*requests.values(), return_exceptions=True)

    async def start_update(
        self,
        owner: str,
        check_id: str,
        directory: str,
        binding_id: str,
        acknowledged: bool,
    ) -> dict:
        """Queue one explicitly confirmed update from the checked active source."""
        self.operations.ensure_writable()
        if acknowledged is not True:
            raise ValueError("请确认插件更新。")
        async with self.lock:
            self.transfers._ensure_idle()
            if self.closing:
                raise ValueError("插件管家正在关闭，请稍后重试。")
            report = self.report
            if not report or report["id"] != check_id or report["owner"] != owner:
                raise ValueError("更新检查结果不存在或无权访问，请重新检查。")
            if report["status"] != "completed":
                raise ValueError("来源检查尚未完成，请稍后重试。")
            entry = next(
                (
                    item
                    for item in report["results"]
                    if item["directory"] == directory
                    and item["binding_id"] == binding_id
                ),
                None,
            )
            if (
                not entry
                or not entry["can_update"]
                or entry["status"] != "update_available"
            ):
                raise ValueError("只有当前更新源发现新版本的插件可以更新，请重新检查。")
            job = self.transfers._new_job(owner, "plugin_update")
            job["update_plan"] = copy.deepcopy(entry)
            job["check_id"] = check_id
            job["update_mode"] = "checked"
            entry.update(can_update=False, status="updating", message="正在更新")
            return self.transfers._start(job, self._update)

    async def start_current_update(self, owner: str, identifier: str) -> dict:
        """Queue a direct command using only the plugin's current update source."""
        self.operations.ensure_writable()
        async with self.lock:
            self.transfers._ensure_idle()
            if self.closing:
                raise ValueError("插件管家正在关闭，请稍后重试。")
            target = self.operations.resolve(identifier)
            self.operations.check_operation(target, "update")
            record = await self.sources.detail(target.directory)
            self.operations.revalidate(target)
            if record["local"]["problem"] or record["identity_changed"]:
                raise ValueError(
                    record["local"]["problem"] or "插件目录身份已变化，请重新关联来源。"
                )
            if record["metadata_changed"]:
                raise ValueError(
                    "插件信息已变化，请先在插件页面点击「更新信息」后重试。"
                )
            binding = record["current_source"]
            if binding is None:
                raise ValueError(
                    "插件没有当前更新源，请先在官方插件页面选择来源，或在「来源详情」中补充来源。"
                )
            job = self.transfers._new_job(owner, "plugin_update")
            job["update_plan"] = self._entry(record, binding)
            job["check_id"] = None
            job["update_mode"] = "current"
            return self.transfers._start(job, self._update)

    async def _update(self, job: dict) -> None:
        entry = job["update_plan"]
        directory = entry["directory"]
        entered = False
        try:
            record = await self.sources.detail(directory)
            if (
                record["revision"] != entry["_record"]["revision"]
                or (record["current_source"] or {}).get("id") != entry["binding_id"]
            ):
                raise ValueError("插件身份、版本或当前更新源已变化，请重新检查。")
            binding = entry["_binding"]
            self.transfers._phase(job, "重新检查当前更新源")
            fresh = self._evaluate(record, binding, await self._fetch(binding))
            record = await self.sources.detail(directory)
            if record["revision"] != entry["_record"]["revision"]:
                raise ValueError("插件或来源已变化，请重新检查。")
            if job["update_mode"] == "current":
                if fresh["status"] == "no_update":
                    job["results"].append(
                        {
                            "directory": directory,
                            "status": "no_update",
                            "message": "已是最新版本。",
                        }
                    )
                    return
                if not fresh["can_update"]:
                    raise ValueError(fresh["message"])
            elif not fresh["can_update"] or fingerprint(
                fresh["_metadata"]
            ) != fingerprint(entry["_metadata"]):
                raise ValueError("远端版本或来源已变化，请重新检查后更新。")
            target = self.operations.resolve(directory)
            self.operations.check_operation(target, "update")
            core_record = self.sources._core(
                record["local"],
                await self.operations.web.get_plugin_install_sources(),
            )
            if fingerprint(core_record) != record["core_revision"]:
                raise ValueError("AstrBot 当前更新源已变化，请重新检查。")
            self.transfers._phase(job, "正在更新并加载：" + target.name)
            self.transfers.in_critical = True
            entered = True
            try:
                current = await self.operations.update_target(
                    target,
                    repo_url=str(fresh["_metadata"]["repo"] or "")
                    if binding["kind"] == "market"
                    else binding["repo_url"],
                    download_url=str(fresh["_metadata"]["download_url"] or "")
                    if binding["kind"] == "market"
                    else "",
                    source_revision=record["revision"],
                )
                await self.operations.web.refresh_plugin_install_source_after_update(
                    current.name,
                    {
                        "record": core_record,
                        "market_plugin": {
                            **fresh["_metadata"],
                            "market_plugin_id": binding["market_plugin_id"],
                        }
                        if binding["kind"] == "market"
                        else None,
                        "repo": binding["repo_url"],
                        "download_url": fresh["_metadata"]["download_url"],
                    },
                )
                updated = await self.sources.detail(directory)
                updated["metadata_snapshot"] = copy.deepcopy(
                    updated["local"]["runtime_metadata"]
                )
                self.sources._event(updated, "updated", job["owner"], binding["id"])
                await self.sources._save_record(updated)
            finally:
                self.transfers.in_critical = False
            message = f"已更新至 {current.version}（{'已启用' if current.activated else '已停用'}）。"
            job["results"].append(
                {"directory": directory, "status": "updated", "message": message}
            )
            status = "updated"
        except Exception as exc:
            if isinstance(exc, asyncio.TimeoutError) and not entered:
                message = "当前更新源检查超时，请稍后重试。"
            elif isinstance(exc, PluginServiceError):
                message = redact(exc.public_message)
            elif isinstance(exc, ValueError):
                message = redact(str(exc))
            else:
                logger.exception("Plugin update failed.")
                message = "更新失败，请检查 AstrBot 日志。"
            if entered:
                message += "可能已修改插件文件，请通过官方插件页面检查或恢复。"
            job["results"].append(
                {"directory": directory, "status": "failed", "message": message}
            )
            self._finish_entry(job, "error", message)
            raise ValueError(message) from exc
        self._finish_entry(job, status, message)

    def _finish_entry(self, job: dict, status: str, message: str) -> None:
        if self.report and self.report["id"] == job["check_id"]:
            for entry in self.report["results"]:
                if (
                    entry["directory"] == job["update_plan"]["directory"]
                    and entry["binding_id"] == job["update_plan"]["binding_id"]
                ):
                    entry.update(status=status, message=message, can_update=False)

    async def close(self) -> None:
        self.closing = True
        if self.task and not self.task.done():
            self.task.cancel()
            await self.task
