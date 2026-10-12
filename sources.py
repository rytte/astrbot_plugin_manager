"""Local provenance and multiple bindings, with AstrBot owning the active source."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import yaml
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


class SourceService:
    """Preserve observations without inventing installation history or trust."""

    def __init__(self, operations, transfers) -> None:
        self.operations = operations
        self.transfers = transfers
        self.root = Path(operations.manager.plugin_store_path).absolute()
        self.path = safe_path(transfers.root.parent, "sources.json")
        self.previews = {}
        self.record_lock = asyncio.Lock()

    async def initialize(self) -> None:
        await self.inventory()

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
                "bindings": [],
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
        if active and not any(item["id"] == active["id"] for item in saved["bindings"]):
            saved["bindings"].append(active)
        if saved["identity"]["author"] != local["author"]:
            for binding in saved["bindings"]:
                binding["verification"] = "metadata_changed"
        saved.update(
            local=local,
            active_binding_id=active["id"] if active else None,
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
        self, owner: str, directory: str, kind: str, value: str
    ) -> dict:
        record = await self.detail(directory)
        self._editable(record)
        local = record["local"]
        if record["identity"]["author"] != local["author"]:
            raise ValueError("作者信息已变更，请先点击「更新信息」，再校验来源。")
        current = next(
            (
                item
                for item in record["bindings"]
                if item["id"] == record["active_binding_id"]
            ),
            None,
        )
        notice = None
        if kind == "market":
            if not is_market_id(value):
                raise ValueError("请填写完整的作者/插件技术名。")
            source = self.operations.web.build_registry_source(None)
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
                    data, notice = await self.official_market(fresh=True)
                    if notice:
                        raise ValueError(
                            "官方市场联网校验失败，当前只能取得旧缓存，请稍后重试。"
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
                        "registry_url": None,
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
                        "仓库地址与当前来源不同，不能作为同一插件的关联来源。"
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

    async def bind(
        self, owner: str, identifier: str, activate: bool, acknowledged: bool
    ) -> dict:
        self.operations.ensure_writable()
        if type(activate) is not bool or acknowledged is not True:
            raise ValueError("请确认来源校验结果，并选择是否作为更新源。")
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
            record["bindings"] = [
                item for item in record["bindings"] if item["id"] != binding["id"]
            ] + [binding]
            self._event(record, "binding_added", owner, binding["id"])
            # Save the original observation before replacing AstrBot's active record.
            await self._save_record(record)
            if activate:
                await self._activate(record, binding, owner)
            await self._save_record(record)
            self.previews.pop(identifier, None)
        return await self.detail(preview["directory"])

    async def select(
        self, owner: str, directory: str, binding_id: str, revision: str
    ) -> dict:
        self.operations.ensure_writable()
        self.transfers._ensure_idle()
        record = await self.detail(directory)
        binding = next(
            (item for item in record["bindings"] if item["id"] == binding_id), None
        )
        if record["revision"] != revision or not binding:
            raise ValueError("来源记录已变化，请刷新后重试。")
        if binding["kind"] == "market" and binding["registry_url"] is not None:
            raise ValueError(
                "自定义市场关联暂不支持在此切换，请使用 AstrBot 原生页面。"
            )
        # Recheck every switch, including a binding restored from old observations.
        preview = await self.preview_binding(
            owner,
            directory,
            binding["kind"],
            binding["market_plugin_id"]
            if binding["kind"] == "market"
            else binding["repo_url"],
        )
        return await self.bind(owner, preview["id"], True, True)

    @staticmethod
    def _event(record: dict, action: str, actor: str, binding_id: str = "") -> None:
        record["history"] = [
            *record["history"],
            {"at": now(), "action": action, "actor": actor, "binding_id": binding_id},
        ][-100:]

    async def _activate(self, record: dict, binding: dict, owner: str) -> None:
        records = await self.operations.web.get_plugin_install_sources()
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
            "registry_name": "Default" if binding["kind"] == "market" else "Repository",
            "market_plugin_id": binding["market_plugin_id"],
            "download_url": "",
            "updated_at": now(),
        }
        if current and current.get("installed_at"):
            core["installed_at"] = current["installed_at"]
        records[local["directory"]] = core
        await self.operations.web.save_plugin_install_sources(records)
        record["last_core_revision"] = fingerprint(core)
        for item in record["bindings"]:
            item.pop("imported_active", None)
        self._event(record, "active_changed", owner, binding["id"])

    @staticmethod
    def _stored_record(record: dict) -> dict:
        return {
            key: copy.deepcopy(record[key])
            for key in (
                "record_id",
                "identity",
                "provenance",
                "bindings",
                "history",
                "last_core_revision",
                "metadata_snapshot",
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

        bindings = {}
        active_id = None
        for binding in record["bindings"]:
            entry = portable(binding)
            if entry is None:
                continue
            bindings[entry["id"]] = entry
            if binding["id"] == record["active_binding_id"] or (
                not record["active_binding_id"] and binding.get("imported_active")
            ):
                active_id = entry["id"]
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
            "bindings": list(bindings.values()),
            "active_binding_id": active_id,
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
        record["bindings"] = [
            {
                **copy.deepcopy(binding),
                "verification": "imported",
                "checked_at": None,
                "imported_active": binding["id"] == incoming["active_binding_id"],
            }
            for binding in incoming["bindings"]
        ]
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
            "source": next(
                (
                    item
                    for item in record["bindings"]
                    if item["id"] == record["active_binding_id"]
                ),
                None,
            ),
        }
        self._event(record, "installed", actor, record["active_binding_id"] or "")
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
        """Summarize recorded provenance and bindings for a resolved directory."""
        record = await self.detail(directory)
        active = next(
            (
                item
                for item in record["bindings"]
                if item["id"] == record["active_binding_id"]
            ),
            None,
        )
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
                        else "备份中的安装来源记录，仅供参考；关联来源须重新校验"
                        if provenance["evidence"] == "bundle_record"
                        else "缺少真实安装记录"
                    ),
                    "当前更新源："
                    + (
                        (active["market_plugin_id"] or active["repo_url"])
                        if active
                        else "未绑定"
                    ),
                    f"已关联来源：{len(record['bindings'])} 个",
                ]
            )
        )
