"""Verify native page discovery and frontend state transitions without a browser."""

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot.dashboard.services.plugin_page_service import PluginPageService

ROOT = Path(__file__).resolve().parents[1]


async def test_native_page_discovery_assets_and_chinese_title(
    transfer_env, monkeypatch
):
    page_name, title = "transfer", "插件管家"
    env = transfer_env
    root = ROOT
    service = PluginPageService(
        SimpleNamespace(context=env.context), config=env.context.get_config()
    )
    monkeypatch.setattr(service, "get_plugin_root_dir", lambda plugin: root)
    metadata = env.owners[env.main.__name__]
    pages = await service.discover_plugin_pages(metadata)
    assert [page.name for page in pages] == ["transfer"]
    secret = env.context.get_config()["dashboard"]["jwt_secret"]
    token = service.issue_plugin_page_asset_token(
        plugin_name=metadata.name,
        page_name=page_name,
        jwt_secret=secret,
        username="admin",
        locale="zh-CN",
    )
    context = service.build_initial_context(
        asset_token=token, jwt_secret=secret, locale="zh-CN", theme="dark"
    )
    assert context["pageTitle"] == title
    assert context["isDark"] is True
    for asset, expected_type in (
        ("", "text/html"),
        ("style.css", "text/css"),
        ("app.js", "application/javascript"),
        ("logo.png", "image/png"),
    ):
        payload = await service.serve_page_content(
            plugin_name=metadata.name,
            page_name=page_name,
            asset_path=asset,
            asset_token=token,
            jwt_secret=secret,
            username="admin",
            locale="zh-CN",
            theme="dark",
        )
        assert payload.content_type.startswith(expected_type)
        if not asset:
            assert 'aria-label="迁移功能"' in payload.content
            assert 'id="source-dialog"' in payload.content
            assert 'id="export-format"' in payload.content
            assert 'id="import-format"' in payload.content
            assert "open-manage" not in payload.content
            assert 'data-theme="dark"' in payload.content
            assert "bridge-sdk.js" in payload.content
            assert "style.css?" in payload.content
            assert "app.js?" in payload.content
        if asset == "logo.png":
            assert payload.content == (root / "logo.png").read_bytes()


def test_frontend_selection_save_confirmation_and_import_defaults():
    executable = shutil.which("node")
    if not executable:
        pytest.skip("Node.js is required for frontend state tests")
    root = Path(__file__).resolve().parents[1]
    script = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
class Element {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.listeners = {};
    this.value = "";
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.classList = {toggle() {}};
    this.content = "";
  }
  set textContent(value) {this.content = String(value); this.children = [];}
  get textContent() {return this.content + this.children.map(child => child.textContent).join("");}
  get childNodes() {return this.children;}
  append(...children) {this.children.push(...children);}
  replaceChildren(...children) {this.children = children; this.content = "";}
  setAttribute(key, value) {this.attributes[key] = value;}
  addEventListener(event, handler) {(this.listeners[event] ||= []).push(handler);}
  async trigger(event, payload = {}) {for (const handler of this.listeners[event] || []) await handler({target: this, ...payload});}
  showModal() {this.open = true;}
  close() {this.open = false;}
}
async function main() {
  const root = process.argv[1];
  const html = fs.readFileSync(path.join(root, "pages/transfer/index.html"), "utf8");
  const elements = Object.fromEntries(Array.from(html.matchAll(/id="([^"]+)"/g), match => {
    const element = new Element("div");
    element.id = match[1];
    return [match[1], element];
  }));
  const tabs = Array.from(html.matchAll(/data-tab="([^"]+)"/g), match => {
    const element = new Element("button");
    element.dataset.tab = match[1];
    return element;
  });
  elements["boot-error"].hidden = true;
  elements["status-filter"].value = "all";
  elements["export-format"].value = "zip";
  elements["import-format"].value = "zip";
  const document = {
    hidden: false, documentElement: {dataset: {}},
    querySelectorAll(selector) {return selector === "[id]" ? Object.values(elements) : tabs;},
    createElement(tag) {return new Element(tag);},
    createTextNode(text) {const element = new Element("text"); element.textContent = text; return element;},
  };
  const plugin = {
    directory: "demo_dir", name: "demo", plugin_id: "Alice/demo", display_name: "<script>展示文本</script>",
    version: "1.0.0", status: "enabled", blocked: "", ranges: [],
    data_available: true, config_available: true, code_size: 1024,
    config_size: 32, data_size: 2048, kv_count: 1,
  };
  const state = {
    csrf: "test", busy: false, plugins: [plugin], jobs: [],
    rule: {rule_version: 1, updated_at: 50, selections: [{directory: plugin.directory, name: plugin.name, plugin_id: plugin.plugin_id, config: true, data: true}]},
    limits: {archive_bytes: 256 * 1024 * 1024},
  };
  let posts = 0;
  const exports = [];
  const bridge = {
    async ready() {}, onContext(handler) {handler({isDark: true});},
    async apiGet(endpoint) {
      if (endpoint === "manage/state") return {csrf: "source-test", busy: false, plugins: [], jobs: []};
      return structuredClone(state);
    },
    async apiPost(endpoint, body) {
      posts += 1;
      if (endpoint === "transfer/rule") {
        state.rule = {rule_version: 1, updated_at: 100, selections: body.selections.map(item => ({...item, name: plugin.name, plugin_id: plugin.plugin_id}))};
        return structuredClone(state.rule);
      }
      if (endpoint === "transfer/export-preview") return {plugins: body.selections.map(item => ({...plugin, ...item})), selections: body.selections.map(item => ({...item, name: plugin.name, plugin_id: plugin.plugin_id}))};
      if (endpoint === "transfer/export") {exports.push(body); return {id: "export-id"};}
      throw new Error("Unexpected endpoint " + endpoint);
    },
    async upload(endpoint) {
      assert.equal(endpoint, "transfer/upload/test");
      return {
        job: {id: "import-id", warnings: []}, astrbot_version: "4.27.4",
        plugins: [{...plugin, exists: true, config: true, data: false, problem: ""},
          {...plugin, directory: "new_dir", name: "new", exists: false, config: true, data: true, problem: ""}],
      };
    },
    async download() {},
  };
  const context = vm.createContext({document, window: {AstrBotPluginPage: bridge, setInterval() {}}, console});
  const source = fs.readFileSync(path.join(root, "pages/transfer/app.js"), "utf8");
  assert.ok(source.trimEnd().endsWith("initialize();"));
  vm.runInContext(source.slice(0, source.lastIndexOf("initialize();")) + "globalThis.boot = initialize();", context);
  await context.boot;
  assert.equal(elements["boot-error"].hidden, true, elements["boot-error"].textContent);
  assert.equal(document.documentElement.dataset.theme, "dark");
  assert.ok(elements["plugin-list"].textContent.includes(plugin.display_name));
  assert.equal(elements["selected-count"].textContent, "0");
  assert.equal(elements["apply-rule"].disabled, false);
  assert.equal(elements.export.disabled, true);
  await elements["apply-rule"].trigger("click");
  assert.equal(elements["selected-count"].textContent, "1");
  assert.equal(elements["data-count"].textContent, "1 个");
  assert.equal(elements["config-count"].textContent, "1 个");
  assert.equal(elements["save-rule"].disabled, true);
  await elements.refresh.trigger("click");
  assert.equal(elements["selected-count"].textContent, "1");
  await vm.runInContext("loadState()", context);
  assert.equal(elements["selected-count"].textContent, "0");
  elements["select-all"].checked = true;
  await elements["select-all"].trigger("change");
  assert.equal(elements["selected-count"].textContent, "1");
  assert.equal(elements["save-rule"].disabled, false);
  assert.equal(elements.export.disabled, false);
  await elements["save-rule"].trigger("click");
  assert.equal(elements["save-rule"].disabled, true);
  assert.equal(elements.export.disabled, false);
  const exporting = elements.export.trigger("click");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.confirmation.open, true);
  await elements["confirmation-cancel"].trigger("click");
  await exporting;
  assert.equal(posts, 2);
  // An unsaved selection can be confirmed and exported without changing the rule.
  const savedRule = JSON.stringify(state.rule);
  elements["bulk-data"].checked = true;
  await elements["bulk-data"].trigger("change");
  assert.equal(elements["save-rule"].disabled, false);
  assert.equal(elements.export.disabled, false);
  const currentExport = elements.export.trigger("click");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements["apply-rule"].disabled, true);
  elements.acknowledged.checked = true;
  await elements["confirmation-form"].trigger("submit", {preventDefault() {}});
  await currentExport;
  assert.equal(exports.length, 1);
  assert.equal(exports[0].selections[0].data, true);
  assert.equal(exports[0].selections[0].plugin_id, plugin.plugin_id);
  assert.equal(JSON.stringify(state.rule), savedRule);
  await elements["apply-rule"].trigger("click");
  assert.equal(elements["data-count"].textContent, "0 个");
  elements["bundle-file"].files = [{name: "test.zip", size: 100}];
  await elements["bundle-file"].trigger("change");
  assert.equal(elements["import-preview"].hidden, false);
  const imported = vm.runInContext("JSON.stringify(Array.from(importChoices.entries()))", context);
  const choices = new Map(JSON.parse(imported));
  assert.deepEqual(choices.get("demo_dir"), {code: false, config: false, data: false});
  assert.deepEqual(choices.get("new_dir"), {code: true, config: false, data: false});
  assert.equal(elements["execute-import"].disabled, false);
  state.rule.selections.push({directory: "missing_dir", name: "missing", config: false, data: false});
  await vm.runInContext("loadState(true)", context);
  assert.doesNotMatch(elements["plugin-list"].textContent, /missing/);
  const beforeApply = vm.runInContext("JSON.stringify(selections())", context);
  await elements["apply-rule"].trigger("click");
  assert.match(elements.message.textContent, /规则未应用.*插件不存在/);
  assert.equal(vm.runInContext("JSON.stringify(selections())", context), beforeApply);
  state.rule.selections.pop();
  state.rule.selections[0].plugin_id = "Another/demo";
  await vm.runInContext("loadState(true)", context);
  await elements["apply-rule"].trigger("click");
  assert.match(elements.message.textContent, /规则未应用.*身份已变化/);
  assert.equal(vm.runInContext("JSON.stringify(selections())", context), beforeApply);
  state.rule.selections[0].plugin_id = plugin.plugin_id;
  state.rule.selections[0].config = true;
  state.plugins[0].config_available = false;
  await vm.runInContext("loadState(true)", context);
  await elements["apply-rule"].trigger("click");
  assert.match(elements.message.textContent, /规则未应用.*配置不存在/);
  assert.equal(vm.runInContext("JSON.stringify(selections())", context), beforeApply);
  await elements["clear-selection"].trigger("click");
  assert.equal(elements.export.disabled, true);
  await elements["save-rule"].trigger("click");
  assert.equal(state.rule.selections.length, 0);
  assert.equal(elements["apply-rule"].disabled, true);
}
main().catch(error => {console.error(error); process.exitCode = 1;});
"""
    result = subprocess.run(
        [executable, "-e", script, str(root)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_management_frontend_selection_binding_polling_and_manifest_confirmation():
    executable = shutil.which("node")
    if not executable:
        pytest.skip("Node.js is required for frontend state tests")
    result = subprocess.run(
        [executable, str(ROOT / "tests/transfer_page_state.cjs"), str(ROOT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
