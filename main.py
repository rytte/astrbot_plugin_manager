"""A single administrator-only entry point for plugin management."""

from __future__ import annotations

import asyncio
import json
import shlex
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr
from astrbot.dashboard.services.plugin_service import (
    PluginServiceError,
    PluginServiceWarning,
)

from .loading import PluginLoadService
from .management_web import ManagementWeb
from .manifests import ManifestService
from .service import INSTALL_USAGE, PluginOperations, PluginTarget
from .sources import SourceService
from .transfer_web import TransferWeb
from .transfers import TransferService
from .updates import UpdateService

USAGE = (
    "插件管家\n"
    "查看帮助   /pm help\n"
    "插件列表   /pm list\n"
    "安装插件   /pm add <仓库地址或市场ID>\n"
    "卸载插件   /pm remove <插件名> [--all]\n"
    "插件信息   /pm info <插件名>\n"
    "启用插件   /pm enable <插件名>\n"
    "停用插件   /pm disable <插件名>\n"
    "重载插件   /pm reload <插件名>\n"
    "更新插件   /pm update <插件名>\n"
    "检查更新   /pm check-update\n"
    "刷新市场   /pm refresh-market\n"
    "搜索插件   /pm search <关键词> [--page <页码>]\n"
    "\n"
    "市场 ID 为：作者/插件名\n"
    "--all 参数会同时删除配置与数据"
)
CONFIRM_SECONDS = 60


@dataclass(frozen=True)
class PendingRemoval:
    expires: float
    target: PluginTarget
    delete_all: bool


class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        unknown = set(config) - {"allow_prefix_omission"}
        if unknown:
            raise ValueError("未知插件管家配置项：" + "、".join(sorted(unknown)))
        if "allow_prefix_omission" not in config:
            raise ValueError("缺少配置项 allow_prefix_omission，请检查插件配置。")
        allow_prefix_omission = config["allow_prefix_omission"]
        if not isinstance(allow_prefix_omission, bool):
            raise ValueError("配置项 allow_prefix_omission 必须是布尔值。")
        self.config = config
        self.service = PluginOperations(
            context,
            self.__class__.__module__,
            allow_prefix_omission=allow_prefix_omission,
        )
        self.ready = False
        self._operations = asyncio.Lock()
        self._removals: dict[tuple[str, str], PendingRemoval] = {}
        self.transfers = TransferService(self.service, self._operations)
        self.transfer_web = TransferWeb(context, self.transfers)
        self.sources = SourceService(self.service, self.transfers)
        self.service.sources = self.sources
        self.manifests = ManifestService(self.sources, self.transfers)
        self.updates = UpdateService(self.sources, self.transfers)
        self.loading = PluginLoadService(self.sources, self.transfers)
        self.chat_updates = UpdateService(self.sources, self.transfers)
        self.management_web = ManagementWeb(
            context,
            self.transfers,
            self.sources,
            self.manifests,
            self.updates,
            self.loading,
        )

    async def initialize(self) -> None:
        """Reject conflicting command names before serving requests."""
        conflicts = self.service.command_conflicts()
        if conflicts:
            raise RuntimeError("插件管家命令冲突：" + "、".join(conflicts))
        await self.transfers.initialize()
        await self.sources.initialize()
        self.transfer_web.register()
        self.management_web.register()
        self.ready = True

    @filter.on_plugin_loaded()
    async def on_plugin_loaded(self, metadata) -> None:
        if self.ready and not metadata.reserved and not self.transfers.busy:
            self.sources.schedule_binding(metadata.root_dir_name)

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        if self.ready:
            await self.sources.schedule_missing_bindings()

    async def reply(self, event: AstrMessageEvent, text: str) -> None:
        """Send plain text through AstrBot's normal response pipeline.

        Args:
            event: The authenticated event receiving the response.
            text: Public status or usage information.
        """
        event.should_call_llm(False)
        event.set_result(event.plain_result(text).use_t2i(False).use_markdown(False))

    async def authorize(self, event: AstrMessageEvent) -> bool:
        """Check live permissions and conflicts before parsing arguments.

        Args:
            event: The real caller context, including API role restrictions.

        Returns:
            Whether this request may access plugin information or operations.
        """
        if (
            not event.is_admin()
            or event.get_extra("_api_key_allow_admin_role") is False
        ):
            await self.reply(event, "插件管家仅限 AstrBot 管理员使用。")
            return False
        if not self.ready:
            await self.reply(event, "插件管家尚未完成初始化，暂不可用。")
            return False
        conflicts = self.service.command_conflicts()
        if conflicts:
            await self.reply(event, "插件管家命令冲突：" + "、".join(conflicts))
            return False
        return True

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command(
        "pm",
        desc="管理员插件管理：list/search/refresh-market/help/info/add/check-update/update/enable/disable/reload/remove。",
    )
    async def plugin(
        self, event: AstrMessageEvent, arguments: GreedyStr
    ) -> AsyncIterator[None]:
        """Dispatch strict subcommands from one protected command.

        Args:
            event: The platform event initiating the request.
            arguments: A subcommand followed by its explicit arguments.
        """
        action = None
        try:
            if not await self.authorize(event):
                yield
                return
            tokens = arguments.split()
            if not tokens or tokens[0] == "help":
                if len(tokens) > 1:
                    raise ValueError(
                        "用法：/pm help；查看插件详情请使用 /pm info <插件名或目录名>。"
                    )
                await self.reply(event, USAGE)
                yield
                return
            action, args = tokens[0], tokens[1:]
            if action == "list":
                if args:
                    raise ValueError("用法：/pm list（不接受页码或其他参数）。")
                await self.reply(event, self.service.list_plugins())
                yield
                return
            if action == "info":
                if len(args) != 1:
                    raise ValueError("用法：/pm info <插件名或目录名>。")
                await self.reply(event, await self.service.plugin_info(args[0]))
                yield
                return
            if action == "search":
                usage = "用法：/pm search <关键词> [--page <页码>]；页码必须为正整数。"
                page = 1
                if "--page" in args:
                    if (
                        len(args) < 3
                        or args[-2] != "--page"
                        or args.count("--page") != 1
                        or not args[-1].isascii()
                        or not args[-1].isdecimal()
                    ):
                        raise ValueError(usage)
                    try:
                        page = int(args[-1])
                    except ValueError:
                        raise ValueError(usage) from None
                    args = args[:-2]
                if not args or page < 1 or any(arg.startswith("--") for arg in args):
                    raise ValueError(usage)
                await self.reply(event, "正在查询官方插件市场，请稍候。")
                yield
                await self.reply(
                    event, await self.service.search_market(" ".join(args), page)
                )
                yield
                return
            if action == "refresh-market":
                if args:
                    raise ValueError("用法：/pm refresh-market（不接受其他参数）。")
                await self.reply(event, "正在刷新官方插件市场，请稍候。")
                yield
                await self.reply(event, await self.service.refresh_market())
                yield
                return
            if action in {"check-update", "update"}:
                if action == "check-update" and args:
                    raise ValueError("用法：/pm check-update（不接受其他参数）。")
                if action == "update" and (len(args) != 1 or args[0].startswith("--")):
                    raise ValueError("用法：/pm update <插件名或目录名>。")
                owner = "chat:" + json.dumps(
                    [event.unified_msg_origin, event.get_sender_id()],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                if action == "check-update":
                    await self.reply(event, "正在按各插件的当前来源检查更新，请稍候。")
                    yield
                    await self.reply(event, await self.chat_updates.check_all(owner))
                else:
                    started = await self.chat_updates.start_current_update(
                        owner, args[0]
                    )
                    job = self.transfers.get_job(started["id"], owner)
                    task = self.transfers.task
                    name = job["update_plan"]["_record"]["local"]["name"]
                    self.logger.info(
                        "Plugin update by %s in %s: %s",
                        event.get_sender_id(),
                        event.unified_msg_origin,
                        name,
                    )
                    await self.reply(event, f"正在检查并更新插件 {name}，请稍候。")
                    yield
                    await asyncio.shield(task)
                    if job["status"] not in {"completed", "completed_with_warning"}:
                        raise ValueError(
                            f"插件 {name} 更新未完成："
                            + (job["error"] or "请检查插件状态。")
                        )
                    await self.reply(
                        event, f"插件 {name}：{job['results'][0]['message']}"
                    )
                yield
                return
            if action not in {"add", "enable", "disable", "reload", "remove"}:
                raise ValueError("未知子命令，请使用 /pm help 查看用法。")
            async with self._operations:
                self.service.ensure_writable()
                if action == "remove":
                    async for result in self.remove(event, args):
                        yield result
                    return
                if action == "add":
                    try:
                        args = shlex.split(arguments)[1:]
                    except ValueError:
                        raise ValueError(INSTALL_USAGE) from None
                if len(args) != 1 or args[0].startswith("--"):
                    raise ValueError(
                        INSTALL_USAGE
                        if action == "add"
                        else f"用法：/pm {action} <插件名或目录名>。"
                    )
                if action == "add":
                    await self.reply(event, "正在安装插件，请稍候。")
                    yield
                    message = await self.service.install(args[0])
                else:
                    target = self.service.resolve(args[0])
                    self.service.check_operation(target, action)
                    self.logger.info(
                        "Plugin operation %s by %s in %s: %s",
                        action,
                        event.get_sender_id(),
                        event.unified_msg_origin,
                        target.name,
                    )
                    if action in {"enable", "disable"} and target.plugin.activated == (
                        action == "enable"
                    ):
                        await self.reply(
                            event,
                            f"插件 {target.name} 已处于{'启用' if action == 'enable' else '停用'}状态。",
                        )
                        yield
                        return
                    verb = {"enable": "启用", "disable": "停用", "reload": "重载"}[
                        action
                    ]
                    await self.reply(event, f"正在{verb}插件 {target.name}。")
                    yield
                    if action == "disable":
                        message = await self.service.disable(target)
                    else:
                        message = await self.service.reload(
                            target, enable=action == "enable"
                        )
                await self.reply(event, message)
                yield
        except ValueError as exc:
            await self.reply(event, str(exc))
            yield
        except (PluginServiceError, PluginServiceWarning) as exc:
            self.logger.exception("Plugin management service rejected an operation.")
            await self.reply(
                event,
                f"市场{'刷新' if action == 'refresh-market' else '查询'}未完成：{exc.public_message}。"
                if action in {"search", "refresh-market"}
                else f"操作未完成：{exc.public_message}。请检查 WebUI 中的插件状态。",
            )
            yield
        except Exception:
            self.logger.exception("Plugin management operation failed.")
            await self.reply(
                event,
                "查询失败，请检查 AstrBot 日志。"
                if action
                in {
                    "list",
                    "info",
                    "search",
                    "refresh-market",
                    "check-update",
                }
                else "操作失败，可能已发生部分变更；请检查 AstrBot 日志，并通过 WebUI 检查或恢复插件。",
            )
            yield
        finally:
            event.stop_event()

    async def remove(
        self, event: AstrMessageEvent, args: list[str]
    ) -> AsyncIterator[None]:
        """Require a bound, expiring confirmation before uninstalling.

        Args:
            event: The administrator's group or private conversation.
            args: The exact target, deletion scope and optional confirmation word.
        """
        if not args or args[1:] not in (
            [],
            ["--all"],
            ["confirm"],
            ["--all", "confirm"],
        ):
            raise ValueError(
                "用法：/pm remove <插件名或目录名> [--all]；确认时追加 confirm。"
            )
        target = self.service.resolve(args[0])
        self.service.check_operation(target, "remove")
        self.service.validated_directory(target)
        delete_all = "--all" in args[1:]
        confirming = len(args) > 1 and args[-1] == "confirm"
        key = (event.unified_msg_origin, event.get_sender_id())
        now = time.monotonic()
        if not confirming:
            self._removals = {
                key: pending
                for key, pending in self._removals.items()
                if pending.expires > now
            }
            self._removals[key] = PendingRemoval(
                now + CONFIRM_SECONDS, target, delete_all
            )
            scope = (
                "将删除插件文件、配置和数据。"
                if delete_all
                else "将删除插件文件，保留配置和数据。"
            )
            option = " --all" if delete_all else ""
            await self.reply(
                event,
                f"确认卸载 {target.name}？{scope}请在当前会话中于 60 秒内发送 /pm remove {target.identifier}{option} confirm。",
            )
            yield
            return
        pending = self._removals.pop(key, None)
        if (
            pending is None
            or now >= pending.expires
            or delete_all != pending.delete_all
            or not pending.target.same_as(target)
        ):
            raise ValueError("卸载确认已过期或插件状态已变化，请重新发送命令。")
        self.logger.info(
            "Plugin removal by %s in %s: %s, delete_all=%s",
            event.get_sender_id(),
            event.unified_msg_origin,
            target.name,
            delete_all,
        )
        await self.reply(event, f"正在卸载插件 {target.name}。")
        yield
        await self.reply(event, await self.service.uninstall(target, delete_all))
        yield

    async def terminate(self) -> None:
        """Invalidate pending requests and confirmations when unloading."""
        self.ready = False
        self._removals.clear()
        await self.sources.close()
        await self.chat_updates.close()
        await self.management_web.close()
        await self.transfer_web.close()
