"""Portable source references, excluding credentials and inherited trust."""

from __future__ import annotations

import hashlib
import json
from urllib.parse import urlsplit, urlunsplit

from astrbot.core.repository import normalize_repository_url, parse_repository_url

from .service import is_market_id

METHODS = {"market", "repository", "upload", "bundle", "unknown", "builtin"}
BINDING_FIELDS = {"id", "kind", "repo_url", "registry_url", "market_plugin_id"}
PROVENANCE_FIELDS = {
    "method",
    "installed_at",
    "observed_at",
    "reported_installed_at",
    "source",
}


def _keys(value, expected: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("来源快照字段缺失、未知或格式不正确。")


def canonical_repository_url(value: str) -> str:
    """Omit GitHub clone suffixes without rewriting branches or Git transports."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "github.com",
            "www.github.com",
        }:
            return value
        parse_repository_url(value)
    except ValueError:
        return value
    parts = [part for part in parsed.path.split("/") if part]
    parts[1] = parts[1].removesuffix(".git")
    return urlunsplit(parsed._replace(path="/" + "/".join(parts)))


def _url(value: str, *, repository=False, sanitize=False) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("来源地址无效。")
    if sanitize:
        parsed = urlsplit(value)
        if parsed.scheme == "https" and parsed.hostname:
            host = parsed.hostname
            if ":" in host:
                host = f"[{host}]"
            if parsed.port is not None:
                host += f":{parsed.port}"
            value = urlunsplit(("https", host, parsed.path, "", ""))
        elif repository:
            value = normalize_repository_url(value)
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ValueError("来源快照仅接受不含凭据的 HTTPS 地址。")
    _ = parsed.port
    if repository:
        parse_repository_url(value)
    return value


def portable_binding(value: dict | None, *, canonicalize: bool = True) -> dict | None:
    """Normalize locators and deliberately omit all validation/actor fields."""
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("kind"), str)
        or value["kind"] not in {"market", "repository"}
    ):
        return None
    kind = value["kind"]
    market_id = value.get("market_plugin_id", "") if kind == "market" else ""
    if not isinstance(market_id, str) or len(market_id) > 1024:
        raise ValueError("来源市场 ID 无效。")
    if kind == "market" and not is_market_id(market_id):
        raise ValueError("来源市场 ID 无效。")
    repo = value.get("repo_url") or ""
    registry = value.get("registry_url") if kind == "market" else None
    if repo:
        repo = _url(repo, repository=True, sanitize=True)
        if canonicalize:
            repo = canonical_repository_url(repo)
    if kind == "repository" and not repo:
        raise ValueError("仓库来源缺少地址。")
    if registry is not None:
        registry = _url(registry, sanitize=True)
    result = {
        "kind": kind,
        "repo_url": repo,
        "registry_url": registry,
        "market_plugin_id": market_id,
    }
    result["id"] = hashlib.sha256(
        json.dumps(result, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:24]
    return result


def _binding(value) -> None:
    _keys(value, BINDING_FIELDS)
    if not isinstance(value["kind"], str) or value["kind"] not in {
        "market",
        "repository",
    }:
        raise ValueError("来源类型无效。")
    if (
        not isinstance(value["market_plugin_id"], str)
        or len(value["market_plugin_id"]) > 1024
    ):
        raise ValueError("来源市场 ID 无效。")
    if value["kind"] == "market":
        if not is_market_id(value["market_plugin_id"]):
            raise ValueError("来源市场 ID 无效。")
    elif value["market_plugin_id"] or value["registry_url"] is not None:
        raise ValueError("仓库来源含市场字段。")
    if value["repo_url"] or value["kind"] == "repository":
        _url(value["repo_url"], repository=True)
    elif value["repo_url"] != "":
        raise ValueError("来源仓库地址无效。")
    if value["registry_url"] is not None:
        _url(value["registry_url"])
    if portable_binding(value, canonicalize=False) != value:
        raise ValueError("来源标识与地址不一致。")


def validate_source(source) -> None:
    """Validate one portable source without importing verification state."""
    if source is not None:
        _binding(source)


def validate_sources(value, identity: dict) -> dict:
    """Validate a closed schema before a bundle can modify local source records."""
    _keys(
        value,
        {"schema_version", "identity", "provenance", "source"},
    )
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("来源快照版本不受支持。")
    _keys(value["identity"], {"name", "author"})
    if value["identity"] != {key: identity[key] for key in ("name", "author")}:
        raise ValueError("来源快照与插件身份不一致。")
    provenance = value["provenance"]
    _keys(provenance, PROVENANCE_FIELDS)
    if not isinstance(provenance["method"], str) or provenance["method"] not in METHODS:
        raise ValueError("安装来源类型无效。")
    for field in ("installed_at", "observed_at", "reported_installed_at"):
        timestamp = provenance[field]
        if timestamp is not None and (
            not isinstance(timestamp, str) or len(timestamp) > 128
        ):
            raise ValueError("来源记录时间无效。")
    if provenance["source"] is not None:
        _binding(provenance["source"])
    validate_source(value["source"])
    return value
