"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.dataset = {}; this.listeners = {};
    this.value = ""; this.hidden = false; this.disabled = false; this.checked = false;
    this.classList = {toggle() {}}; this.content = "";
  }
  set textContent(value) {this.content = String(value); this.children = [];}
  get textContent() {return this.content + this.children.map(child => child.textContent).join("");}
  get childNodes() {return this.children;}
  append(...children) {this.children.push(...children);}
  replaceChildren(...children) {this.children = children; this.content = "";}
  setAttribute() {}
  addEventListener(event, handler) {(this.listeners[event] ||= []).push(handler);}
  async trigger(event) {for (const handler of this.listeners[event] || []) await handler({target: this, preventDefault() {}});}
  showModal() {this.open = true;}
  close() {this.open = false;}
}
const settle = () => new Promise(resolve => setImmediate(resolve));
function descendants(element) {return element.children.flatMap(child => [child, ...descendants(child)]);}

async function main() {
  const root = process.argv[2];
  const html = fs.readFileSync(path.join(root, "pages/transfer/index.html"), "utf8");
  const elements = Object.fromEntries(Array.from(html.matchAll(/id="([^"]+)"/g), match => {
    const element = new Element("div"); element.id = match[1]; return [match[1], element];
  }));
  const tabs = Array.from(html.matchAll(/data-tab="([^"]+)"/g), match => {
    const element = new Element("button"); element.dataset.tab = match[1]; return element;
  });
  elements["boot-error"].hidden = true; elements["status-filter"].value = "all";
  elements["export-format"].value = "zip"; elements["import-format"].value = "zip";
  const document = {
    hidden: false, documentElement: {dataset: {}},
    querySelectorAll(selector) {return selector === "[id]" ? Object.values(elements) : tabs;},
    createElement(tag) {return new Element(tag);},
    createTextNode(text) {const element = new Element("text"); element.textContent = text; return element;},
  };
  const binding = {id: "official", kind: "market", registry_url: null, market_plugin_id: "Alice/demo", repo_url: "https://github.com/alice/demo", verification: "observed"};
  const record = {
    revision: "r1", identity_changed: false, active_binding_id: binding.id, bindings: [binding], history: [],
    provenance: {method: "market", evidence: "core_record_observed", observed_at: "2026-10-11"},
    local: {directory: "demo_dir", name: "demo", author: "Alice", display_name: "<img src=x onerror=alert(1)>", status: "enabled", version: "1.0.0", problem: "", reserved: false},
  };
  const unbound = structuredClone(record); unbound.local.directory = "unknown_dir"; unbound.local.name = "unknown"; unbound.bindings = []; unbound.active_binding_id = null; unbound.provenance.method = "unknown";
  const management = {csrf: "source-token", busy: false, plugins: [record, unbound], jobs: []};
  const plugin = {...record.local, plugin_id: "Alice/demo", logo: "/api/file/icon-token", blocked: "", ranges: [], data_available: true, config_available: true, code_size: 1024, config_size: 32, data_size: 2048, kv_count: 1};
  const state = {csrf: "zip-token", busy: false, plugins: [plugin, {...plugin, ...unbound.local, logo: null}], jobs: [], rule: {updated_at: null, selections: []}, limits: {archive_bytes: 268435456}};
  const posts = [], downloads = [], uploads = [];
  const statisticsRequests = [], managementRequests = [];
  let delayStatistics = true, delayManagement = true, failStatistics = false, failManagement = false;
  let failState = false, failMetadataSync = false;
  let delayedPreview, failPreview = false;
  const bridge = {
    async ready() {}, onContext(handler) {handler({isDark: true});},
    async apiGet(endpoint) {
      if (endpoint === "manage/state") {
        if (failManagement) throw new Error("source unavailable");
        const response = structuredClone(management);
        if (delayManagement) return new Promise((resolve, reject) => managementRequests.push({resolve, reject, response}));
        return response;
      }
      if (endpoint === "transfer/state") {
        if (failState) throw new Error("插件状态读取失败");
        const response = structuredClone(state);
        for (const entry of response.plugins) Object.assign(entry, {statistics_status: entry.blocked ? "unavailable" : "pending", code_size: null, config_size: null, data_size: null, kv_count: null});
        return response;
      }
      if (endpoint === "transfer/statistics") {
        if (failStatistics) throw new Error("statistics unavailable");
        const response = {plugins: state.plugins.map(entry => ({...structuredClone(entry), statistics_status: entry.blocked ? "unavailable" : "ready"}))};
        if (delayStatistics) return new Promise((resolve, reject) => statisticsRequests.push({resolve, reject, response}));
        return response;
      }
      if (endpoint === "transfer/jobs") return {busy: state.busy, jobs: structuredClone(state.jobs)};
      throw new Error("Unexpected endpoint " + endpoint);
    },
    async apiPost(endpoint, body) {
      posts.push({endpoint, body});
      if (endpoint.startsWith("manage/")) assert.equal(body.csrf, "source-token");
      if (endpoint === "manage/source-preview") {
        if (failPreview) throw new Error("仓库身份不一致");
        if (delayedPreview) return new Promise(resolve => {delayedPreview.resolve = resolve;});
        return {id: "preview", binding};
      }
      if (endpoint === "manage/source-bind") return structuredClone(record);
      if (endpoint === "manage/metadata-sync") {
        if (failMetadataSync) throw new Error("插件信息已变化，请刷新后重试。");
        assert.equal(body.directory, record.local.directory);
        assert.equal(body.revision, "metadata-r1");
        record.metadata_changed = false; record.metadata_notice = "";
        record.revision = "metadata-r2";
        return structuredClone(record);
      }
      if (endpoint === "manage/source-select") {record.active_binding_id = body.binding_id; record.revision = "r3"; return structuredClone(record);}
      if (endpoint === "manage/manifest-export") return {id: "export-id"};
      if (endpoint === "manage/manifest-install") return {id: "install-id"};
      throw new Error("Unexpected endpoint " + endpoint);
    },
    async upload(endpoint) {
      uploads.push(endpoint);
      assert.ok(!endpoint.includes("?"), "AstrBot native bridge rejects query strings in upload endpoints");
      if (endpoint === "transfer/upload/zip-token") return {job: {id: "zip-id", warnings: []}, astrbot_version: "4.27", plugins: [{...plugin, exists: false, config: true, data: true, problem: ""}]};
      assert.equal(endpoint, "manage/manifest-upload/source-token");
      return {job: {id: "install-id", warnings: []}, expires_at: 2000000000, plugins: [
        {market_plugin_id: "Alice/new", name: "new", directory: "new_dir", status: "install", observed_version: "1.0", version: "2.0"},
        {market_plugin_id: "Alice/existing", name: "existing", status: "skip", observed_version: "1.0", version: "2.0"},
        {market_plugin_id: "Alice/conflict", name: "conflict", status: "blocked", problem: "身份冲突"},
      ]};
    },
    async download(...args) {downloads.push(args);},
  };
  const context = vm.createContext({document, window: {AstrBotPluginPage: bridge, setInterval() {}}, console});
  const source = fs.readFileSync(path.join(root, "pages/transfer/app.js"), "utf8");
  assert.ok(!source.includes("innerHTML"));
  assert.ok(!source.includes("page-entry"));
  vm.runInContext(source.slice(0, source.lastIndexOf("initialize();")) + "globalThis.boot = initialize();", context);
  await context.boot;
  async function refresh() {await vm.runInContext("loadState(true)", context); await settle();}
  assert.equal(elements["boot-error"].hidden, true, elements["boot-error"].textContent);
  assert.equal(document.documentElement.dataset.theme, "dark");
  assert.ok(elements["plugin-list"].textContent.includes(record.local.display_name));

  // A slow storage scan or source lookup cannot delay the list or its controls.
  assert.match(elements["plugin-list"].textContent, /统计中/);
  assert.match(elements["plugin-list"].textContent, /来源读取中/);
  assert.equal(elements["manifest-file"].disabled, true);
  elements["select-all"].checked = true; await elements["select-all"].trigger("change");
  assert.equal(elements["selected-count"].textContent, "2");
  assert.equal(elements["selected-size"].textContent, "统计中…");
  assert.equal(elements["save-rule"].disabled, false);
  elements.search.value = "unknown"; await elements.search.trigger("input");
  assert.equal(elements["plugin-list"].children.length, 1);
  elements.search.value = ""; await elements.search.trigger("input");
  managementRequests[0].resolve(managementRequests[0].response); await settle();
  assert.equal(elements["manifest-file"].disabled, false);
  assert.match(elements["plugin-list"].textContent, /统计中/);
  const initialIcon = descendants(elements["plugin-list"]).find(element => element.tagName === "img");
  statisticsRequests[0].resolve(statisticsRequests[0].response); await settle();
  assert.doesNotMatch(elements["plugin-list"].textContent, /统计中/);
  assert.equal(elements["selected-count"].textContent, "2");
  assert.equal(elements["selected-size"].textContent, "2.0 KiB");
  assert.equal(descendants(elements["plugin-list"]).find(element => element.tagName === "img"), initialIcon);
  await elements["clear-selection"].trigger("click");
  delayStatistics = false; delayManagement = false;
  assert.match(elements["plugin-list"].children[0].children.at(-1).textContent, /官方市场/);
  assert.match(elements["plugin-list"].children[1].children.at(-1).textContent, /未绑定更新源/);
  const icon = descendants(elements["plugin-list"]).find(element => element.tagName === "img");
  assert.equal(icon.src, plugin.logo);
  assert.equal(descendants(elements["plugin-list"]).filter(element => element.tagName === "img")[1].src, "/favicon.svg");

  // The export sidebar is specific to its tab; switching keeps the current choices.
  assert.equal(elements["export-sidebar"].hidden, false);
  await tabs[1].trigger("click");
  assert.equal(elements["export-sidebar"].hidden, true);
  assert.equal(elements["import-panel"].hidden, false);
  assert.equal(document.documentElement.dataset.pageTab, "import-panel");
  await tabs[2].trigger("click");
  assert.equal(elements["export-sidebar"].hidden, true);
  assert.equal(elements["history-panel"].hidden, false);
  await tabs[0].trigger("click");
  assert.equal(elements["export-sidebar"].hidden, false);

  // Select-all follows the filter and preserves hidden selections.
  assert.equal(elements["bulk-data"].disabled, true);
  assert.equal(elements["clear-selection"].disabled, true);
  const firstChoice = descendants(elements["plugin-list"]).find(element => element.tagName === "input");
  firstChoice.checked = true; await firstChoice.trigger("change");
  assert.equal(elements["select-all"].indeterminate, true);
  elements.search.value = "unknown"; await elements.search.trigger("input");
  assert.equal(elements["select-all"].checked, false);
  assert.equal(elements["select-all"].indeterminate, false);
  elements["select-all"].checked = true; await elements["select-all"].trigger("change");
  assert.equal(elements["selection-label"].textContent, "已选 2 个插件");
  elements["select-all"].checked = false; await elements["select-all"].trigger("change");
  assert.equal(elements["selection-label"].textContent, "已选 1 个插件");
  elements.search.value = ""; await elements.search.trigger("input");
  assert.equal(elements["select-all"].indeterminate, true);
  state.plugins[1].blocked = "无法导出"; await refresh();
  elements["select-all"].checked = true; await elements["select-all"].trigger("change");
  assert.equal(elements["selected-count"].textContent, "1");
  state.plugins[1].blocked = ""; await refresh();
  await elements["clear-selection"].trigger("click");
  assert.equal(elements["selected-count"].textContent, "0");
  assert.equal(elements["select-all"].indeterminate, false);

  // Format changes retain unsaved ZIP choices and maintain a separate JSON selection.
  elements["select-all"].checked = true; await elements["select-all"].trigger("change");
  // Redrawing selections must preserve the decoded image: its token cannot be reused.
  assert.equal(descendants(elements["plugin-list"]).find(element => element.tagName === "img"), icon);
  await icon.trigger("error");
  assert.equal(icon.src, "/favicon.svg");
  await icon.trigger("error");
  assert.equal(icon.hidden, true);
  elements["bulk-data"].checked = true; await elements["bulk-data"].trigger("change");
  const firstDataChoice = descendants(elements["plugin-list"].children[0]).filter(element => element.tagName === "input")[1];
  firstDataChoice.checked = false; await firstDataChoice.trigger("change");
  assert.equal(elements["bulk-data"].indeterminate, true);
  assert.equal(elements["bulk-data"].checked, false);
  elements["bulk-data"].checked = true; await elements["bulk-data"].trigger("change");
  const zipSelections = vm.runInContext("JSON.stringify(selections())", context);
  assert.equal(elements["selected-count"].textContent, "2");
  elements["export-format"].value = "manifest"; await elements["export-format"].trigger("change");
  assert.equal(descendants(elements["plugin-list"]).find(element => element.tagName === "img"), icon);
  assert.equal(elements["selected-count"].textContent, "0");
  assert.equal(elements.export.disabled, true);
  elements["select-all"].checked = true; await elements["select-all"].trigger("change");
  assert.equal(elements["selected-count"].textContent, "1");
  assert.equal(elements["select-all"].checked, true);
  assert.equal(elements["bulk-data"].disabled, true);
  elements.search.value = "unknown"; await elements.search.trigger("input");
  assert.equal(elements["select-all"].disabled, true);
  elements.search.value = ""; await elements.search.trigger("input");
  assert.equal(elements.export.disabled, false);
  assert.match(elements["plugin-list"].textContent, /暂不能分享/);
  await elements.export.trigger("click");
  assert.deepEqual(Array.from(posts.find(item => item.endpoint === "manage/manifest-export").body.directories), ["demo_dir"]);
  assert.equal(downloads[0][0], "manage/manifest-download");
  elements["export-format"].value = "zip"; await elements["export-format"].trigger("change");
  assert.equal(vm.runInContext("JSON.stringify(selections())", context), zipSelections);
  assert.equal(elements["save-rule"].disabled, false);
  assert.equal(elements.export.disabled, false);

  // Source management opens from the plugin row and handles errors in its dialog.
  const sourceButton = descendants(elements["plugin-list"]).find(element => element.tagName === "button" && element.textContent === "来源管理");
  await sourceButton.trigger("click");
  assert.equal(elements["source-dialog"].open, true);
  elements["binding-kind"].value = "repository"; elements["binding-value"].value = binding.repo_url;
  failPreview = true; await elements["binding-form"].trigger("submit"); failPreview = false;
  assert.equal(elements["source-message"].textContent, "仓库身份不一致");
  assert.equal(elements["source-message"].hidden, false);
  await elements.refresh.trigger("click"); await settle();
  assert.equal(elements.message.hidden, true);
  assert.equal(elements["source-message"].hidden, true);
  await elements["binding-form"].trigger("submit");
  assert.equal(elements["save-binding"].disabled, false);
  await elements["save-binding"].trigger("click");
  assert.equal(posts.find(item => item.endpoint === "manage/source-bind").body.activate, true);
  assert.equal(elements["save-binding"].disabled, true);
  await elements["binding-form"].trigger("submit");
  // External changes invalidate the preview and update the current source.
  record.revision = "r2"; record.active_binding_id = null;
  await vm.runInContext("pollJobs()", context);
  assert.equal(elements["save-binding"].disabled, true);
  assert.ok(elements["source-summary"].textContent.includes("未绑定更新源"));
  const selectSource = descendants(elements["binding-list"]).find(element => element.tagName === "button");
  const selecting = selectSource.trigger("click"); await settle();
  assert.equal(elements.confirmation.open, true);
  elements.acknowledged.checked = true; await elements["confirmation-form"].trigger("submit"); await selecting;
  assert.ok(posts.some(item => item.endpoint === "manage/source-select" && item.body.revision === "r2"));
  // Closing during remote validation must not resurrect the dialog or preview.
  delayedPreview = {};
  const checking = elements["binding-form"].trigger("submit");
  assert.equal(elements["binding-value"].disabled, true);
  await elements["close-source"].trigger("click");
  delayedPreview.resolve({id: "late", binding}); await checking;
  assert.equal(vm.runInContext("bindingPreview", context), null);
  assert.equal(elements["source-dialog"].open, false);

  // Both import formats keep their previews when users switch between them.
  elements["bundle-file"].files = [{name: "plugins.zip", size: 100}];
  await elements["bundle-file"].trigger("change");
  const zipPreview = vm.runInContext("preview.job.id", context);
  elements["import-format"].value = "manifest"; await elements["import-format"].trigger("change");
  elements["manifest-file"].files = [{name: "plugins.json", size: 100}];
  await elements["manifest-file"].trigger("change");
  assert.equal(elements["manifest-preview"].hidden, false);
  assert.equal(vm.runInContext("installChoices.size", context), 1);
  const importChecks = descendants(elements["manifest-list"]).filter(element => element.tagName === "input");
  assert.deepEqual(importChecks.map(element => [element.checked, element.disabled]), [[true, false], [false, true], [false, true]]);
  elements["import-format"].value = "zip"; await elements["import-format"].trigger("change");
  assert.equal(elements["bundle-import"].hidden, false);
  assert.equal(vm.runInContext("preview.job.id", context), zipPreview);
  elements["import-format"].value = "manifest"; await elements["import-format"].trigger("change");
  assert.equal(vm.runInContext("manifestPreview.job.id", context), "install-id");
  assert.equal(elements["install-manifest"].disabled, false);
  const cancelled = elements["install-manifest"].trigger("click"); await settle();
  assert.equal(elements.confirmation.open, true);
  await elements["confirmation-cancel"].trigger("click"); await cancelled;
  assert.equal(posts.filter(item => item.endpoint === "manage/manifest-install").length, 0);
  const installing = elements["install-manifest"].trigger("click"); await settle();
  elements.acknowledged.checked = true;
  await elements["confirmation-form"].trigger("submit"); await installing;
  const installation = posts.find(item => item.endpoint === "manage/manifest-install");
  assert.deepEqual(Array.from(installation.body.ids), ["Alice/new"]);
  assert.equal(installation.body.acknowledged, true);
  assert.equal(elements["manifest-preview"].hidden, true);
  assert.equal(vm.runInContext("preview.job.id", context), zipPreview);
  assert.equal(elements["history-panel"].hidden, false);
  assert.equal(elements["export-sidebar"].hidden, true);

  // A shared history supports all four task kinds and the correct downloads.
  const job = {status: "completed", created_at: 100, phase: "完成", results: [], warnings: [], expired: false};
  state.jobs = [{...job, kind: "export", id: "zip-out"}, {...job, kind: "import", id: "zip-in", backup_ready: true}, {...job, kind: "manifest_export", id: "json-out"}, {...job, kind: "manifest_install", id: "json-in"}];
  await vm.runInContext("pollJobs()", context);
  assert.equal(elements["job-count"].textContent, "4");
  for (const text of ["导出插件包", "导入插件包", "导出插件清单", "清单安装"]) assert.ok(elements["job-list"].textContent.includes(text));
  const downloadButtons = descendants(elements["job-list"]).filter(element => element.tagName === "button");
  assert.equal(downloadButtons.length, 3);
  for (const button of downloadButtons) await button.trigger("click");
  assert.deepEqual(downloads.slice(1).map(args => [args[0], args[1].kind || "manifest"]), [["transfer/download", "package"], ["transfer/download", "rollback"], ["manage/manifest-download", "manifest"]]);
  // If the official association disappears, stale sharing selections are cleared.
  record.bindings = [];
  await refresh();
  elements["export-format"].value = "manifest"; await elements["export-format"].trigger("change");
  assert.equal(elements["selected-count"].textContent, "0");
  assert.equal(elements.export.disabled, true);

  // Source labels use the active binding, independently of installation history.
  record.provenance.method = "upload";
  record.bindings = [{...binding, kind: "repository"}]; record.active_binding_id = binding.id;
  await refresh();
  const sourceLabel = elements["plugin-list"].children[0].children.at(-1).children[0];
  assert.equal(sourceLabel.textContent, "Git 仓库");
  assert.ok(sourceLabel.title.includes(binding.repo_url));
  assert.ok(sourceLabel.title.includes("上传安装"));
  record.bindings = [{...binding, registry_url: "https://registry.example/plugins.json"}];
  await refresh();
  assert.match(elements["plugin-list"].children[0].children.at(-1).textContent, /自定义市场/);
  record.active_binding_id = null; await refresh();
  assert.match(elements["plugin-list"].children[0].children.at(-1).textContent, /未绑定更新源/);

  // Editing the author keeps the plugin selectable; sync only updates its record.
  elements["export-format"].value = "zip"; await elements["export-format"].trigger("change");
  record.metadata_changed = true; record.metadata_notice = "插件信息已变更：作者。";
  record.revision = "metadata-r1";
  await refresh();
  assert.match(elements["plugin-list"].textContent, /插件信息已变更：作者/);
  assert.match(elements["plugin-list"].children[0].textContent, /已打开/);
  let updateInfo = descendants(elements["plugin-list"]).find(element => element.tagName === "button" && element.textContent === "更新信息");
  assert.equal(updateInfo.disabled, false);
  const beforeSync = vm.runInContext("JSON.stringify(selections())", context);
  // Refresh dismisses the stale action error, while a failed refresh shows its own error.
  failMetadataSync = true; await updateInfo.trigger("click"); failMetadataSync = false;
  assert.equal(elements.message.textContent, "插件信息已变化，请刷新后重试。");
  assert.equal(elements.message.hidden, false);
  failState = true; await elements.refresh.trigger("click"); failState = false;
  assert.equal(elements.message.textContent, "插件状态读取失败");
  assert.equal(elements.message.hidden, false);
  await elements.refresh.trigger("click"); await settle();
  assert.equal(elements.message.hidden, true);
  assert.equal(elements.message.textContent, "");
  assert.equal(vm.runInContext("JSON.stringify(selections())", context), beforeSync);
  updateInfo = descendants(elements["plugin-list"]).find(element => element.tagName === "button" && element.textContent === "更新信息");
  await updateInfo.trigger("click");
  assert.ok(posts.some(item => item.endpoint === "manage/metadata-sync"));
  assert.doesNotMatch(elements["plugin-list"].textContent, /插件信息已变更：作者/);
  assert.equal(vm.runInContext("JSON.stringify(selections())", context), beforeSync);
  assert.ok(posts.every(item => !item.endpoint.includes("reload")));

  // Task execution locks selection controls without changing saved UI choices.
  elements["export-format"].value = "zip"; await elements["export-format"].trigger("change");
  const beforeBusy = vm.runInContext("JSON.stringify(selections())", context);
  state.busy = true; await vm.runInContext("loadState(true)", context);
  assert.equal(elements["select-all"].disabled, true);
  assert.equal(elements["clear-selection"].disabled, true);
  assert.equal(elements["bulk-data"].disabled, true);
  elements["select-all"].checked = false; await elements["select-all"].trigger("change");
  await elements["clear-selection"].trigger("click");
  assert.equal(vm.runInContext("JSON.stringify(selections())", context), beforeBusy);

  // Refreshes may finish out of order; older details must not replace newer data.
  state.busy = false; delayStatistics = true; delayManagement = true;
  await refresh();
  const oldStatistics = statisticsRequests.at(-1), oldManagement = managementRequests.at(-1);
  state.plugins[0].code_size = 4096;
  record.bindings = [{...binding, kind: "repository"}]; record.active_binding_id = binding.id;
  await refresh();
  const latestStatistics = statisticsRequests.at(-1), latestManagement = managementRequests.at(-1);
  latestStatistics.resolve(latestStatistics.response); latestManagement.resolve(latestManagement.response); await settle();
  assert.equal(elements["plugin-list"].children[0].children[4].textContent, "4.0 KiB");
  assert.match(elements["plugin-list"].children[0].children.at(-1).textContent, /Git 仓库/);
  oldStatistics.resolve(oldStatistics.response); oldManagement.resolve(oldManagement.response); await settle();
  assert.equal(elements["plugin-list"].children[0].children[4].textContent, "4.0 KiB");
  assert.match(elements["plugin-list"].children[0].children.at(-1).textContent, /Git 仓库/);

  // An identity change during the scan cannot attach sizes to a different plugin.
  await refresh();
  const changed = statisticsRequests.at(-1);
  changed.response.plugins[0].plugin_id = "Someone/else";
  changed.resolve(changed.response);
  managementRequests.at(-1).resolve(managementRequests.at(-1).response); await settle();
  assert.match(elements["plugin-list"].textContent, /插件信息已变化，请刷新状态/);
  assert.equal(elements["plugin-list"].children[0].children[4].textContent, "—");

  // Failure keeps the list usable and never presents unknown statistics as zero.
  delayStatistics = false; delayManagement = false; failStatistics = true; failManagement = true;
  await refresh();
  assert.equal(elements["boot-error"].hidden, true);
  assert.match(elements["plugin-list"].textContent, /统计失败/);
  assert.equal(elements["plugin-list"].children[0].children[4].textContent, "统计失败");
  assert.match(elements["plugin-list"].children[0].children.at(-1).children[0].title, /来源刷新失败/);
  await elements["clear-selection"].trigger("click");
  elements["select-all"].checked = true; await elements["select-all"].trigger("change");
  assert.equal(elements["selected-count"].textContent, "2");
  assert.equal(elements["selected-size"].textContent, "暂不可用");
  assert.equal(elements["save-rule"].disabled, false);
  vm.runInContext("managementState = {plugins: []}", context);
  await refresh();
  assert.match(elements["plugin-list"].textContent, /来源读取失败/);
  assert.equal(elements["manifest-file"].disabled, true);
  // A successful retry fills in values without discarding the current selection.
  failStatistics = false; failManagement = false;
  await refresh();
  assert.doesNotMatch(elements["plugin-list"].textContent, /统计失败|统计中/);
  assert.equal(elements["selected-count"].textContent, "2");
  assert.equal(elements["selected-size"].textContent, "9.0 KiB");
}
main().catch(error => {console.error(error); process.exitCode = 1;});
