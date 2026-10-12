"""Exercise real AstrBot registries, event permissions and response pipelines."""

import asyncio
import copy
import functools
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
ASTRBOT_SOURCE = Path(os.environ.get("ASTRBOT_SOURCE", PLUGIN_DIR.parent / "AstrBot"))
sys.path.insert(0, str(ASTRBOT_SOURCE))
sys.path.insert(0, str(PLUGIN_DIR.parent))
os.environ["ASTRBOT_ROOT"] = tempfile.mkdtemp(prefix="plugin-manager-tests-")

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Node, Nodes, Plain
from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.pipeline.context import PipelineContext, call_handler
from astrbot.core.pipeline.process_stage.stage import ProcessStage, StarRequestSubStage
from astrbot.core.pipeline.respond.stage import RespondStage
from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
from astrbot.core.pipeline.scheduler import PipelineScheduler
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.star.context import Context
from astrbot.core.star.star import StarMetadata
from astrbot.core.star.star_handler import StarHandlerRegistry, star_handlers_registry
from astrbot.dashboard.services.plugin_service import PluginService

main = importlib.import_module("astrbot_plugin_manager.main")
service_module = importlib.import_module("astrbot_plugin_manager.service")
catalog_module = importlib.import_module("astrbot_plugin_manager.catalog")
ORIGINAL_HANDLERS = [
    item for item in star_handlers_registry if item.handler_module_path == main.__name__
]


def message_text(components):
    """Collect text from plain and forwarded message components."""
    parts = []
    for component in components:
        if isinstance(component, Plain):
            parts.append(component.text)
        elif isinstance(component, Node):
            parts.append(message_text(component.content))
        elif isinstance(component, Nodes):
            parts.append(message_text(component.nodes))
    return "".join(parts)


@pytest.fixture
async def env(monkeypatch, tmp_path, request):
    """Isolate all lifecycle side effects in temporary directories."""
    registry = StarHandlerRegistry()
    metadata = StarMetadata(
        name="astrbot_plugin_manager",
        module_path=main.__name__,
        root_dir_name="astrbot_plugin_manager",
        activated=True,
    )
    owners = {main.__name__: metadata}
    for module_name in (
        "astrbot_plugin_manager.service",
        "astrbot_plugin_manager.catalog",
        "astrbot.core.star.star_handler",
        "astrbot.core.star.register.star_handler",
        "astrbot.core.pipeline.waking_check.stage",
        "astrbot.core.pipeline.result_decorate.stage",
        "astrbot.core.pipeline.context_utils",
    ):
        monkeypatch.setattr(
            importlib.import_module(module_name), "star_handlers_registry", registry
        )
    for module_name in (
        "astrbot.core.star.star_handler",
        "astrbot.core.star.base",
        "astrbot.core.pipeline.waking_check.stage",
        "astrbot.core.pipeline.context_utils",
        "astrbot.core.pipeline.process_stage.method.star_request",
        "astrbot.core.pipeline.result_decorate.stage",
    ):
        monkeypatch.setattr(importlib.import_module(module_name), "star_map", owners)
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["admins_id"] = ["admin"]
    config["wake_prefix"] = ["/"]
    config["plugin_set"] = ["*"]
    config["provider_settings"]["enable"] = True
    tools = SimpleNamespace(func_list=[], iter_builtin_tools=lambda: [])
    context = Context.__new__(Context)
    context.registered_web_apis = []
    context.provider_manager = SimpleNamespace(llm_tools=tools)
    context.get_config = lambda umo=None: config
    context.get_all_stars = lambda: list(owners.values())
    context.get_registered_star = lambda name: next(
        (item for item in owners.values() if item.name == name), None
    )
    context.get_using_tts_provider = lambda umo=None: None
    context.get_using_tts_provider_async = AsyncMock(return_value=None)
    context.conversation_manager = SimpleNamespace(
        get_curr_conversation_id=AsyncMock(return_value=None),
        get_conversation=AsyncMock(),
    )
    store = tmp_path / "plugins"
    store.mkdir()
    reserved = tmp_path / "builtin"
    reserved.mkdir()
    configs = tmp_path / "config"
    configs.mkdir()
    (store / metadata.root_dir_name).mkdir()
    staged = []
    actions = []
    manager = SimpleNamespace(
        context=context,
        _pm_lock=asyncio.Lock(),
        plugin_store_path=str(store),
        reserved_plugin_path=str(reserved),
        plugin_config_path=str(configs),
        failed_plugin_dict={},
        reload=AsyncMock(side_effect=AssertionError("Unsafe core reload was called")),
        turn_on_plugin=AsyncMock(
            side_effect=AssertionError("Unsafe core enable was called")
        ),
    )

    async def terminate(target):
        assert manager._pm_lock.locked()
        staged[:] = [copy.copy(target)]
        actions.append("terminate")

    async def unbind(name, module_path):
        assert manager._pm_lock.locked()
        actions.append("unbind")
        owners.pop(module_path)

    async def load(*, specified_module_path):
        assert manager._pm_lock.locked()
        assert specified_module_path
        actions.append("load")
        restored = copy.copy(staged[0])
        restored.activated = True
        restored.star_cls = object()
        owners[specified_module_path] = restored
        return True, None

    async def disable(name):
        owners[
            next(key for key, item in owners.items() if item.name == name)
        ].activated = False

    async def uninstall(name, *, delete_config=False, delete_data=False):
        target = context.get_registered_star(name)
        owners.pop(target.module_path)
        (store / target.root_dir_name).rmdir()
        if delete_config:
            (configs / f"{target.root_dir_name}_config.json").unlink(missing_ok=True)
        if delete_data:
            for dirname in ("plugin_data", "plugins_data"):
                candidate = tmp_path / dirname / target.root_dir_name
                if candidate.exists():
                    candidate.rmdir()

    async def uninstall_failed(name, *, delete_config=False, delete_data=False):
        manager.failed_plugin_dict.pop(name)
        (store / name).rmdir()

    manager._terminate_plugin = AsyncMock(side_effect=terminate)
    manager._unbind_plugin = AsyncMock(side_effect=unbind)
    manager.load = AsyncMock(side_effect=load)
    manager.turn_off_plugin = AsyncMock(side_effect=disable)
    manager.install_plugin = AsyncMock()
    manager.uninstall_plugin = AsyncMock(side_effect=uninstall)
    manager.uninstall_failed_plugin = AsyncMock(side_effect=uninstall_failed)
    context._star_manager = manager
    prefs = {}

    async def global_get(key, default=None):
        return copy.deepcopy(prefs.get(key, default))

    async def global_put(key, value):
        prefs[key] = copy.deepcopy(value)

    preferences = SimpleNamespace(
        global_get=AsyncMock(side_effect=global_get),
        global_put=AsyncMock(side_effect=global_put),
        range_get_async=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(service_module, "sp", preferences)
    monkeypatch.setattr("astrbot.dashboard.services.plugin_service.sp", preferences)
    sync = AsyncMock()
    monkeypatch.setattr(PluginService, "sync_skills_after_plugin_change", sync)
    monkeypatch.setattr(PluginService, "remove_plugin_install_source", AsyncMock())
    plugin_config = AstrBotConfig(
        config_path=str(tmp_path / "manager_config.json"),
        schema=json.loads(
            (PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8")
        ),
    )
    plugin_config.update(getattr(request, "param", {}))
    plugin = main.Main(context, plugin_config)
    metadata.star_cls = plugin
    for original in ORIGINAL_HANDLERS:
        handler = copy.copy(original)
        handler.handler = functools.partial(original.handler, plugin)
        registry.append(handler)
    await plugin.initialize()

    def event(text="", user="admin", platform="qq_one", group=None, admin=True):
        message = AstrBotMessage()
        message.type = (
            MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
        )
        message.self_id = "robot"
        message.session_id = group or user
        message.message_id = "message"
        message.sender = MessageMember(user_id=user, nickname=user)
        message.group_id = group
        message.message_str = text
        message.message = [Plain(text)]
        result = AstrMessageEvent(
            text,
            message,
            PlatformMetadata("aiocqhttp", "Test", platform),
            message.session_id,
        )
        result.role = "admin" if admin else "member"
        result.sent = []

        async def send(chain):
            result.sent.append(message_text(chain.chain))
            result._has_send_oper = True

        result.send = AsyncMock(side_effect=send)
        return result

    pipeline_context = PipelineContext(
        config, SimpleNamespace(context=context), "default"
    )
    waking = WakingCheckStage()
    await waking.initialize(pipeline_context)
    session_class = importlib.import_module(
        "astrbot.core.star.session_plugin_manager"
    ).SessionPluginManager
    monkeypatch.setattr(
        session_class,
        "filter_handlers_by_session",
        AsyncMock(side_effect=lambda event, handlers: handlers),
    )
    model_calls = []

    class FakeAgent:
        async def process(self, event):
            model_calls.append(event)
            yield None

    request = StarRequestSubStage()
    await request.initialize(pipeline_context)
    process = ProcessStage()
    process.ctx = pipeline_context
    process.star_request_sub_stage = request
    process.agent_sub_stage = FakeAgent()
    decorate = ResultDecorateStage()
    await decorate.initialize(pipeline_context)
    respond = RespondStage()
    await respond.initialize(pipeline_context)
    scheduler = PipelineScheduler.__new__(PipelineScheduler)
    scheduler.ctx = pipeline_context
    scheduler.stages = [waking, process, decorate, respond]
    response_scheduler = PipelineScheduler.__new__(PipelineScheduler)
    response_scheduler.ctx = pipeline_context
    response_scheduler.stages = [decorate, respond]

    async def invoke(event, arguments):
        async for _ in call_handler(event, plugin.plugin, arguments):
            await response_scheduler.execute(event)

    def add_target(
        name="demo_plugin", directory="demo_directory", active=True, reserved=False
    ):
        target = StarMetadata(
            name=name,
            root_dir_name=directory,
            module_path=f"data.plugins.{directory}.main",
            star_cls=object() if active else None,
            activated=active,
            reserved=reserved,
            author="Tester",
            version="1.0.0",
            desc="A test plugin",
        )
        owners[target.module_path] = target
        ((reserved_store if reserved else store) / directory).mkdir(exist_ok=True)
        return target

    reserved_store = reserved
    return SimpleNamespace(
        plugin=plugin,
        main=main,
        service_module=service_module,
        catalog=catalog_module,
        context=context,
        owners=owners,
        registry=registry,
        config=config,
        plugin_config=plugin_config,
        tools=tools,
        manager=manager,
        event=event,
        scheduler=scheduler,
        invoke=invoke,
        add_target=add_target,
        actions=actions,
        prefs=prefs,
        preferences=preferences,
        sync=sync,
        model_calls=model_calls,
        root=tmp_path,
    )


@pytest.fixture
def core_installs(env):
    """Fake core downloads/loading while exercising the real WebUI service."""
    import yaml

    packages = {}

    async def install(
        repo_url, proxy="", *, ignore_version_check=False, download_url=""
    ):
        # The official core installer owns this lock; callers must not hold it.
        assert not env.manager._pm_lock.locked()
        async with env.manager._pm_lock:
            package = packages[download_url or repo_url]
            if isinstance(package, Exception):
                raise package
            target = env.add_target(
                name=package["name"],
                directory=package.get("directory", package["name"]),
                active=package.get("active", True),
            )
            target.author = package["author"]
            target.version = package["version"]
            target.repo = repo_url
            code = Path(env.manager.plugin_store_path) / target.root_dir_name
            (code / "metadata.yaml").write_text(
                yaml.safe_dump(
                    {**package, "desc": "Installed by the fake core"},
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )
            (code / "main.py").write_text(
                "# Core-installed test plugin\n", encoding="utf-8"
            )
            return {"name": target.name, "repo": repo_url}

    env.manager.install_plugin.side_effect = install
    return packages


@pytest.fixture
async def transfer_env(env, monkeypatch):
    """Seed canonical metadata and isolated per-plugin KV for transfer tests."""
    import yaml
    from astrbot_plugin_manager import transfers

    values = {}
    terminated = {}

    def make_plugin(name="demo_plugin", directory="demo_directory", active=False):
        target = env.add_target(name=name, directory=directory, active=active)
        target.display_name = "示例插件"
        code = Path(env.manager.plugin_store_path) / directory
        metadata = {
            "name": name,
            "display_name": target.display_name,
            "author": target.author,
            "version": target.version,
            "desc": target.desc,
            "astrbot_version": ">=4.27,<5",
        }
        (code / "metadata.yaml").write_text(
            yaml.safe_dump(metadata, allow_unicode=True), encoding="utf-8"
        )
        (code / "main.py").write_text(
            "raise RuntimeError('plugin code must not execute during transfer')\n",
            encoding="utf-8",
        )
        (code / "_conf_schema.json").write_text(
            json.dumps({"enabled": {"type": "bool", "default": True}}), encoding="utf-8"
        )
        return target

    async def read_range(scope, scope_id):
        return [
            SimpleNamespace(key=key, value={"val": copy.deepcopy(value)})
            for key, value in values.get((scope, scope_id), {}).items()
        ]

    async def clear(scope, scope_id):
        values[(scope, scope_id)] = {}

    async def put(scope, scope_id, key, value):
        values.setdefault((scope, scope_id), {})[key] = copy.deepcopy(value)

    async def terminate(target):
        assert env.manager._pm_lock.locked()
        terminated[target.module_path] = copy.copy(target)
        env.actions.append("terminate")

    async def load(*, specified_module_path=None, specified_dir_name=None):
        assert env.manager._pm_lock.locked()
        if specified_dir_name is None:
            restored = copy.copy(terminated[specified_module_path])
            restored.activated = True
            restored.star_cls = object()
            env.owners[specified_module_path] = restored
        else:
            code = Path(env.manager.plugin_store_path) / specified_dir_name
            metadata = yaml.safe_load(
                (code / "metadata.yaml").read_text(encoding="utf-8")
            )
            module = f"data.plugins.{specified_dir_name}.main"
            active = module not in env.prefs.get("inactivated_plugins", [])
            restored = env.add_target(
                metadata["name"], specified_dir_name, active=active
            )
            for field in ("name", "author", "version", "display_name", "repo", "desc"):
                setattr(restored, field, metadata.get(field, ""))
        env.actions.append("load")
        return True, None

    env.preferences.range_get_async.side_effect = read_range
    env.preferences.flush = AsyncMock()
    env.preferences.clear_async = AsyncMock(side_effect=clear)
    env.preferences.put_async = AsyncMock(side_effect=put)
    env.manager._terminate_plugin.side_effect = terminate
    env.manager.load.side_effect = load
    monkeypatch.setattr(
        env.plugin.service.web, "get_online_plugins", AsyncMock(return_value=({}, None))
    )
    monkeypatch.setattr(
        env.manager, "_iter_plugin_llm_tools", lambda module: [], raising=False
    )
    monkeypatch.setattr(
        transfers.shutil, "disk_usage", lambda path: SimpleNamespace(free=1 << 40)
    )
    env.context.get_config()["dashboard"]["jwt_secret"] = (
        "transfer-test-secret-with-at-least-32-bytes"
    )
    env.make_transfer_plugin = make_plugin
    env.kv_values = values
    return env
