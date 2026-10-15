"""Observe AstrBot's current source and retain pending migration references."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from astrbot.api import logger
from astrbot.core.repository import normalize_repository_url, parse_repository_url

from .identity import authors_match
from .redaction import redact
from .service import MARKET_QUERY_TIMEOUT_SECONDS, is_market_id
from .source_bundle import METHODS, portable_binding, validate_sources
from .transfer_bundle import MAX_JSON, component, load_json, safe_path, write_json

SOURCE_LABELS = {
    "market": "插件市场",
    "repository": "代码仓库",
    "upload": "上传安装",
    "bundle": "迁移包导入",
    "unknown": "来源未知",
    "builtin": "AstrBot 内置",
}
METADATA_FIELDS = ("name", "author", "display_name", "version", "repo")
METADATA_LABELS = {
    "name": "插件名",
    "author": "作者",
    "display_name": "显示名称",
    "version": "版本",
    "repo": "仓库地址",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def repository_url(value: str) -> str:
    """Use credential-free HTTPS repository locators for new page bindings."""
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("仓库地址无效。")
    normalized = normalize_repository_url(value)
    parsed = urlsplit(normalized)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ValueError("页面绑定仅支持不含凭据的 HTTPS Git 仓库地址。")
    parse_repository_url(normalized)
    return normalized


def same_repository(first: str, second: str) -> bool:
    """Compare host, port and repository path; never compare owner/repo alone."""
    try:
        left, right = urlsplit(repository_url(first)), urlsplit(repository_url(second))
        paths = [url.path.rstrip("/").removesuffix(".git") for url in (left, right)]
        if left.hostname in {"github.com", "www.github.com"}:
            paths = [path.casefold() for path in paths]
        return (
            left.hostname.removeprefix("www.") == right.hostname.removeprefix("www.")
            and left.port == right.port
            and paths[0] == paths[1]
        )
    except (TypeError, ValueError):
        return False


@dataclass
class _BindingMarket:
    data: dict
    refreshed: bool = False
    error: Exception | None = None


class SourceService:
    """Preserve observations without inventing installation history or trust."""

    def __init__(self, operations, transfers) -> None:
        self.operations = operations
        self.transfers = transfers
        self.root = Path(operations.manager.plugin_store_path).absolute()
        self.path = safe_path(transfers.root.parent, "sources.json")
        self.previews = {}
        self.record_lock = asyncio.Lock()
        self.auto_task: asyncio.Task | None = None
        self.auto_pending: set[str] = set()
        self.auto_in_critical = False
        self.closing = False

    async def initialize(self) -> None:
        await self.schedule_missing_bindings()

    @staticmethod
    def _auto_eligible(record: dict) -> bool:
        local = record["local"]
        return bool(
            local["status"] in {"enabled", "disabled"}
            and not local["reserved"]
            and not local["problem"]
            and not record["identity_changed"]
            and not record["metadata_changed"]
            and not record["current_source"]
            and not record["pending_source"]
        )

    async def schedule_missing_bindings(self) -> None:
        for record in await self.inventory():
            if self._auto_eligible(record):
                self.schedule_binding(record["local"]["directory"])

    def schedule_binding(self, directory: str) -> None:
        """Queue source discovery without blocking AstrBot's plugin load hook."""
        if self.closing or not directory:
            return
        self.auto_pending.add(directory)
        if self.auto_task is None or self.auto_task.done():
            self.auto_task = asyncio.create_task(self._auto_bind())

    async def _auto_bind(self) -> None:
        market = None
        try:
            while self.auto_pending and not self.closing:
                directories = self.auto_pending.copy()
                self.auto_pending.clear()
                records = [
                    record
                    for record in await self.inventory()
                    if record["local"]["directory"] in directories
                    and self._auto_eligible(record)
                ]
                if not records:
                    continue
                try:
                    self.operations.ensure_writable()
                    if market is None:
                        market = self._binding_market()
                except Exception as exc:
                    market = None
                    for record in records:
                        await self._auto_notice(record, str(exc))
                    continue
                for record in records:
                    if self.closing:
                        return
                    try:
                        binding = await self._discover_binding(record, market)
                        if binding is None:
                            raise ValueError(
                                "市场中未找到匹配条目，插件也未声明仓库地址。"
                            )
                        async with (
                            self.transfers.operation_lock,
                            self.operations.manager._pm_lock,
                        ):
                            self.operations.ensure_writable()
                            self.transfers._ensure_idle()
                            current = await self.detail(record["local"]["directory"])
                            if current["current_source"]:
                                continue
                            if (
                                not self._auto_eligible(current)
                                or current["revision"] != record["revision"]
                            ):
                                raise ValueError(
                                    "插件信息或来源已变化，请在来源详情中重新校验。"
                                )
                            self.auto_in_critical = True
                            try:
                                await self._activate(
                                    current,
                                    binding,
                                    "插件管家",
                                    action="source_auto_bound",
                                )
                                await self._save_record(current)
                            finally:
                                self.auto_in_critical = False
                    except Exception as exc:
                        await self._auto_notice(record, str(exc))
        except Exception:
            logger.exception("Automatic plugin source binding failed")

    async def _auto_notice(self, record: dict, message: str) -> None:
        directory = record["local"]["directory"]
        notice = "来源未自动绑定：" + redact(message)
        logger.warning("%s: %s", directory, notice)
        async with self.transfers.operation_lock:
            current = next(
                (
                    item
                    for item in await self.inventory()
                    if item["local"]["directory"] == directory
                ),
                None,
            )
            if current and self._auto_eligible(current):
                current["auto_binding_notice"] = notice
                await self._save_record(current)

    async def close(self) -> None:
        self.closing = True
        self.auto_pending.clear()
        if self.auto_task and not self.auto_task.done():
            if not self.auto_in_critical:
                self.auto_task.cancel()
            await asyncio.gather(self.auto_task, return_exceptions=True)

    def _store(self) -> dict:
        if not self.path.exists():
            return {"schema_version": 1, "plugins": {}, "archived": []}
        data = load_json(self.path)
        if (
            not isinstance(data, dict)
            or data.get("schema_version") != 1
            or not isinstance(data.get("plugins"), dict)
            or not isinstance(data.get("archived"), list)
        ):
            raise ValueError("来源记录格式无效，请保留文件并检查 sources.json。")
        return data

    def _locals(self) -> list[dict]:
        registered = {
            item.root_dir_name: item
            for item in self.operations.context.get_all_stars()
            if item.root_dir_name
        }
        directories = set(registered) | set(self.operations.manager.failed_plugin_dict)
        directories.update(
            path.name
            for path in self.root.iterdir()
            if path.is_dir()
            and not path.name.startswith(".")
            and ((path / "main.py").exists() or (path / f"{path.name}.py").exists())
        )
        result = []
        for directory in sorted(directories, key=str.casefold):
            plugin = registered.get(directory)
            failure = self.operations.manager.failed_plugin_dict.get(directory, {})
            record = {
                "directory": directory,
                "name": getattr(plugin, "name", None) or failure.get("name") or "",
                "author": getattr(plugin, "author", None)
                or failure.get("author")
                or "",
                "display_name": getattr(plugin, "display_name", None)
                or failure.get("display_name")
                or "",
                "version": getattr(plugin, "version", None)
                or failure.get("version")
                or "",
                "repo": getattr(plugin, "repo", None) or failure.get("repo") or "",
                "module_path": getattr(plugin, "module_path", None)
                or f"data.plugins.{directory}.main",
                "reserved": bool(plugin and plugin.reserved),
                "status": ("enabled" if plugin.activated else "disabled")
                if plugin
                else (
                    "failed"
                    if directory in self.operations.manager.failed_plugin_dict
                    else "pending"
                ),
                "problem": "",
            }
            record["runtime_metadata"] = {
                field: record[field] for field in METADATA_FIELDS
            }
            if not record["reserved"]:
                try:
                    component(directory, module=True)
                    code = safe_path(self.root, directory)
                    for filename in ("metadata.yaml", "metadata.yml"):
                        metadata_path = safe_path(code, filename)
                        if not metadata_path.is_file():
                            continue
                        if metadata_path.stat().st_size > MAX_JSON:
                            raise ValueError("插件元数据过大。")
                        metadata = yaml.safe_load(
                            metadata_path.read_text(encoding="utf-8")
                        )
                        if not isinstance(metadata, dict):
                            raise ValueError("插件元数据不是映射。")
                        if plugin and metadata.get("name") != record["name"]:
                            raise ValueError(
                                "磁盘插件名与运行时不同，请先重载或检查插件。"
                            )
                        for field in (
                            "name",
                            "author",
                            "display_name",
                            "version",
                            "repo",
                        ):
                            if isinstance(metadata.get(field), str):
                                record[field] = metadata[field]
                        break
                    if not record["name"] or not record["author"]:
                        raise ValueError("插件身份不完整，暂不能绑定或导出清单。")
                except (ValueError, OSError, yaml.YAMLError, UnicodeError) as exc:
                    record["problem"] = redact(str(exc))
            if plugin is None:
                record["runtime_metadata"] = {
                    field: record[field] for field in METADATA_FIELDS
                }
            result.append(record)
        return result

    @staticmethod
    def _identity(local: dict) -> dict:
        return {field: local[field] for field in ("directory", "name", "author")}

    @staticmethod
    def _binding(core: dict | None) -> dict | None:
        if not isinstance(core, dict) or core.get("implicit"):
            return None
        kind = core.get("install_method")
        if kind not in {"market", "repository"}:
            return None
        binding = {
            "kind": kind,
            "repo_url": str(core.get("repo") or ""),
            "registry_url": core.get("registry_url") if kind == "market" else None,
            "market_plugin_id": str(core.get("market_plugin_id") or "")
            if kind == "market"
            else "",
        }
        if kind == "market" and not is_market_id(binding["market_plugin_id"]):
            return None
        if kind == "repository" and not binding["repo_url"]:
            return None
        binding["id"] = fingerprint(binding)[:24]
        binding.update(verification="observed", checked_at=None)
        return binding

    @staticmethod
    def _core(local: dict, records: dict) -> dict | None:
        core = records.get(local["directory"])
        if core is None:
            core = records.get(local["name"])
        if not isinstance(core, dict) or core.get("implicit"):
            return None
        if core.get("root_dir_name") not in (None, local["directory"]):
            return None
        if core.get("name") not in (None, "", local["name"]):
            return None
        return core

    def _view(self, local: dict, core: dict | None, store: dict) -> dict:
        saved = copy.deepcopy(store["plugins"].get(local["directory"], {}))
        mismatch = bool(
            saved
            and any(
                saved.get("identity", {}).get(field) != local[field]
                for field in ("directory", "name")
            )
        )
        if mismatch:
            saved = {}
            core = None
        first_seen = now()
        if not saved:
            saved = {
                "record_id": uuid.uuid4().hex,
                "identity": {
                    **self._identity(local),
                    "author": local["runtime_metadata"]["author"] or local["author"],
                },
                "provenance": {
                    "method": "builtin"
                    if local["reserved"]
                    else str((core or {}).get("install_method") or "unknown"),
                    "evidence": "core_record_observed" if core else "unknown",
                    "observed_at": first_seen,
                    "installed_at": None,
                },
                "pending_source": None,
                "history": [],
                "metadata_snapshot": copy.deepcopy(local["runtime_metadata"]),
            }
            if core:
                saved["provenance"]["source"] = self._binding(core)
                saved["provenance"]["reported_installed_at"] = core.get("installed_at")
        if "metadata_snapshot" not in saved:
            saved["metadata_snapshot"] = {
                **local["runtime_metadata"],
                "name": saved["identity"]["name"],
                "author": saved["identity"]["author"],
            }
        changes = [
            field
            for field in METADATA_FIELDS
            if saved["metadata_snapshot"].get(field, "") != local[field]
        ]
        active = self._binding(core)
        validation = saved.get("source_validation")
        if active:
            if not validation or validation["id"] != active["id"]:
                validation = {
                    "id": active["id"],
                    "author": saved["identity"]["author"],
                    "checked_at": None,
                }
            if validation["author"] != local["author"]:
                active["verification"] = "metadata_changed"
            elif validation["checked_at"]:
                active.update(
                    verification="identity_matched", checked_at=validation["checked_at"]
                )
        saved.update(
            local=local,
            current_source=active,
            auto_binding_notice="" if active else saved.get("auto_binding_notice", ""),
            pending_source=None if active else saved["pending_source"],
            source_validation=validation if active else None,
            core_revision=fingerprint(core),
            identity_changed=mismatch,
            metadata_changed=bool(changes) and not mismatch and not local["problem"],
            metadata_notice="插件信息已变更："
            + "、".join(METADATA_LABELS[field] for field in changes)
            + "。"
            if changes
            else "",
        )
        saved["revision"] = fingerprint(
            {
                "local": local,
                "core": core,
                "record": store["plugins"].get(local["directory"]),
            }
        )
        return saved

    async def inventory(self) -> list[dict]:
        locals_, records = await asyncio.gather(
            asyncio.to_thread(self._locals),
            self.operations.web.get_plugin_install_sources(),
        )
        async with self.record_lock:
            store = await asyncio.to_thread(self._store)
            changed = False
            for local in locals_:
                core = self._core(local, records)
                record = self._view(local, core, store)
                previous = store["plugins"].get(local["directory"])
                if record["identity_changed"]:
                    continue
                if not previous or previous.get("last_core_revision") != fingerprint(
                    core
                ):
                    if previous:
                        self._event(record, "core_observed", "AstrBot")
                    record["last_core_revision"] = fingerprint(core)
                    store["plugins"][local["directory"]] = self._stored_record(record)
                    changed = True
            if changed:
                await asyncio.to_thread(write_json, self.path, store)
            return [
                self._view(local, self._core(local, records), store)
                for local in locals_
            ]

    async def detail(self, directory: str) -> dict:
        for record in await self.inventory():
            if record["local"]["directory"] == directory:
                return record
        raise ValueError("插件目录不存在，请刷新列表。")

    async def sync_metadata(self, owner: str, directory: str, revision: str) -> dict:
        """Acknowledge local metadata without reloading or changing core state."""
        self.operations.ensure_writable()
        self.transfers._ensure_idle()
        async with self.transfers.operation_lock, self.operations.manager._pm_lock:
            self.transfers._ensure_idle()
            self.operations.ensure_writable()
            record = await self.detail(directory)
            self._editable(record)
            if record["revision"] != revision:
                raise ValueError("插件信息已变化，请刷新后重试。")
            if not record["metadata_changed"]:
                return record
            old_identity = record["identity"]
            local = record["local"]
            # Only revise matching saved selections; never adopt a reused name.
            rule = self.transfers.rule()
            old_id = "/".join(
                old_identity[field].lower().replace("/", "_")
                for field in ("author", "name")
            )
            new_id = "/".join(
                local[field].lower().replace("/", "_") for field in ("author", "name")
            )
            changed_rule = False
            for selection in rule["selections"]:
                if (
                    selection["directory"] == directory
                    and selection["name"] == local["name"]
                    and selection["plugin_id"] == old_id
                    and old_id != new_id
                ):
                    selection["plugin_id"] = new_id
                    changed_rule = True
            if changed_rule:
                rule["updated_at"] = time.time()
                await asyncio.to_thread(write_json, self.transfers.rule_path, rule)
            record["identity"] = self._identity(local)
            record["metadata_snapshot"] = {
                field: local[field] for field in METADATA_FIELDS
            }
            self._event(record, "metadata_synced", owner)
            await self._save_record(record)
        return await self.detail(directory)

    @staticmethod
    def _editable(record: dict) -> None:
        if record["local"]["reserved"] or record["local"]["problem"]:
            raise ValueError("内置插件或身份异常插件不能修改来源。")
        if record["identity_changed"]:
            raise ValueError("目录被其他身份复用，原来源记录已隔离；请检查后重新安装。")

    async def official_market(self, *, fresh=False) -> tuple[dict, str | None]:
        try:
            return await asyncio.wait_for(
                self.operations.web.get_online_plugins(
                    custom_registry=None, force_refresh=fresh
                ),
                timeout=MARKET_QUERY_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise ValueError("官方市场响应超时，请稍后重试。") from exc

    async def preview_binding(
        self, owner: str, directory: str, kind: str, value: str, *, registry_url=None
    ) -> dict:
        record = await self.detail(directory)
        self._editable(record)
        local = record["local"]
        if record["identity"]["author"] != local["author"]:
            raise ValueError("作者信息已变更，请先点击「更新信息」，再校验来源。")
        current = record["current_source"]
        if current and (
            kind != current["kind"]
            or (
                kind == "market"
                and (
                    value != current["market_plugin_id"]
                    or registry_url != current["registry_url"]
                )
            )
            or (kind == "repository" and value != current["repo_url"])
        ):
            raise ValueError("已有当前更新源，更换来源请使用 AstrBot 官方插件页面。")
        notice = None
        if kind == "market":
            if not is_market_id(value):
                raise ValueError("请填写完整的作者/插件技术名。")
            source = self.operations.web.build_registry_source(registry_url)
            data = self.operations.web.load_plugin_cache(source.cache_file)
        elif kind == "repository":
            url = repository_url(value)
            metadata = await asyncio.wait_for(
                self.operations.manager.inspect_plugin_repository(url),
                timeout=MARKET_QUERY_TIMEOUT_SECONDS,
            )
            binding = {
                "kind": kind,
                "repo_url": url,
                "registry_url": None,
                "market_plugin_id": "",
            }
        else:
            raise ValueError("只支持官方市场 ID 或 HTTPS 仓库绑定。")
        # Retry cached marketplace mismatches once against the remote registry.
        for attempt in range(2):
            if kind == "market":
                if attempt:
                    try:
                        data, notice = await asyncio.wait_for(
                            self.operations.web.get_online_plugins(
                                custom_registry=registry_url, force_refresh=True
                            ),
                            timeout=MARKET_QUERY_TIMEOUT_SECONDS,
                        )
                    except asyncio.TimeoutError as exc:
                        raise ValueError("市场响应超时，请稍后重试。") from exc
                    if notice:
                        raise ValueError(
                            "市场联网校验失败，当前只能取得旧缓存，请稍后重试。"
                        )
                metadata = self.operations.web.resolve_market_plugin_entry_by_id(
                    data, value
                )
            try:
                if kind == "market":
                    if not metadata:
                        raise ValueError("官方市场中未找到该 ID。")
                    binding = {
                        "kind": "market",
                        "market_plugin_id": value,
                        "registry_url": registry_url,
                        "repo_url": repository_url(metadata.get("repo")),
                    }
                if metadata.get("name") != local["name"] or not authors_match(
                    local["author"], metadata.get("author"), market=kind == "market"
                ):
                    raise ValueError("来源中的作者或插件名与本机不一致，拒绝绑定。")
                if (
                    current
                    and current.get("repo_url")
                    and not same_repository(current["repo_url"], binding["repo_url"])
                ):
                    raise ValueError(
                        "仓库地址与当前来源不同，请使用 AstrBot 官方插件页面处理。"
                    )
            except ValueError:
                if kind != "market" or attempt:
                    raise
            else:
                break
        binding["id"] = fingerprint(binding)[:24]
        binding.update(verification="identity_matched", checked_at=now())
        identifier = uuid.uuid4().hex
        self.previews = {
            key: value
            for key, value in self.previews.items()
            if value["expires_at"] > time.time()
        }
        if len(self.previews) >= 128:
            raise ValueError("待确认来源过多，请稍后重试。")
        self.previews[identifier] = {
            "owner": owner,
            "directory": directory,
            "revision": record["revision"],
            "binding": binding,
            "expires_at": time.time() + 600,
        }
        return {
            "id": identifier,
            "binding": binding,
            "notice": notice,
            "expires_at": self.previews[identifier]["expires_at"],
        }

    async def preview_existing(self, owner: str, directory: str, revision: str) -> dict:
        """Revalidate the current or imported source without choosing another source."""
        record = await self.detail(directory)
        if record["revision"] != revision:
            raise ValueError("来源记录已变化，请刷新后重试。")
        source = record["current_source"] or record["pending_source"]
        if not source:
            raise ValueError("没有可校验的来源，请先补充来源。")
        return await self.preview_binding(
            owner,
            directory,
            source["kind"],
            source["market_plugin_id"]
            if source["kind"] == "market"
            else source["repo_url"],
            registry_url=source.get("registry_url"),
        )

    async def bind(self, owner: str, identifier: str, acknowledged: bool) -> dict:
        self.operations.ensure_writable()
        if acknowledged is not True:
            raise ValueError("请确认来源校验结果并设为当前更新源。")
        preview = self.previews.get(identifier)
        if (
            not preview
            or preview["owner"] != owner
            or preview["expires_at"] <= time.time()
        ):
            raise ValueError("来源校验已过期或无权访问，请重新校验。")
        self.transfers._ensure_idle()
        async with self.transfers.operation_lock, self.operations.manager._pm_lock:
            self.transfers._ensure_idle()
            record = await self.detail(preview["directory"])
            self._editable(record)
            if record["revision"] != preview["revision"]:
                raise ValueError("插件身份或来源已变化，请重新校验。")
            binding = preview["binding"]
            # Save the original observation before replacing AstrBot's active record.
            await self._save_record(record)
            await self._activate(record, binding, owner)
            record["pending_source"] = None
            await self._save_record(record)
            self.previews.pop(identifier, None)
        return await self.detail(preview["directory"])

    @staticmethod
    def _event(record: dict, action: str, actor: str, binding_id: str = "") -> None:
        record["history"] = [
            *record["history"],
            {"at": now(), "action": action, "actor": actor, "binding_id": binding_id},
        ][-100:]

    def _complete_repository(self, record: dict, repo_url: str) -> None:
        """Fill missing disk and runtime repository metadata for AstrBot's source UI."""
        local = record["local"]
        plugins = [
            plugin
            for plugin in self.operations.context.get_all_stars()
            if plugin.root_dir_name == local["directory"]
        ]
        previous = [(plugin, plugin.repo) for plugin in plugins]
        metadata_path, original = None, None
        temporary = None
        written = False
        try:
            if not local["repo"]:
                code = safe_path(self.root, local["directory"])
                metadata_path = next(
                    (
                        path
                        for filename in ("metadata.yaml", "metadata.yml")
                        if (path := safe_path(code, filename)).is_file()
                    ),
                    safe_path(code, "metadata.yaml"),
                )
                original = (
                    metadata_path.read_bytes() if metadata_path.exists() else None
                )
                metadata = (
                    yaml.safe_load(original.decode("utf-8"))
                    if original is not None
                    else {
                        **{
                            field: local[field]
                            for field in METADATA_FIELDS
                            if field != "repo"
                        },
                        **{
                            field: copy.deepcopy(getattr(plugins[0], field))
                            for field in (
                                "desc",
                                "short_desc",
                                "support_platforms",
                                "astrbot_version",
                                "pages",
                            )
                            if plugins and getattr(plugins[0], field) is not None
                        },
                    }
                )
                if (
                    not isinstance(metadata, dict)
                    or metadata.get("repo")
                    or any(
                        metadata.get(field, local[field]) != local[field]
                        for field in ("name", "author", "version")
                    )
                ):
                    raise ValueError("插件仓库信息已变化，请刷新后重新校验。")
                if original is None and any(
                    not isinstance(metadata.get(field), str)
                    or not metadata[field].strip()
                    for field in ("name", "author", "desc", "version")
                ):
                    raise ValueError(
                        "插件缺少完整元数据，请先补齐 metadata.yaml 后再绑定来源。"
                    )
                metadata["repo"] = repo_url
                temporary = safe_path(code, ".pm-metadata-" + uuid.uuid4().hex)
                temporary.write_text(
                    yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )
                if original is not None:
                    temporary.chmod(metadata_path.stat().st_mode)
                    if metadata_path.read_bytes() != original:
                        raise ValueError("插件仓库信息已变化，请刷新后重新校验。")
                elif metadata_path.exists():
                    raise ValueError("插件仓库信息已变化，请刷新后重新校验。")
                temporary.replace(metadata_path)
                written = True
            for plugin in plugins:
                if not plugin.repo:
                    plugin.repo = local["repo"] or repo_url
        except Exception:
            if written:
                if original is None:
                    metadata_path.unlink(missing_ok=True)
                else:
                    metadata_path.write_bytes(original)
            for plugin, previous_repo in previous:
                plugin.repo = previous_repo
            raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        if not local["repo"]:
            local["repo"] = repo_url
            local["runtime_metadata"]["repo"] = repo_url
        if written or any(not previous_repo for _, previous_repo in previous):
            record["metadata_snapshot"]["repo"] = local["repo"]

    async def _activate(
        self, record: dict, binding: dict, owner: str, *, action="source_repaired"
    ) -> None:
        records = await self.operations.web.get_plugin_install_sources()
        before_records = copy.deepcopy(records)
        local = record["local"]
        current = self._core(local, records)
        if fingerprint(current) != record["core_revision"]:
            raise ValueError("AstrBot 来源记录已变化，请重新校验。")
        core = {
            "schema_version": 1,
            "root_dir_name": local["directory"],
            "name": local["name"],
            "author": local["author"],
            "install_method": binding["kind"],
            "repo": binding["repo_url"],
            "registry_url": binding["registry_url"],
            "registry_name": await self.operations.web.resolve_registry_name(
                binding["registry_url"]
            )
            if binding["kind"] == "market"
            else "Repository",
            "market_plugin_id": binding["market_plugin_id"],
            "download_url": "",
            "updated_at": now(),
        }
        if current and current.get("installed_at"):
            core["installed_at"] = current["installed_at"]
        if local["name"] != local["directory"] and (
            (records.get(local["name"]) or {}).get("root_dir_name")
            == local["directory"]
            or self._core(local, {local["name"]: records.get(local["name"])})
            is not None
        ):
            records.pop(local["name"], None)
        records[local["directory"]] = core
        await self.operations.web.save_plugin_install_sources(records)
        try:
            self._complete_repository(record, binding["repo_url"])
        except Exception:
            await self.operations.web.save_plugin_install_sources(before_records)
            raise
        record["last_core_revision"] = fingerprint(core)
        record["source_validation"] = {
            "id": binding["id"],
            "author": local["author"],
            "checked_at": binding["checked_at"],
        }
        record["auto_binding_notice"] = ""
        self._event(record, action, owner, binding["id"])

    def _binding_market(self) -> _BindingMarket:
        source = self.operations.web.build_registry_source(None)
        return _BindingMarket(self.operations.web.load_plugin_cache(source.cache_file))

    async def _refresh_binding_market(self, market: _BindingMarket) -> None:
        if not market.refreshed:
            market.refreshed = True
            try:
                data, notice = await self.official_market(fresh=True)
                if notice:
                    raise ValueError("市场联网校验失败，请稍后在来源详情中重试。")
                market.data = data
            except Exception as exc:
                market.error = exc
        if market.error is not None:
            raise market.error

    async def _discover_binding(
        self, record: dict, market: _BindingMarket | None = None
    ) -> dict | None:
        """Match cached market entries, refresh mismatches, then inspect unmatched repos."""
        self._editable(record)
        local = record["local"]
        repo = repository_url(local["repo"]) if local["repo"] else ""
        if market is None:
            market = self._binding_market()
        for attempt in range(2):
            if attempt:
                await self._refresh_binding_market(market)
            try:
                identities = [
                    (identifier, metadata)
                    for identifier, metadata in self.operations.web.iter_market_plugin_entries(
                        market.data
                    )
                    if metadata.get("name") == local["name"]
                    and authors_match(
                        local["author"], metadata.get("author"), market=True
                    )
                ]
                matches = [
                    (identifier, metadata)
                    for identifier, metadata in identities
                    if not repo or same_repository(repo, metadata.get("repo"))
                ]
                if len(matches) > 1:
                    raise ValueError("市场中有多个匹配条目，请在官方插件页面选择来源。")
                if identities and not matches:
                    raise ValueError(
                        "市场条目的仓库地址与本机不一致，请在来源详情中检查。"
                    )
                binding = None
                if matches:
                    identifier, metadata = matches[0]
                    if not is_market_id(identifier):
                        raise ValueError("市场条目的插件 ID 无效，拒绝自动绑定。")
                    binding = {
                        "kind": "market",
                        "market_plugin_id": identifier,
                        "registry_url": None,
                        "repo_url": repository_url(metadata.get("repo")),
                    }
            except ValueError:
                if market.refreshed and market.error is None:
                    raise
            else:
                if binding is not None or (market.refreshed and market.error is None):
                    break
        if binding is None and repo:
            metadata = await asyncio.wait_for(
                self.operations.manager.inspect_plugin_repository(repo),
                timeout=MARKET_QUERY_TIMEOUT_SECONDS,
            )
            if metadata.get("name") != local["name"] or not authors_match(
                local["author"], metadata.get("author"), market=False
            ):
                raise ValueError("仓库中的作者或插件名与本机不一致，拒绝自动绑定。")
            binding = {
                "kind": "repository",
                "market_plugin_id": "",
                "registry_url": None,
                "repo_url": repo,
            }
        elif binding is None:
            return None
        binding["id"] = fingerprint(binding)[:24]
        binding.update(verification="identity_matched", checked_at=now())
        return binding

    async def bind_restored(self, directory: str, actor: str) -> list[str]:
        """Verify and bind a restored source while the transfer and core locks are held."""
        preview = None
        try:
            record = await self.detail(directory)
            if record["current_source"]:
                return []
            if record["pending_source"]:
                preview = await self.preview_existing(
                    actor, directory, record["revision"]
                )
            else:
                binding = await self._discover_binding(record)
                current = await self.detail(directory)
                if current["revision"] != record["revision"]:
                    raise ValueError("插件身份或来源已变化，请在来源详情中重新校验。")
                if binding is None:
                    current["auto_binding_notice"] = (
                        "来源未自动绑定：市场中未找到匹配条目，插件也未声明仓库地址。"
                    )
                    await self._save_record(current)
                    return []
                await self._activate(current, binding, actor)
                await self._save_record(current)
                return []
            record = await self.detail(directory)
            if record["revision"] != self.previews[preview["id"]]["revision"]:
                raise ValueError("插件身份或来源已变化，请在来源详情中重新校验。")
            await self._activate(record, preview["binding"], actor)
            record["pending_source"] = None
            await self._save_record(record)
            return []
        except Exception as exc:
            return [
                f"{directory}：来源未自动绑定，{redact(str(exc)) or '请在来源详情中检查。'}"
            ]
        finally:
            if preview:
                self.previews.pop(preview["id"], None)

    @staticmethod
    def _stored_record(record: dict) -> dict:
        return {
            key: copy.deepcopy(record[key])
            for key in (
                "record_id",
                "identity",
                "provenance",
                "pending_source",
                "source_validation",
                "history",
                "last_core_revision",
                "metadata_snapshot",
                "auto_binding_notice",
            )
            if key in record
        }

    async def _save_record(self, record: dict) -> None:
        async with self.record_lock:
            store = await asyncio.to_thread(self._store)
            directory = record["local"]["directory"]
            previous = store["plugins"].get(directory)
            if previous and previous["identity"] != record["identity"]:
                store["archived"] = [*store["archived"], previous][-100:]
            store["plugins"][directory] = self._stored_record(record)
            await asyncio.to_thread(write_json, self.path, store)

    async def source_snapshot(self, directory: str, name: str) -> dict:
        store = await asyncio.to_thread(self._store)
        records = await self.operations.web.get_plugin_install_sources()
        keys = {directory, name}
        return {
            "directory": directory,
            "name": name,
            "record": store["plugins"].get(directory),
            "core": {
                key: records.get(key)
                for key in keys
                if key == directory
                or (records.get(key) or {}).get("root_dir_name") == directory
                or self._core(
                    {"directory": directory, "name": name}, {key: records.get(key)}
                )
                is not None
            },
        }

    async def restore_sources(self, snapshot: dict) -> None:
        async with self.record_lock:
            store = await asyncio.to_thread(self._store)
            if snapshot["record"] is None:
                store["plugins"].pop(snapshot["directory"], None)
            else:
                store["plugins"][snapshot["directory"]] = snapshot["record"]
            await asyncio.to_thread(write_json, self.path, store)
        records = await self.operations.web.get_plugin_install_sources()
        for key, value in snapshot["core"].items():
            if value is None:
                records.pop(key, None)
            else:
                records[key] = value
        await self.operations.web.save_plugin_install_sources(records)

    async def export_sources(
        self, directory: str, metadata: dict
    ) -> tuple[dict, list[str]]:
        record = await self.detail(directory)
        if any(
            record["local"][field] != metadata[field] for field in ("name", "author")
        ):
            raise ValueError("快照期间插件来源身份变化，请重新导出。")
        return self.portable_sources(record)

    def portable_sources(self, record: dict) -> tuple[dict, list[str]]:
        """Export a previously read record without repeating inventory scans."""
        directory = record["local"]["directory"]
        warnings = []

        def portable(binding):
            try:
                return portable_binding(binding)
            except ValueError:
                warnings.append(f"{directory}：一条来源地址无法安全迁移，已省略。")
                if (
                    isinstance(binding, dict)
                    and binding.get("kind") == "market"
                    and binding.get("repo_url")
                ):
                    try:
                        return portable_binding({**binding, "repo_url": ""})
                    except ValueError:
                        pass
                return None

        provenance = record["provenance"]
        data = {
            "schema_version": 1,
            "identity": {field: record["local"][field] for field in ("name", "author")},
            "provenance": {
                "method": provenance["method"]
                if provenance["method"] in METHODS
                else "unknown",
                **{
                    field: str(provenance[field])
                    if provenance.get(field) is not None
                    else None
                    for field in (
                        "installed_at",
                        "observed_at",
                        "reported_installed_at",
                    )
                },
                "source": portable(provenance.get("source")),
            },
            "source": portable(record["current_source"] or record["pending_source"]),
        }
        return validate_sources(data, record["local"]), warnings

    async def note_bundle(self, directory: str, actor: str, incoming: dict) -> None:
        record = await self.detail(directory)
        validate_sources(incoming, record["local"])
        records = await self.operations.web.get_plugin_install_sources()
        records.pop(directory, None)
        name = record["local"]["name"]
        if name != directory and (
            (records.get(name) or {}).get("root_dir_name") == directory
            or self._core(record["local"], {name: records.get(name)}) is not None
        ):
            records.pop(name, None)
        await self.operations.web.save_plugin_install_sources(records)
        record["provenance"] = {
            **copy.deepcopy(incoming["provenance"]),
            "evidence": "bundle_record",
        }
        record["identity"] = self._identity(record["local"])
        record["metadata_snapshot"] = {
            field: record["local"][field] for field in METADATA_FIELDS
        }
        record["pending_source"] = copy.deepcopy(incoming["source"])
        record["last_core_revision"] = fingerprint(None)
        self._event(record, "bundle_imported", actor)
        self._event(record, "bundle_sources_imported", actor)
        await self._save_record(record)

    async def observe_install(self, directory: str, method: str, actor: str) -> None:
        record = await self.detail(directory)
        record["provenance"] = {
            "method": method,
            "evidence": "manager_operation",
            "installed_at": now(),
            "observed_at": now(),
            "source": record["current_source"],
        }
        self._event(
            record, "installed", actor, (record["current_source"] or {}).get("id", "")
        )
        await self._save_record(record)

    async def retire(self, directory: str, actor: str) -> None:
        """Archive a confirmed uninstall so later installations start a new record."""
        async with self.record_lock:
            store = await asyncio.to_thread(self._store)
            previous = store["plugins"].pop(directory, None)
            if previous:
                self._event(previous, "uninstalled", actor)
                store["archived"] = [*store["archived"], previous][-100:]
                await asyncio.to_thread(write_json, self.path, store)

    async def describe(self, directory: str) -> str:
        """Summarize provenance and AstrBot's current source for a directory."""
        record = await self.detail(directory)
        active = record["current_source"]
        provenance = record["provenance"]
        return redact(
            "\n".join(
                [
                    f"安装来源：{SOURCE_LABELS.get(provenance['method'], provenance['method'])}",
                    "记录依据："
                    + (
                        "管家安装记录"
                        if provenance["evidence"] == "manager_operation"
                        else "首次观察到的 AstrBot 记录，不能证明最初安装来源"
                        if provenance["evidence"] == "core_record_observed"
                        else "备份中的安装来源记录，仅供参考；更新源须重新校验"
                        if provenance["evidence"] == "bundle_record"
                        else "缺少真实安装记录"
                    ),
                    "当前更新源："
                    + (
                        (active["market_plugin_id"] or active["repo_url"])
                        if active
                        else "未绑定"
                    ),
                    "来源状态："
                    + (
                        "跟随 AstrBot"
                        if active
                        else "备份来源待校验"
                        if record["pending_source"]
                        else "缺少更新源"
                    ),
                ]
            )
        )
