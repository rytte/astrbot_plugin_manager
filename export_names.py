"""Display names and portable filenames for user-created exports."""

from __future__ import annotations

from .transfer_bundle import component

MAX_EXPORT_NAME = 80


def export_name(value: str) -> str:
    """Validate an optional filename stem without silently replacing characters."""
    if not isinstance(value, str):
        raise ValueError("导出名称必须是字符串。")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("导出名称不能包含控制字符。")
    value = value.strip()
    if not value:
        return ""
    if len(value) > MAX_EXPORT_NAME:
        raise ValueError(f"导出名称不能超过 {MAX_EXPORT_NAME} 个字符。")
    try:
        component(value)
    except ValueError as exc:
        raise ValueError(
            '导出名称不能包含路径、\\ / : * ? " < > |、保留文件名或以句点结尾。'
        ) from exc
    return value


def export_filename(job: dict, kind: str) -> str:
    """Keep the download label separate from fixed, private storage paths."""
    if kind == "manifest":
        return (export_name(job.get("name", "")) or "plugins") + ".json"
    if kind == "package":
        return (
            export_name(job.get("name", "")) or f"astrbot-package-{job['id'][:8]}"
        ) + ".zip"
    if kind == "rollback":
        return f"astrbot-rollback-{job['id'][:8]}.zip"
    raise ValueError("下载类型无效。")
