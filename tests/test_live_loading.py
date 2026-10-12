"""Load and reload real temporary plugins with AstrBot's actual lifecycle code."""

import asyncio
import importlib
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.core.star.star_manager import PluginManager
from astrbot.core.star.updater import _PluginUpdater


@pytest.fixture
def live_env(env, monkeypatch):
    stars = list(env.owners.values())
    for module_name in (
        "astrbot.core.star.star",
        "astrbot.core.star.base",
        "astrbot.core.star.context",
        "astrbot.core.star.star_manager",
    ):
        module = importlib.import_module(module_name)
        monkeypatch.setattr(module, "star_map", env.owners)
        monkeypatch.setattr(module, "star_registry", stars)
    manager_module = importlib.import_module("astrbot.core.star.star_manager")
    monkeypatch.setattr(manager_module, "star_handlers_registry", env.registry)
    monkeypatch.setattr(manager_module.llm_tools, "func_list", env.tools.func_list)
    monkeypatch.setattr(manager_module, "sp", env.preferences)
    monkeypatch.setattr(manager_module, "sync_command_configs", AsyncMock())
    root = Path(env.manager.plugin_store_path)
    for name, directory in (("data", root.parent), ("data.plugins", root)):
        package = ModuleType(name)
        package.__path__ = [str(directory)]
        monkeypatch.setitem(sys.modules, name, package)
    manager = PluginManager.__new__(PluginManager)
    manager.context = env.context
    manager.config = env.config
    manager.plugin_store_path = str(root)
    manager.plugin_config_path = env.manager.plugin_config_path
    manager.reserved_plugin_path = env.manager.reserved_plugin_path
    manager.conf_schema_fname = "_conf_schema.json"
    manager.logo_fname = "logo.png"
    manager.failed_plugin_dict = {}
    manager.failed_plugin_info = ""
    manager._pm_lock = asyncio.Lock()
    modules = []
    manager._get_plugin_modules = lambda: modules
    env.context._star_manager = manager
    env.plugin.service.manager = manager
    env.plugin.service.web.plugin_manager = manager
    env.live_manager = manager
    env.live_modules = modules
    env.live_stars = stars
    env.live_root = root
    yield env
    for module_name in list(sys.modules):
        if module_name.startswith(
            ("data.plugins.reload_probe", "data.plugins.astrbot_plugin_manager")
        ):
            del sys.modules[module_name]


@pytest.mark.parametrize("entrypoint", ["command", "manifest"])
@pytest.mark.parametrize("source", ["market", "repository"])
async def test_chat_and_manifest_install_initialize_plugin_through_actual_core(
    live_env, monkeypatch, entrypoint, source
):
    env = live_env
    name, author, version = "reload_probe", "Probe", "1.0.0"
    repo = "https://github.com/probe/reload_probe"
    identifier = f"{author}/{name}"
    url = "https://cloud.astrbot.app/packages/probe.zip"
    files = {
        "metadata.yaml": f"name: {name}\nauthor: {author}\ndesc: Probe\nversion: {version}\n",
        "main.py": """from astrbot.api.star import Star
class Main(Star):
    def __init__(self, context):
        self.context = context
    async def initialize(self):
        assert self.context.dependencies_prepared
        self.initialized = True
""",
        "requirements.txt": "fake-dependency-for-install-test>=1\n",
    }

    async def download(repo_url, proxy="", *, download_url=""):
        assert repo_url == repo
        assert download_url == (url if source == "market" else "")
        directory = env.live_root / name
        directory.mkdir()
        for filename, content in files.items():
            (directory / filename).write_text(content, encoding="utf-8")
        env.live_modules.append({"pname": name, "module": "main"})
        return str(directory)

    async def prepare_dependencies(plugin_path, directory):
        assert directory == name
        assert await asyncio.to_thread(Path(plugin_path, "requirements.txt").is_file)
        env.context.dependencies_prepared = True

    async def preview_download(repo_url, path):
        assert repo_url == repo
        with zipfile.ZipFile(path, "w") as archive:
            for filename, content in files.items():
                archive.writestr(filename, content)

    env.live_manager._updater = SimpleNamespace(install=AsyncMock(side_effect=download))
    dependencies = AsyncMock(side_effect=prepare_dependencies)
    monkeypatch.setattr(env.live_manager, "_ensure_plugin_requirements", dependencies)
    monkeypatch.setattr("astrbot.core.star.star_manager.Metric.upload", AsyncMock())
    monkeypatch.setattr(
        env.plugin.service.web,
        "get_online_plugins",
        AsyncMock(
            return_value=(
                {
                    identifier: {
                        "name": name,
                        "author": author,
                        "version": version,
                        "repo": repo,
                        "download_url": url,
                    }
                },
                None,
            )
        ),
    )
    monkeypatch.setattr(
        "astrbot_plugin_manager.manifests.download_repository_archive", preview_download
    )
    if entrypoint == "command":
        event = env.event("/pm add " + (identifier if source == "market" else repo))
        await env.scheduler.execute(event)
        assert "已安装（已启用）" in "\n".join(event.sent)
    else:
        preview = await env.plugin.manifests.preview(
            "admin",
            {
                "format": "astrbot-plugin-manager-manifest",
                "schema_version": 1,
                "name": "测试清单",
                "plugins": [{"display_name": "Probe", "market_plugin_id": identifier}]
                if source == "market"
                else [{"display_name": "Probe", "repo_url": repo}],
            },
        )
        await env.plugin.manifests.start(
            "admin", preview["job"]["id"], [{"id": "0", "source": source}], True
        )
        await env.plugin.transfers.task
        job = env.plugin.transfers.get_job(preview["job"]["id"], "admin")
        assert job["status"] == "completed", job
        assert job["results"][0]["enabled"] is True
        assert not job.get("reload_required")
    target = env.context.get_registered_star(name)
    assert target.activated and target.star_cls.initialized
    assert (await env.plugin.sources.detail(name))["local"]["status"] == "enabled"
    dependencies.assert_awaited_once()
    env.sync.assert_awaited_once()
    assert env.prefs["plugin_install_sources"][name]["install_method"] == source


@pytest.mark.parametrize("desktop", [False, True])
async def test_reload_reimports_main_helper_commands_and_tools_even_with_same_timestamp(
    live_env, monkeypatch, desktop
):
    env = live_env
    monkeypatch.setenv("ASTRBOT_DESKTOP_CLIENT", "1" if desktop else "0")
    monkeypatch.setenv("ASTRBOT_DESKTOP_MANAGED", "1" if desktop else "0")
    directory = env.live_root / "reload_probe"
    directory.mkdir()
    (directory / "metadata.yaml").write_text(
        "name: reload_probe\nauthor: Probe\ndesc: Reload probe\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    helper = directory / "helper.py"
    helper.write_text("VALUE = 1\n", encoding="utf-8")
    (directory / "main.py").write_text(
        '''from astrbot.api.star import Star
from astrbot.api.event import filter
from .helper import VALUE
class Main(Star):
    def __init__(self, context):
        self.context = context
        self.marker = VALUE
    async def initialize(self):
        self.initialized = True
    async def terminate(self):
        self.terminated = True
    @filter.command("probe_command")
    async def probe(self, event):
        event.set_result(event.plain_result(str(VALUE)))
    @filter.llm_tool(name="probe_tool")
    async def tool(self, event):
        """Return the current probe revision."""
        return str(VALUE)
''',
        encoding="utf-8",
    )
    env.live_modules.append({"pname": "reload_probe", "module": "main"})
    success, error = await env.live_manager.load(specified_dir_name="reload_probe")
    assert success, error
    original = env.context.get_registered_star("reload_probe")
    previous = original.star_cls
    old_main = sys.modules[original.module_path]
    old_helper = sys.modules["data.plugins.reload_probe.helper"]
    stat = await asyncio.to_thread(helper.stat)
    helper.write_text("VALUE = 2\n", encoding="utf-8")
    await asyncio.to_thread(os.utime, helper, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    event = env.event("/pm reload reload_probe")
    await env.scheduler.execute(event)
    assert "已重载" in "\n".join(event.sent)
    current = env.context.get_registered_star("reload_probe")
    assert previous.terminated
    assert current.star_cls is not previous and current.star_cls.marker == 2
    assert sys.modules[current.module_path] is not old_main
    assert sys.modules["data.plugins.reload_probe.helper"] is not old_helper
    commands = [
        item
        for item in env.registry
        if item.handler_module_path == current.module_path
        and item.handler_name == "probe"
    ]
    assert len(commands) == 1 and commands[0].handler.args == (current.star_cls,)
    tools = [item for item in env.tools.func_list if item.name == "probe_tool"]
    assert len(tools) == 1 and await tools[0].handler(env.event()) == "2"
    env.sync.assert_awaited_once()
    assert not env.model_calls


@pytest.mark.parametrize("was_inactive", [False, True])
async def test_pending_load_initializes_only_the_confirmed_plugin(
    live_env, was_inactive
):
    env = live_env
    env.plugin.transfers.manager = env.live_manager
    directory = env.live_root / "reload_probe"
    directory.mkdir()
    (directory / "metadata.yaml").write_text(
        "name: reload_probe\nauthor: Probe\ndesc: Pending probe\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    (directory / "main.py").write_text(
        """from astrbot.api.star import Star
class Main(Star):
    def __init__(self, context):
        self.context = context
    async def initialize(self):
        self.initialized = True
""",
        encoding="utf-8",
    )
    other = env.live_root / "other_pending"
    other.mkdir()
    (other / "main.py").write_text(
        "raise RuntimeError('unselected plugin must not load')\n", encoding="utf-8"
    )
    env.live_modules.extend(
        [
            {"pname": "reload_probe", "module": "main"},
            {"pname": "other_pending", "module": "main"},
        ]
    )
    module_path = "data.plugins.reload_probe.main"
    env.prefs["inactivated_plugins"] = ["data.plugins.other.main"] + (
        [module_path] if was_inactive else []
    )
    record = await env.plugin.sources.detail("reload_probe")
    result = await env.plugin.loading.load(
        "reload_probe", record["revision"], True
    )
    assert result["status"] == "enabled", result
    assert not result["warnings"]
    assert not env.plugin.transfers.jobs
    assert not list(env.plugin.transfers.jobs_root.glob("*"))
    current = env.context.get_registered_star("reload_probe")
    assert current.activated and current.star_cls.initialized
    assert env.prefs["inactivated_plugins"] == ["data.plugins.other.main"]
    assert not env.context.get_registered_star("other_pending")
    assert "other_pending" not in env.live_manager.failed_plugin_dict
    assert (await env.plugin.sources.detail("reload_probe"))["local"]["status"] == "enabled"
    env.sync.assert_awaited_once()


async def test_pending_load_reports_real_core_initialization_failure(live_env):
    env = live_env
    env.plugin.transfers.manager = env.live_manager
    directory = env.live_root / "reload_probe"
    directory.mkdir()
    (directory / "metadata.yaml").write_text(
        "name: reload_probe\nauthor: Probe\ndesc: Failing probe\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    (directory / "main.py").write_text(
        """from astrbot.api.star import Star
class Main(Star):
    def __init__(self, context):
        pass
    async def initialize(self):
        raise RuntimeError("probe initialization failed")
""",
        encoding="utf-8",
    )
    module_path = "data.plugins.reload_probe.main"
    env.prefs["inactivated_plugins"] = [module_path]
    env.live_modules.append({"pname": "reload_probe", "module": "main"})
    record = await env.plugin.sources.detail("reload_probe")
    with pytest.raises(ValueError, match="probe initialization failed"):
        await env.plugin.loading.load("reload_probe", record["revision"], True)
    assert not env.plugin.transfers.jobs
    assert not list(env.plugin.transfers.jobs_root.glob("*"))
    assert module_path in env.prefs["inactivated_plugins"]
    assert not env.context.get_registered_star("reload_probe")
    assert (await env.plugin.sources.detail("reload_probe"))["local"]["status"] == "failed"
    env.sync.assert_not_awaited()


@pytest.mark.parametrize("active", [True, False])
@pytest.mark.parametrize("source_kind", ["market", "repository"])
@pytest.mark.parametrize("author_declaration", ["Probe", "Probe, Coauthor"])
async def test_update_uses_core_archive_and_preserves_enabled_state(
    live_env, monkeypatch, active, source_kind, author_declaration
):
    env = live_env
    directory = env.live_root / "reload_probe"
    directory.mkdir()
    module_path = "data.plugins.reload_probe.main"
    main_code = '''from astrbot.api.star import Star
from astrbot.api.event import filter
from .helper import VALUE
class Main(Star):
    def __init__(self, context):
        self.marker = VALUE
    async def initialize(self):
        self.initialized = True
    async def terminate(self):
        self.terminated = True
    @filter.command("update_probe")
    async def probe(self, event):
        event.set_result(event.plain_result(str(VALUE)))
    @filter.llm_tool(name="update_probe_tool")
    async def tool(self, event):
        """Return the installed update revision."""
        return str(VALUE)
'''
    original_metadata = f"name: reload_probe\nauthor: {author_declaration}\ndesc: Update probe\nversion: 1.0.0\n"
    (directory / "metadata.yaml").write_text(original_metadata, encoding="utf-8")
    (directory / "main.py").write_text(main_code, encoding="utf-8")
    (directory / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    if not active:
        env.prefs["inactivated_plugins"] = [module_path]
    env.live_modules.append({"pname": "reload_probe", "module": "main"})
    success, error = await env.live_manager.load(specified_dir_name="reload_probe")
    assert success, error
    original = env.context.get_registered_star("reload_probe")
    assert original.activated is active
    previous_instance = original.star_cls
    repo_url = "https://github.com/probe/reload_probe/tree/stable"
    download_url = "https://example.com/update.zip" if source_kind == "market" else ""
    remote = {
        "name": "reload_probe",
        "author": "Probe" if source_kind == "market" else author_declaration,
        "version": "2.0.0",
        "repo": repo_url,
        "download_url": download_url,
    }
    env.prefs["plugin_install_sources"] = {
        "reload_probe": {
            "root_dir_name": "reload_probe",
            "name": "reload_probe",
            "author": author_declaration,
            "install_method": source_kind,
            "repo": repo_url,
            "registry_url": None,
            "market_plugin_id": "Probe/reload_probe" if source_kind == "market" else "",
            "installed_at": "2025-01-01T00:00:00Z",
        }
    }
    monkeypatch.setattr(
        env.plugin.service.web,
        "get_online_plugins",
        AsyncMock(return_value=({"Probe/reload_probe": remote}, None)),
    )
    monkeypatch.setattr(
        env.live_manager, "inspect_plugin_repository", AsyncMock(return_value=remote)
    )
    updater = _PluginUpdater()
    updater.plugin_store_path = str(env.live_root)

    async def download(url, destination):
        assert env.live_manager._pm_lock.locked()
        assert url == download_url
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr(
                "plugin/metadata.yaml", original_metadata.replace("1.0.0", "2.0.0")
            )
            archive.writestr("plugin/main.py", main_code)
            archive.writestr("plugin/helper.py", "VALUE = 2\n")

    async def repository_download(path, url, *, proxy):
        assert url == repo_url
        await download("", path + ".zip")

    monkeypatch.setattr(updater, "_download_file", AsyncMock(side_effect=download))
    monkeypatch.setattr(
        updater, "_download_repository", AsyncMock(side_effect=repository_download)
    )
    env.live_manager._updater = updater
    env.live_manager._ensure_plugin_requirements = AsyncMock()
    provenance = (await env.plugin.sources.detail("reload_probe"))["provenance"]
    await env.plugin.updates.start_check("admin")
    await env.plugin.updates.task
    report = env.plugin.updates.state("admin")
    entry = report["results"][0]
    assert entry["can_update"]
    job = await env.plugin.updates.start_update(
        "admin", report["id"], "reload_probe", entry["binding_id"], True
    )
    await env.plugin.transfers.task
    completed = env.plugin.transfers.get_job(job["id"], "admin")
    assert completed["status"] == "completed", completed["error"]
    current = env.context.get_registered_star("reload_probe")
    assert current.version == "2.0.0" and current.activated is active
    assert (
        len(
            [
                plugin
                for plugin in env.context.get_all_stars()
                if plugin.root_dir_name == "reload_probe"
            ]
        )
        == 1
    )
    if active:
        assert previous_instance.terminated
        assert current.star_cls is not previous_instance
        assert current.star_cls.initialized and current.star_cls.marker == 2
    else:
        assert current.star_cls is None
        assert env.prefs["inactivated_plugins"] == [module_path]
    commands = [
        item
        for item in env.registry
        if item.handler_module_path == module_path and item.handler_name == "probe"
    ]
    assert len(commands) == 1
    tools = [tool for tool in env.tools.func_list if tool.name == "update_probe_tool"]
    assert len(tools) == 1 and tools[0].active is active
    current_source = await env.plugin.sources.detail("reload_probe")
    assert current_source["provenance"] == provenance
    assert not current_source["metadata_changed"]
    env.live_manager._ensure_plugin_requirements.assert_awaited_once_with(
        str(directory), "reload_probe"
    )
    env.sync.assert_awaited_once()


@pytest.mark.parametrize("allow_prefix_omission", [True, False])
@pytest.mark.parametrize("broken_revision", [False, True])
async def test_manager_reloads_itself_and_clears_confirmations(
    live_env, broken_revision, allow_prefix_omission
):
    env = live_env
    source_root = (await asyncio.to_thread(Path(env.main.__file__).resolve)).parent
    directory = env.live_root / "astrbot_plugin_manager"
    for source in [
        *source_root.glob("*.py"),
        source_root / "metadata.yaml",
        source_root / "_conf_schema.json",
    ]:
        shutil.copy2(source, directory / source.name)
    config_path = (
        Path(env.live_manager.plugin_config_path) / f"{directory.name}_config.json"
    )
    config_path.write_text(
        json.dumps({"allow_prefix_omission": allow_prefix_omission}), encoding="utf-8"
    )
    main_path = directory / "main.py"
    original_source = main_path.read_text(encoding="utf-8")
    main_path.write_text(original_source + "\nREVISION = 1\n", encoding="utf-8")
    env.owners.clear()
    env.registry.clear()
    env.live_stars.clear()
    env.live_modules.append({"pname": directory.name, "module": "main"})
    success, error = await env.live_manager.load(specified_dir_name=directory.name)
    assert success, error
    original = env.context.get_registered_star(directory.name)
    previous = original.star_cls
    assert previous.service.allow_prefix_omission is allow_prefix_omission
    previous._removals[("session", "admin")] = object()
    old_main = sys.modules[original.module_path]
    old_service = sys.modules["data.plugins.astrbot_plugin_manager.service"]
    revision = (
        '\nraise RuntimeError("broken revision")\n'
        if broken_revision
        else "\nREVISION = 2\n"
    )
    main_path.write_text(original_source + revision, encoding="utf-8")
    event = env.event(f"/pm reload {directory.name}")
    await env.scheduler.execute(event)
    assert not previous.ready and not previous._removals
    assert event.is_stopped() and not env.model_calls
    if broken_revision:
        assert "失败" in "\n".join(event.sent)
        assert "已重载" not in "\n".join(event.sent)
        assert env.context.get_registered_star(directory.name) is None
        env.sync.assert_not_awaited()
        return
    current = env.context.get_registered_star(directory.name)
    assert current.star_cls is not previous and current.star_cls.ready
    assert current.star_cls.service.allow_prefix_omission is allow_prefix_omission
    assert not current.star_cls._removals
    assert sys.modules[current.module_path] is not old_main
    assert sys.modules[current.module_path].REVISION == 2
    assert sys.modules["data.plugins.astrbot_plugin_manager.service"] is not old_service
    handlers = [item for item in env.registry if item.handler_name == "plugin"]
    assert len(handlers) == 1 and handlers[0].handler.args == (current.star_cls,)
    env.sync.assert_awaited_once()
