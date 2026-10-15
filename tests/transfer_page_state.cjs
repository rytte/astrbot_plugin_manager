"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.dataset = {}; this.listeners = {}; this.attributes = {};
    this.value = ""; this.hidden = false; this.disabled = false; this.checked = false;
    this.classList = {toggle() {}}; this.content = "";
  }
  set textContent(value) {this.content = String(value); this.children = [];}
  get textContent() {return this.content + this.children.map(child => child.textContent).join("");}
  get childNodes() {return this.children;}
  append(...children) {this.children.push(...children);}
  replaceChildren(...children) {this.children = children; this.content = "";}
  setAttribute(key, value) {this.attributes[key] = value;}
  addEventListener(event, handler) {(this.listeners[event] ||= []).push(handler);}
  async trigger(event, payload = {}) {for (const handler of this.listeners[event] || []) await handler({target: this, preventDefault() {}, ...payload});}
  contains(element) {return element === this || this.children.some(child => child.contains(element));}
  click() {return this.trigger("click");}
  focus() {if (!this.disabled) Element.document.activeElement = this;}
  scrollIntoView() {this.scrolledIntoView = true;}
  showModal() {this.open = true;}
  close() {this.open = false;}
}
const settle = () => new Promise(resolve => setImmediate(resolve));
function descendants(element) {return element.children.flatMap(child => [child, ...descendants(child)]);}

async function main() {
  const root = process.argv[2];
  const html = fs.readFileSync(path.join(root, "pages/transfer/index.html"), "utf8");
  assert.ok(!html.includes("恢复资料使用说明"));
  const elements = Object.fromEntries(Array.from(html.matchAll(/id="([^"]+)"/g), match => {
    const element = new Element("div"); element.id = match[1]; return [match[1], element];
  }));
  const tabs = Array.from(html.matchAll(/data-tab="([^"]+)"/g), match => {
    const element = new Element("button"); element.dataset.tab = match[1]; return element;
  });
  elements["boot-error"].hidden = true; elements["status-filter"].value = "all";
  elements["export-format"].value = "zip"; elements["import-format"].value = "zip";
  const document = {
    hidden: false, documentElement: {dataset: {}}, listeners: {},
    addEventListener: Element.prototype.addEventListener, trigger: Element.prototype.trigger,
    querySelectorAll(selector) {return selector === "[id]" ? Object.values(elements) : tabs;},
    createElement(tag) {return new Element(tag);},
    createTextNode(text) {const element = new Element("text"); element.textContent = text; return element;},
  };
  Element.document = document;
  const binding = {id: "official", kind: "market", registry_url: null, market_plugin_id: "Alice/demo", repo_url: "https://github.com/alice/demo", verification: "observed"};
  const record = {
    revision: "r1", identity_changed: false, current_source: binding, pending_source: null, history: [],
    provenance: {method: "market", evidence: "core_record_observed", observed_at: "2026-10-11"},
    local: {directory: "demo_dir", name: "demo", author: "Alice", display_name: "<img src=x onerror=alert(1)>", status: "enabled", version: "1.0.0", problem: "", reserved: false},
  };
  const unbound = structuredClone(record); unbound.local.directory = "unknown_dir"; unbound.local.name = "unknown"; unbound.current_source = null; unbound.provenance.method = "unknown";
  const management = {csrf: "source-token", busy: false, plugins: [record, unbound], jobs: [], updates: {id: null, status: "idle", results: [], done: 0, total: 0}};
  const plugin = {...record.local, plugin_id: "Alice/demo", logo: "/api/file/icon-token", blocked: "", ranges: [], data_available: true, config_available: true, code_size: 1024, config_size: 32, data_size: 2048, kv_count: 1};
  const state = {csrf: "zip-token", busy: false, plugins: [plugin, {...plugin, ...unbound.local, logo: null}], jobs: [], rule: {updated_at: null, selections: []}, limits: {archive_bytes: 268435456}};
  const posts = [], downloads = [], uploads = [];
  const statisticsRequests = [], managementRequests = [];
  let delayStatistics = true, delayManagement = true, failStatistics = false, failManagement = false;
  let failState = false, failMetadataSync = false, failDelete = false;
  let pendingDownload = null;
  let pendingZipUpload = null;
  let delayedPreview, failPreview = false;
  let manifestResponse = null, failDraft = false;
  let updateRequests = 0, failUpdate = false;
  let failLoad = false, pendingLoad = null;
  let failRecovery = false;
  const recoveryResponse = {job: {id: "recovery-id", source_id: "zip-in", kind: "rollback", status: "preview", created_at: 100, phase: "还原预检完成", results: [], warnings: [], expired: false}, expires_at: 2000000000, plugins: [{directory: "demo_dir", name: "demo", display_name: "示例插件", components: {code: "replace", config: "remove", data: "replace"}, problem: ""}]};
  const bridge = {
    async ready() {}, onContext(handler) {handler({isDark: true});},
    async apiGet(endpoint, params) {
      if (endpoint === "manage/updates") {updateRequests += 1; return structuredClone(management.updates);}
      if (endpoint === "manage/manifest-preview") {
        const job = state.jobs.find(job => job.id === params.id);
        return job && ["queued", "running"].includes(job.status) ? {ready: false, job: structuredClone(job)} : structuredClone(manifestResponse);
      }
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
      if (endpoint === "transfer/rollback-plan") return structuredClone(recoveryResponse);
      throw new Error("Unexpected endpoint " + endpoint);
    },
    async apiPost(endpoint, body) {
      posts.push({endpoint, body});
      if (endpoint.startsWith("manage/")) assert.equal(body.csrf, "source-token");
      if (endpoint === "manage/check-updates") {
        assert.deepEqual(Object.keys(body), ["csrf"]);
        management.updates = {id: "check-id", status: "running", checked_at: "2026-10-12T10:00:00Z", done: 0, total: 1, results: [record.current_source].filter(Boolean).map(source => ({directory: record.local.directory, binding_id: source.id, revision: record.revision, status: "pending", local_version: "1.0.0", remote_version: "", message: "等待检查", can_update: false}))};
        return structuredClone(management.updates);
      }
      if (endpoint === "manage/update-plugin") {
        if (failUpdate) throw new Error("远端版本或来源已变化，请重新检查后更新。");
        assert.equal(body.check_id, management.updates.id);
        assert.equal(body.binding_id, record.current_source.id);
        assert.equal(body.acknowledged, true);
        const job = {id: "update-job", kind: "plugin_update", status: "running", created_at: 100, phase: "正在更新", results: [], warnings: [], expired: false};
        state.jobs.unshift(job); state.busy = true;
        management.updates.results[0].status = "updating";
        management.updates.results[0].can_update = false;
        return structuredClone(job);
      }
      if (endpoint === "manage/load-plugin") {
        if (failLoad) throw new Error("插件信息或状态已变化，请刷新列表后重新确认加载。");
        assert.equal(body.directory, "pending_dir");
        assert.equal(body.revision, "pending-r1");
        assert.equal(body.acknowledged, true);
        state.busy = true;
        return new Promise(resolve => {pendingLoad = {resolve};});
      }
      if (endpoint === "transfer/delete-job") {
        assert.equal(body.csrf, "zip-token"); assert.equal(body.acknowledged, true);
        if (failDelete) throw new Error("任务文件删除失败，请重试。");
        state.jobs = state.jobs.filter(job => job.id !== body.id);
        return {id: body.id, deleted: true};
      }
      if (endpoint === "transfer/rollback-preview") {
        assert.equal(body.csrf, "zip-token");
        state.jobs.unshift(structuredClone(recoveryResponse.job));
        return structuredClone(recoveryResponse);
      }
      if (endpoint === "transfer/rollback") {
        assert.equal(body.csrf, "zip-token"); assert.equal(body.acknowledged, true);
        if (failRecovery) throw new Error("目标数据在预检后变化，请重新预检。");
        const job = state.jobs.find(job => job.id === body.id);
        job.status = "running"; state.busy = true;
        return structuredClone(job);
      }
      if (["manage/source-preview", "manage/source-revalidate"].includes(endpoint)) {
        if (failPreview) throw new Error("仓库身份不一致");
        if (delayedPreview) return new Promise(resolve => {delayedPreview.resolve = resolve;});
        return {id: "preview", binding};
      }
      if (endpoint === "manage/source-bind") {assert.equal(body.acknowledged, true); assert.ok(!("activate" in body)); record.current_source = binding; record.pending_source = null; return structuredClone(record);}
      if (endpoint === "manage/metadata-sync") {
        if (failMetadataSync) throw new Error("插件信息已变化，请刷新后重试。");
        assert.equal(body.directory, record.local.directory);
        assert.equal(body.revision, "metadata-r1");
        record.metadata_changed = false; record.metadata_notice = "";
        record.revision = "metadata-r2";
        return structuredClone(record);
      }
      if (endpoint === "manage/manifest-export") {
        const job = {id: "export-id", kind: "manifest_export", name: body.name, download_filename: body.name + ".json", status: "completed", created_at: 100, phase: "清单已导出", results: [], warnings: [], expired: false};
        state.jobs.unshift(job);
        return structuredClone(job);
      }
      if (endpoint === "manage/manifest-draft") {
        if (failDraft) throw new Error("选择保存失败");
        manifestResponse.draft = structuredClone(body.draft); return {saved: true};
      }
      if (endpoint === "manage/manifest-recheck") {
        const job = {...manifestResponse.job, id: "rechecked-id", status: "running", phase: "重新解析来源", preview_expired: false};
        manifestResponse = {...manifestResponse, job: {...job, status: "preview"}, draft: null};
        state.jobs.unshift(job); state.busy = true;
        return {ready: false, job};
      }
      if (endpoint === "manage/manifest-install") {
        const job = state.jobs.find(job => job.id === body.id);
        if (job) {job.status = "completed"; job.preview_task = false;}
        return {id: "install-id"};
      }
      throw new Error("Unexpected endpoint " + endpoint);
    },
    async upload(endpoint) {
      uploads.push(endpoint);
      assert.ok(!endpoint.includes("?"), "AstrBot native bridge rejects query strings in upload endpoints");
      if (endpoint === "transfer/upload/zip-token") {
        const response = {job: {id: "zip-id", warnings: []}, astrbot_version: "4.27", plugins: [{...plugin, exists: false, config: true, data: true, problem: ""}]};
        if (pendingZipUpload) return new Promise((resolve, reject) => Object.assign(pendingZipUpload, {resolve, reject, response}));
        return response;
      }
      assert.equal(endpoint, "manage/manifest-upload/source-token");
      manifestResponse = {ready: true, job: {id: "install-id", name: "生产环境备份", kind: "manifest_install", status: "preview", phase: "等待选择", preview_task: true, preview_expires_at: 2000000000, created_at: 100, results: [], warnings: []}, expires_at: 2000000000, plugins: [
        {id: "0", display_name: "新插件", market_plugin_id: "Alice/new", repo_url: "https://github.com/alice/new", default_source: "market", sources: [{kind: "market", name: "new", directory: "demo_dir", status: "install", version: "2.0", logo: "https://example.com/market.png"}, {kind: "repository", name: "new", directory: "demo_dir", status: "install", version: "2.1", logo: "/api/file/repository-icon"}]},
        {id: "1", display_name: "已有插件", market_plugin_id: "Alice/existing", repo_url: "", default_source: "market", sources: [{kind: "market", name: "existing", status: "skip", version: "2.0"}]},
        {id: "2", display_name: "冲突插件", market_plugin_id: "Alice/conflict", repo_url: "", default_source: "market", sources: [{kind: "market", name: "conflict", status: "blocked", problem: "身份冲突"}]},
      ]};
      const job = {...manifestResponse.job, status: "running", phase: "正在下载仓库包"};
      state.jobs.unshift(job); state.busy = true;
      return {ready: false, job};
    },
    async download(...args) {
      downloads.push(args);
      if (pendingDownload) return new Promise((resolve, reject) => {pendingDownload.resolve = resolve; pendingDownload.reject = reject;});
    },
  };
  let timerClock = 0, timerSerial = 0;
  const messageTimers = new Map();
  class MessageDate extends Date {static now() {return timerClock;}}
  function tickMessages(duration) {
    const deadline = timerClock + duration;
    while (true) {
      const next = [...messageTimers].filter(([, timer]) => timer.at <= deadline).sort((first, second) => first[1].at - second[1].at)[0];
      if (!next) break;
      timerClock = next[1].at; messageTimers.delete(next[0]); next[1].handler();
    }
    timerClock = deadline;
  }
  const context = vm.createContext({document, Date: MessageDate, window: {
    AstrBotPluginPage: bridge, setInterval() {},
    setTimeout(handler, delay) {const id = ++timerSerial; messageTimers.set(id, {handler, at: timerClock + delay}); return id;},
    clearTimeout(id) {messageTimers.delete(id);},
  }, console});
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

  // A manual check covers all current sources even with a filter, and polls only while running.

  elements.search.value = "unknown"; await elements.search.trigger("input");
  await elements["check-updates"].trigger("click");
  assert.equal(elements["check-updates"].disabled, true);
  assert.match(elements["update-summary"].textContent, /0 \/ 1/);
  await vm.runInContext("pollJobs()", context);
  assert.equal(updateRequests, 1);
  management.updates.status = "completed"; management.updates.done = 1;
  for (const result of management.updates.results) Object.assign(result, {status: "update_available", message: "发现新版本", remote_version: "2.0.0", can_update: true});
  await vm.runInContext("pollJobs()", context);
  assert.match(elements["update-summary"].textContent, /当前更新源有新版本：1/);
  await vm.runInContext("pollJobs()", context);
  assert.equal(updateRequests, 2, "Completed checks must not keep fetching source status");
  elements.search.value = ""; await elements.search.trigger("input");
  const updateButtons = () => descendants(elements["plugin-list"]).filter(element => element.tagName === "button" && element.className === "plugin-update-badge");
  assert.equal(updateButtons().length, 1);
  assert.equal(updateButtons()[0].textContent, "2.0.0");
  assert.equal(updateButtons()[0].attributes["aria-label"], "更新 demo 至 2.0.0");
  assert.equal(updateButtons()[0].disabled, false);
  assert.equal(updateButtons()[0].children[0].className, "plugin-update-icon");
  assert.equal(updateButtons()[0].children[0].attributes["aria-hidden"], "true");
  const updateHeading = descendants(elements["plugin-list"]).find(element => element.className === "plugin-heading");
  const updateBadges = updateHeading.children.find(element => element.className === "plugin-badges");
  const statusIndex = updateBadges.children.findIndex(element => element.className === "plugin-status enabled");
  assert.ok(statusIndex >= 0);
  assert.equal(updateBadges.children[statusIndex + 1], updateButtons()[0]);
  assert.match(elements["plugin-list"].textContent, /v1.0.0/);
  assert.doesNotMatch(elements["plugin-list"].textContent, /本机 .* → 来源|发现新版本/);
  assert.ok(!descendants(elements["plugin-list"]).some(element => element.tagName === "button" && element.textContent === "更新"));
  assert.doesNotMatch(elements["plugin-list"].textContent, /3.0.0/);
  vm.runInContext("busy = true; renderPlugins()", context);
  assert.equal(updateButtons()[0].disabled, true);
  vm.runInContext("busy = false; requesting = true; renderPlugins()", context);
  assert.equal(updateButtons()[0].disabled, true);
  vm.runInContext("requesting = false; managementStatus = 'failed'; renderPlugins()", context);
  assert.equal(updateButtons()[0].disabled, true);
  vm.runInContext("managementStatus = 'ready'; renderPlugins()", context);
  management.updates.results[0].can_update = false;
  await vm.runInContext("loadManagement()", context);
  assert.equal(updateButtons()[0].disabled, true);
  management.updates.results[0].can_update = true;
  await vm.runInContext("loadManagement()", context);
  assert.equal(updateButtons()[0].disabled, false);
  record.history = [
    {at: "2026-10-15T15:35:00Z", action: "metadata_synced", actor: "astrbot"},
    {at: "2026-10-15T15:34:00Z", action: "core_observed", actor: "AstrBot"},
    {at: "2026-10-15T15:33:00Z", action: "source_repaired", actor: "admin"},
  ];
  await vm.runInContext("openSource('demo_dir')", context);
  assert.match(elements["source-summary"].textContent, /来源 2.0.0/);
  assert.match(elements["source-history"].textContent, /AstrBot/);
  assert.doesNotMatch(elements["source-history"].textContent, / · astrbot/);
  assert.match(elements["source-history"].textContent, / · admin/);
  assert.equal(elements["source-repair"].hidden, true);
  assert.equal(descendants(elements["source-summary"]).filter(element => element.tagName === "button" && element.textContent === "更新").length, 1);
  await elements["close-source"].trigger("click");
  record.revision = "source-changed";
  await vm.runInContext("loadManagement()", context);
  assert.equal(updateButtons().length, 0);
  assert.match(elements["plugin-list"].textContent, /重新检查/);
  record.revision = "r1"; await vm.runInContext("loadManagement()", context);
  const cancelledUpdate = updateButtons()[0].trigger("click"); await settle();
  assert.equal(elements.confirmation.open, true);
  assert.equal(elements["confirmation-title"].textContent, "确认更新插件");
  assert.equal(elements["confirmation-notices"].textContent, "将通过 AstrBot 下载、处理依赖并加载插件，保持原来的运行状态。");
  const confirmationDetails = descendants(elements["confirmation-list"]);
  assert.equal(confirmationDetails.find(element => element.className === "confirmation-plugin-id").textContent, "demo_dir");
  assert.equal(confirmationDetails.find(element => element.className === "confirmation-update-source").textContent, "官方市场 / Alice/demo");
  assert.equal(confirmationDetails.find(element => element.className === "confirmation-version-current").textContent, "1.0.0");
  assert.equal(confirmationDetails.find(element => element.className === "confirmation-version-target").textContent, "2.0.0");
  assert.equal(confirmationDetails.find(element => element.className === "confirmation-version-arrow").textContent, "→");
  assert.equal(confirmationDetails.some(element => element.tagName === "img"), false);
  assert.equal(elements["confirmation-acknowledgement"].hidden, true);
  assert.equal(elements.acknowledged.checked, false);
  assert.equal(elements.acknowledged.required, false);
  assert.equal(elements.acknowledged.disabled, true);
  await elements["confirmation-cancel"].trigger("click"); await cancelledUpdate;
  assert.ok(!posts.some(item => item.endpoint === "manage/update-plugin"));
  failUpdate = true;
  const rejectedUpdate = updateButtons()[0].trigger("click"); await settle();
  await elements["confirmation-form"].trigger("submit"); await rejectedUpdate;
  assert.match(elements.message.textContent, /远端版本或来源已变化/);
  assert.equal(updateButtons().length, 1);
  failUpdate = false;
  const updating = updateButtons()[0].trigger("click"); await settle();
  assert.equal(elements.acknowledged.checked, false);
  await elements["confirmation-form"].trigger("submit"); await updating;
  assert.equal(updateButtons().length, 0);
  assert.match(elements["job-list"].textContent, /更新插件/);
  state.busy = false; state.jobs[0].status = "completed";
  record.local.version = "2.0.0"; state.plugins[0].version = "2.0.0"; record.revision = "updated";
  management.updates.results[0].status = "updated"; management.updates.results[0].message = "已更新至 2.0.0";
  await vm.runInContext("pollJobs()", context); await settle();
  assert.match(elements["plugin-list"].textContent, /已更新至 2.0.0/);
  assert.equal(updateButtons().length, 0);
  // Restore the base fixture for the remaining export/import and source scenarios.
  record.local.version = "1.0.0"; record.revision = "r1"; state.plugins[0].version = "1.0.0";
  management.updates = {id: null, status: "idle", done: 0, total: 0, results: []};
  state.jobs = state.jobs.filter(job => job.kind !== "plugin_update");
  await refresh();

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
  elements["export-name"].value = "生产环境备份";
  await elements.export.trigger("click");
  assert.deepEqual(Array.from(posts.find(item => item.endpoint === "manage/manifest-export").body.directories), ["demo_dir"]);
  assert.equal(downloads[0][0], "manage/manifest-download");
  assert.equal(posts.find(item => item.endpoint === "manage/manifest-export").body.name, "生产环境备份");
  assert.equal(downloads[0][2], "生产环境备份.json");
  assert.match(elements["job-list"].textContent, /生产环境备份/);
  const manifestDownload = descendants(elements["job-list"]).find(element => element.tagName === "button" && element.textContent.includes("下载清单 JSON"));
  assert.ok(manifestDownload);
  assert.equal(manifestDownload.disabled, false, "A completed manifest remains downloadable immediately after export finishes");
  elements["export-format"].value = "zip"; await elements["export-format"].trigger("change");
  assert.equal(vm.runInContext("JSON.stringify(selections())", context), zipSelections);
  assert.equal(elements["save-rule"].disabled, false);
  assert.equal(elements.export.disabled, false);

  // Missing sources can be repaired; ordinary switching stays in the official UI.
  record.current_source = null; await refresh();
  const sourceButton = descendants(elements["plugin-list"]).find(element => element.tagName === "button" && element.textContent === "来源详情");
  await sourceButton.trigger("click");
  assert.equal(elements["source-dialog"].open, true);
  elements["binding-kind"].value = "repository"; elements["binding-value"].value = binding.repo_url;
  failPreview = true; await elements["binding-form"].trigger("submit"); failPreview = false;
  assert.match(elements["source-message"].textContent, /仓库身份不一致/);
  assert.equal(elements["source-message"].hidden, false);
  assert.equal(elements.message.hidden, true, "Dialog feedback must not also appear behind the dialog");
  await elements.refresh.trigger("click"); await settle();
  assert.equal(elements.message.hidden, true);
  assert.equal(elements["source-message"].hidden, true);
  await elements["binding-form"].trigger("submit");
  assert.equal(elements["save-binding"].disabled, false);
  await elements["save-binding"].trigger("click");
  assert.equal(posts.find(item => item.endpoint === "manage/source-bind").body.acknowledged, true);
  assert.equal(elements["source-repair"].hidden, true);
  assert.equal(elements["save-binding"].disabled, true);
  await descendants(elements["source-summary"]).find(element => element.tagName === "button" && element.textContent === "重新校验当前来源").trigger("click");
  // External changes invalidate the preview and update the current source.
  record.revision = "r2"; record.current_source = null;
  await vm.runInContext("pollJobs()", context);
  assert.equal(elements["save-binding"].disabled, true);
  assert.ok(elements["source-summary"].textContent.includes("未绑定更新源"));
  // Imported locators remain visible but cannot be shared before revalidation.
  record.pending_source = structuredClone(binding);
  record.provenance.evidence = "bundle_record";
  await elements.refresh.trigger("click"); await settle();
  assert.match(elements["source-summary"].textContent, /备份来源尚未生效/);
  assert.match(elements["source-pending"].textContent, /备份来源待校验/);
  assert.match(elements["source-provenance"].textContent, /备份中的安装来源记录/);
  elements["export-format"].value = "manifest"; await elements["export-format"].trigger("change");
  assert.equal(elements.export.disabled, true);
  assert.match(elements["plugin-list"].textContent, /暂不能分享/);
  elements["export-format"].value = "zip"; await elements["export-format"].trigger("change");
  const validateImported = descendants(elements["source-pending"]).find(element => element.tagName === "button");
  await validateImported.trigger("click");
  assert.equal(elements["source-validation"].hidden, false);
  await elements["save-binding"].trigger("click");
  assert.ok(posts.some(item => item.endpoint === "manage/source-revalidate" && item.body.revision === "r2"));
  assert.equal(record.pending_source, null);
  assert.equal(elements["source-pending"].hidden, true);
  assert.equal(elements["source-repair"].hidden, true);
  assert.ok(!posts.some(item => item.endpoint === "manage/source-select"));
  record.current_source = null; await refresh();
  // Closing during remote validation must not resurrect the dialog or preview.
  delayedPreview = {};
  const checking = elements["binding-form"].trigger("submit");
  assert.equal(elements["binding-value"].disabled, true);
  await elements["close-source"].trigger("click");
  delayedPreview.resolve({id: "late", binding}); await checking;
  assert.equal(vm.runInContext("bindingPreview", context), null);
  assert.equal(elements["source-dialog"].open, false);

  record.current_source = binding; await refresh();

  // ZIP preview stays visibly pending, clears failures, and can retry the same file.
  elements["bundle-file"].files = [{name: "plugins.zip", size: 100}];
  elements["bundle-file"].value = "plugins.zip";
  pendingZipUpload = {};
  let zipUploading = elements["bundle-file"].trigger("change");
  assert.equal(elements["bundle-status"].hidden, false);
  assert.equal(elements["bundle-status"].dataset.status, "preparing");
  assert.match(elements["bundle-status-title"].textContent, /正在上传并预检/);
  assert.equal(elements["file-label"].textContent, "plugins.zip · 100 B");
  assert.equal(elements["bundle-upload"].attributes["aria-busy"], "true");
  assert.equal(elements["bundle-file"].disabled, true);
  assert.equal(elements["execute-import"].disabled, true);
  pendingZipUpload.reject(new Error("ZIP 无效或已损坏。")); await zipUploading;
  assert.equal(elements["bundle-status"].dataset.status, "failed");
  assert.equal(elements["bundle-status"].attributes.role, "alert");
  assert.match(elements["bundle-status-detail"].textContent, /ZIP 无效或已损坏.*重新选择/);
  assert.equal(elements["bundle-upload"].attributes["aria-busy"], "false");
  assert.equal(elements["bundle-file"].disabled, false);
  assert.equal(elements["bundle-file"].value, "");
  assert.equal(elements["import-preview"].hidden, true);
  assert.equal(vm.runInContext("preview", context), null);
  pendingZipUpload = {};
  zipUploading = elements["bundle-file"].trigger("change");
  assert.equal(elements["bundle-status"].dataset.status, "preparing");
  assert.equal(elements.message.hidden, true);
  pendingZipUpload.resolve(pendingZipUpload.response); await zipUploading;
  assert.equal(elements["bundle-status"].dataset.status, "completed");
  assert.equal(elements["bundle-status"].attributes.role, "status");
  assert.match(elements["bundle-status-detail"].textContent, /已解析 1 个插件/);
  assert.equal(elements["execute-import"].disabled, false);
  const zipUploads = uploads.length;
  elements["bundle-file"].files = [{name: "oversized.zip", size: state.limits.archive_bytes + 1}];
  await elements["bundle-file"].trigger("change");
  assert.equal(uploads.length, zipUploads);
  assert.equal(elements["bundle-status"].dataset.status, "failed");
  assert.match(elements["bundle-status-detail"].textContent, /超过上传限制/);
  assert.equal(vm.runInContext("preview", context), null);
  assert.equal(vm.runInContext("importChoices.size", context), 0);
  assert.equal(elements["execute-import"].disabled, true);
  assert.equal(elements["import-preview"].hidden, true);
  pendingZipUpload = null;
  elements["bundle-file"].files = [{name: "plugins.zip", size: 100}];
  await elements["bundle-file"].trigger("change");

  // Both import formats keep their previews when users switch between them.
  const zipPreview = vm.runInContext("preview.job.id", context);
  const zipIcon = descendants(elements["import-list"]).find(element => element.tagName === "img");
  assert.equal(zipIcon.src, plugin.logo);
  assert.equal(zipIcon.referrerPolicy, "no-referrer");
  assert.equal(zipIcon.loading, "lazy");
  await vm.runInContext("renderImportPreview()", context);
  assert.equal(descendants(elements["import-list"]).find(element => element.tagName === "img"), zipIcon);
  assert.notEqual(descendants(elements["plugin-list"]).find(element => element.tagName === "img"), zipIcon);
  await zipIcon.trigger("error");
  assert.equal(zipIcon.src, "/favicon.svg");
  await zipIcon.trigger("error");
  assert.equal(zipIcon.hidden, true);
  elements["import-format"].value = "manifest"; await elements["import-format"].trigger("change");
  elements["manifest-file"].files = [{name: "plugins.json", size: 100}];
  await elements["manifest-file"].trigger("change");
  assert.equal(elements["manifest-progress"].hidden, false);
  assert.match(elements["manifest-progress"].textContent, /下载仓库包/);
  assert.equal(vm.runInContext("requesting", context), false, "Background preview must not block the page");
  vm.runInContext("clearManifestPreview(); showTab('history-panel'); renderJobs(displayedJobs)", context);
  const progressAction = descendants(elements["job-list"]).find(element => element.tagName === "button" && element.textContent === "查看预检进度");
  assert.ok(progressAction);
  assert.equal(progressAction.disabled, false, "An active background check remains readable while the instance is busy");
  await progressAction.trigger("click");
  assert.equal(elements["import-panel"].hidden, false);
  assert.equal(elements["manifest-progress"].hidden, false);
  Object.assign(state.jobs.find(job => job.id === "install-id"), manifestResponse.job); state.busy = false;
  await vm.runInContext("pollJobs()", context);
  assert.equal(elements["manifest-progress"].hidden, true);
  assert.equal(elements["manifest-preview"].hidden, false);
  assert.equal(elements["manifest-name"].textContent, "生产环境备份");
  assert.equal(vm.runInContext("installChoices.size", context), 1);
  const marketIcon = descendants(elements["manifest-list"]).find(element => element.tagName === "img");
  assert.equal(marketIcon.src, "https://example.com/market.png");
  assert.equal(marketIcon.referrerPolicy, "no-referrer");
  assert.equal(marketIcon.loading, "lazy");
  await vm.runInContext("renderManifest()", context);
  assert.equal(descendants(elements["manifest-list"]).find(element => element.tagName === "img"), marketIcon);
  await vm.runInContext("renderPlugins()", context);
  assert.ok(descendants(elements["plugin-list"]).includes(initialIcon), "Manifest icons must not take image nodes from the export list");
  const missingIcon = descendants(elements["manifest-list"]).filter(element => element.tagName === "img")[1];
  assert.equal(missingIcon.src, "/favicon.svg");
  const importChecks = descendants(elements["manifest-list"]).filter(element => element.tagName === "input");
  assert.deepEqual(importChecks.map(element => [element.checked, element.disabled]), [[true, false], [false, true], [false, true]]);
  const sourceSelect = descendants(elements["manifest-list"]).find(element => element.tagName === "select");
  assert.equal(sourceSelect.value, "market");
  sourceSelect.value = "repository"; await sourceSelect.trigger("change");
  const repositoryIcon = descendants(elements["manifest-list"]).find(element => element.tagName === "img");
  assert.equal(repositoryIcon.src, "/api/file/repository-icon");
  await vm.runInContext("renderManifest()", context);
  assert.equal(descendants(elements["manifest-list"]).find(element => element.tagName === "img"), repositoryIcon);
  await repositoryIcon.trigger("error");
  assert.equal(repositoryIcon.src, "/favicon.svg");
  await repositoryIcon.trigger("error");
  assert.equal(repositoryIcon.hidden, true);
  let currentSourceSelect = descendants(elements["manifest-list"]).find(element => element.tagName === "select");
  currentSourceSelect.value = "market"; await currentSourceSelect.trigger("change");
  assert.equal(descendants(elements["manifest-list"]).find(element => element.tagName === "img"), marketIcon);
  currentSourceSelect = descendants(elements["manifest-list"]).find(element => element.tagName === "select");
  currentSourceSelect.value = "repository"; await currentSourceSelect.trigger("change");
  assert.equal(descendants(elements["manifest-list"]).find(element => element.tagName === "img"), repositoryIcon);
  assert.match(elements["manifest-list"].textContent, /https:\/\/github.com\/alice\/new/);
  assert.equal(vm.runInContext("selectedManifestSource(manifestPreview.plugins[0]).version", context), "2.1");
  assert.equal(manifestResponse.draft[0].source, "repository");
  failDraft = true;
  let savedCheck = descendants(elements["manifest-list"]).find(element => element.tagName === "input");
  savedCheck.checked = false; await savedCheck.trigger("change");
  assert.equal(elements["retry-manifest-draft"].hidden, false);
  assert.equal(elements["install-manifest"].disabled, true);
  failDraft = false; await elements["retry-manifest-draft"].trigger("click");
  assert.equal(manifestResponse.draft[0].selected, false);
  vm.runInContext("clearManifestPreview(); showTab('history-panel'); renderJobs(displayedJobs)", context);
  const resumeAction = descendants(elements["job-list"]).find(element => element.tagName === "button" && element.textContent === "继续选择安装");
  assert.ok(resumeAction);
  await resumeAction.trigger("click");
  assert.equal(elements["import-panel"].hidden, false);
  assert.equal(elements["import-format"].value, "manifest");
  assert.equal(vm.runInContext("selectedManifestSource(manifestPreview.plugins[0]).kind", context), "repository");
  assert.equal(vm.runInContext("installChoices.size", context), 0, "A saved unchecked item must remain unchecked after reconnection");
  savedCheck = descendants(elements["manifest-list"]).find(element => element.tagName === "input");
  savedCheck.checked = true; await savedCheck.trigger("change");
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
  assert.deepEqual(JSON.parse(JSON.stringify(installation.body.selections)), [{id: "0", source: "repository"}]);
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
  const downloadButtons = descendants(elements["job-list"]).filter(element => element.tagName === "button" && element.textContent.startsWith("下载"));
  assert.equal(downloadButtons.length, 3);
  for (const button of downloadButtons) await button.trigger("click");
  assert.deepEqual(downloads.slice(1).map(args => [args[0], args[1].kind || "manifest"]), [["transfer/download", "package"], ["transfer/download", "rollback"], ["manage/manifest-download", "manifest"]]);
  // Downloads have their own lifecycle; other operations and polling cannot lock them.
  vm.runInContext("requesting = true; busy = true;", context);
  await vm.runInContext("renderJobs(displayedJobs)", context);
  assert.ok(descendants(elements["job-list"]).filter(element => element.tagName === "button" && element.textContent.startsWith("下载")).every(element => !element.disabled));
  const findManifestDownload = () => descendants(elements["job-list"]).find(element => element.tagName === "button" && element.textContent.includes("下载清单 JSON"));
  const beforeDownload = downloads.length;
  pendingDownload = {};
  const downloading = findManifestDownload().trigger("click"); await settle();
  assert.equal(downloads.length, beforeDownload + 1);
  let pendingButton = descendants(elements["job-list"]).find(element => element.tagName === "button" && element.textContent === "正在下载…");
  assert.equal(pendingButton.disabled, true);
  await vm.runInContext("pollJobs()", context);
  pendingButton = descendants(elements["job-list"]).find(element => element.tagName === "button" && element.textContent === "正在下载…");
  assert.equal(pendingButton.disabled, true);
  await pendingButton.trigger("click");
  assert.equal(downloads.length, beforeDownload + 1);
  pendingDownload.reject(new Error("清单下载失败，请重试。")); await downloading;
  pendingDownload = null;
  assert.match(elements.message.textContent, /清单下载失败，请重试。/);
  assert.equal(elements.message.hidden, false);
  assert.equal(findManifestDownload().disabled, false);
  await findManifestDownload().trigger("click");
  assert.match(elements.message.textContent, /已发起清单 JSON下载/);
  state.jobs[2].expired = true; await vm.runInContext("pollJobs()", context);
  const expiredDownload = descendants(elements["job-list"]).find(element => element.tagName === "button" && element.textContent === "文件已被旧版清理");
  assert.equal(expiredDownload.disabled, true);
  const beforeExpired = downloads.length;
  await expiredDownload.trigger("click");
  assert.equal(downloads.length, beforeExpired);
  vm.runInContext("requesting = false; busy = false;", context);
  // If the official association disappears, stale sharing selections are cleared.
  record.current_source = null;
  await refresh();
  elements["export-format"].value = "manifest"; await elements["export-format"].trigger("change");
  assert.equal(elements["selected-count"].textContent, "0");
  assert.equal(elements.export.disabled, true);

  // Source labels use the current source, independently of installation history.
  record.provenance.method = "upload";
  record.current_source = {...binding, kind: "repository"};
  await refresh();
  const sourceLabel = elements["plugin-list"].children[0].children.at(-1).children[0];
  assert.equal(sourceLabel.textContent, "Git 仓库");
  assert.ok(sourceLabel.title.includes(binding.repo_url));
  assert.ok(sourceLabel.title.includes("上传安装"));
  record.current_source = {...binding, registry_url: "https://registry.example/plugins.json"};
  await refresh();
  assert.match(elements["plugin-list"].children[0].children.at(-1).textContent, /自定义市场/);
  record.current_source = null; await refresh();
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
  assert.match(elements.message.textContent, /插件信息已变化，请刷新后重试。/);
  assert.equal(elements.message.hidden, false);
  failState = true; await elements.refresh.trigger("click"); failState = false;
  assert.match(elements.message.textContent, /插件状态读取失败/);
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
  record.current_source = {...binding, kind: "repository"};
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

  // Every job has an explicit deletion action; active work cannot be deleted.
  const statuses = ["preparing", "preview", "queued", "running", "completed", "completed_with_warning", "failed", "interrupted", "rollback_failed"];
  state.busy = false;
  state.jobs = statuses.map(status => ({id: status + "-id", kind: "import", status, created_at: 100, phase: "阶段", warnings: [], results: [], backup_ready: status === "rollback_failed"}));
  vm.runInContext("previousBusy = false", context);
  await vm.runInContext("pollJobs()", context);
  const taskCard = id => elements["job-list"].children.find(card => descendants(card).some(element => element.dataset.jobId === id));
  const deleteAction = id => descendants(taskCard(id)).find(element => element.tagName === "button" && element.textContent === "删除记录及文件");
  const menuToggle = id => descendants(taskCard(id)).find(element => element.className === "job-menu-toggle");
  const menuPanel = id => descendants(taskCard(id)).find(element => element.className === "job-menu-panel");
  for (const [index, status] of statuses.entries()) {
    const card = elements["job-list"].children[index];
    assert.equal(card.children.find(element => element.className === "job-meta").textContent, new Date(100 * 1000).toLocaleString("zh-CN"));
    assert.ok(!card.textContent.includes((status + "-id").slice(0, 8)));
    assert.ok(card.children[0].children[0].children[1].className.includes("job-status " + status));
    assert.equal(deleteAction(status + "-id").disabled, ["preparing", "queued", "running"].includes(status));
    assert.equal(menuPanel(status + "-id").hidden, true);
  }
  // Task polling preserves the open menu and keyboard focus until it is dismissed.
  await menuToggle("completed-id").trigger("click");
  assert.equal(menuPanel("completed-id").hidden, false);
  assert.equal(document.activeElement, deleteAction("completed-id"));
  const oldMenu = vm.runInContext("jobMenus.get('completed-id').menu", context);
  await vm.runInContext("pollJobs()", context);
  await oldMenu.trigger("focusout", {relatedTarget: null});
  assert.equal(menuPanel("completed-id").hidden, false);
  assert.equal(document.activeElement, deleteAction("completed-id"));
  await document.trigger("keydown", {key: "Escape"});
  assert.equal(menuPanel("completed-id").hidden, true);
  assert.equal(document.activeElement, menuToggle("completed-id"));
  await menuToggle("failed-id").trigger("keydown", {key: "ArrowDown"});
  assert.equal(menuPanel("failed-id").hidden, false);
  await menuToggle("completed-id").trigger("click");
  assert.equal(menuPanel("failed-id").hidden, true);
  assert.equal(menuPanel("completed-id").hidden, false);
  await document.trigger("click", {target: elements["history-panel"]});
  assert.equal(menuPanel("completed-id").hidden, true);
  const beforeDeletion = posts.filter(item => item.endpoint === "transfer/delete-job").length;
  await deleteAction("running-id").trigger("click");
  assert.equal(posts.filter(item => item.endpoint === "transfer/delete-job").length, beforeDeletion);
  await menuToggle("rollback_failed-id").trigger("click");
  let deleting = deleteAction("rollback_failed-id").trigger("click"); await settle();
  assert.equal(vm.runInContext("openJobMenuId", context), null);
  assert.equal(elements["delete-confirmation"].open, true);
  assert.equal(elements["delete-recovery-warning"].hidden, false);
  await elements["cancel-delete-job"].trigger("click"); await deleting;
  assert.equal(posts.filter(item => item.endpoint === "transfer/delete-job").length, beforeDeletion);
  deleting = deleteAction("failed-id").trigger("click"); await settle();
  failDelete = true;
  await elements["delete-job-form"].trigger("submit"); await deleting; failDelete = false;
  assert.match(elements.message.textContent, /任务文件删除失败/);
  assert.ok(state.jobs.some(job => job.id === "failed-id"));
  deleting = deleteAction("failed-id").trigger("click"); await settle();
  await elements["delete-job-form"].trigger("submit"); await deleting;
  assert.ok(!state.jobs.some(job => job.id === "failed-id"));
  assert.equal(elements["job-count"].textContent, "8");
  assert.match(elements.message.textContent, /任务记录及相关文件已删除/);

  // Deleting a confirmed preview clears the corresponding import controls.
  vm.runInContext("preview = {job: {id: 'preview-id', warnings: []}, plugins: []}; renderImportPreview()", context);
  deleting = deleteAction("preview-id").trigger("click"); await settle();
  await elements["delete-job-form"].trigger("submit"); await deleting;
  assert.equal(elements["import-preview"].hidden, true);
  assert.equal(vm.runInContext("preview", context), null);
  assert.equal(elements["bundle-status"].dataset.status, "failed");
  assert.match(elements["bundle-status-title"].textContent, /已失效/);

  state.jobs.push({id: "manifest-preview-id", kind: "manifest_install", status: "preview", created_at: 100, warnings: [], results: []});
  await vm.runInContext("pollJobs()", context);
  vm.runInContext("manifestPreview = {job: {id: 'manifest-preview-id', warnings: []}, plugins: []}; installChoices.add('example'); renderManifest()", context);
  elements["manifest-file"].value = "manifest.json";
  deleting = deleteAction("manifest-preview-id").trigger("click"); await settle();
  await elements["delete-job-form"].trigger("submit"); await deleting;
  assert.equal(elements["manifest-preview"].hidden, true);
  assert.equal(elements["manifest-file"].value, "");
  assert.equal(vm.runInContext("manifestPreview", context), null);
  assert.equal(vm.runInContext("installChoices.size", context), 0);

  // Only the task that just finished determines the completion notice severity.
  state.busy = true;
  state.jobs = [{id: "new-export", kind: "export", status: "running", results: [], warnings: []}];
  await vm.runInContext("pollJobs()", context);
  state.busy = false; state.jobs[0].status = "failed";
  state.jobs[0].error = "配置快照失败，请检查文件。";
  await vm.runInContext("pollJobs()", context);
  assert.match(elements.message.textContent, /任务执行未完成：配置快照失败/);
  assert.equal(elements.message.attributes.role, "alert");
  tickMessages(60000); assert.equal(elements.message.hidden, false);
  state.busy = true;
  state.jobs.unshift({id: "retry-export", kind: "export", status: "running", results: [], warnings: []});
  await vm.runInContext("pollJobs()", context);
  state.busy = false; state.jobs[0].status = "completed";
  await vm.runInContext("pollJobs()", context);
  assert.equal(elements.message.attributes.role, "status");
  assert.match(elements.message.textContent, /任务已结束/);

  // Success notices expire; hovering and keyboard focus both pause the clock.
  vm.runInContext("message('保存规则完成')", context);
  assert.equal(elements.message.attributes.role, "status");
  assert.equal(elements.message.attributes["aria-live"], "polite");
  tickMessages(3999); assert.equal(elements.message.hidden, false);
  tickMessages(1); assert.equal(elements.message.hidden, true);
  vm.runInContext("message('已应用规则')", context);
  tickMessages(1000);
  await elements.message.trigger("mouseenter");
  tickMessages(10000); assert.equal(elements.message.hidden, false);
  await elements.message.trigger("focusin");
  await elements.message.trigger("mouseleave");
  tickMessages(10000); assert.equal(elements.message.hidden, false);
  await elements.message.trigger("focusout", {relatedTarget: elements.refresh});
  tickMessages(2999); assert.equal(elements.message.hidden, false);
  tickMessages(1); assert.equal(elements.message.hidden, true);

  // A prior timer cannot dismiss a newer error; errors stay until dismissed.
  vm.runInContext("message('下载已发起')", context);
  const oldTimer = [...messageTimers.values()][0];
  vm.runInContext("message('配置文件无法读取，请刷新后重试。', true)", context);
  oldTimer.handler(); tickMessages(60000);
  assert.equal(elements.message.hidden, false);
  assert.equal(elements.message.attributes.role, "alert");
  assert.equal(elements.message.attributes["aria-live"], "assertive");
  const errorContent = elements.message.children[1];
  vm.runInContext("message('配置文件无法读取，请刷新后重试。', true)", context);
  assert.equal(elements.message.children[1], errorContent, "Polling the same error must not redraw or announce it repeatedly");
  await elements.message.children.at(-1).trigger("click");
  assert.equal(elements.message.hidden, true);
  assert.equal(messageTimers.size, 0);

  // Task navigation is available during work and brings its panel into view.
  vm.runInContext("busy = true; message('导出任务已启动', false, {task: true})", context);
  const taskAction = descendants(elements.message).find(element => element.textContent === "查看任务");
  assert.equal(taskAction.disabled, false);
  await taskAction.trigger("click");
  assert.equal(elements["history-panel"].hidden, false);
  assert.equal(elements["history-panel"].scrolledIntoView, true);
  assert.equal(elements.message.hidden, true);

  // Modal notices live only inside the dialog and are cleared when it closes.
  elements["source-dialog"].showModal();
  vm.runInContext("message('来源关联失败，请重新校验。', true)", context);
  assert.equal(elements.message.hidden, true);
  assert.equal(elements["source-message"].hidden, false);
  tickMessages(60000); assert.equal(elements["source-message"].hidden, false);
  await elements["close-source"].trigger("click");
  assert.equal(elements["source-message"].hidden, true);
  assert.equal(elements.message.hidden, true);
  elements["source-dialog"].showModal();
  vm.runInContext("message('恢复任务已结束', false, {task: true})", context);
  await descendants(elements["source-message"]).find(element => element.textContent === "查看任务").trigger("click");
  assert.equal(elements["source-dialog"].open, false);
  assert.equal(elements["source-message"].hidden, true);
  assert.equal(messageTimers.size, 0);

  // The manager has no standard data component and cannot be restored online.
  vm.runInContext("busy = false; requesting = false; clearManifestPreview()", context);
  elements["export-format"].value = "zip";
  const managerPlugin = {...plugin, directory: "astrbot_plugin_manager", name: "astrbot_plugin_manager", display_name: "插件管家", data_available: false, ranges: [], kv_count: 0};
  elements.search.value = ""; elements["status-filter"].value = "all";
  vm.runInContext("state.plugins.push(" + JSON.stringify(managerPlugin) + "); choices.set('astrbot_plugin_manager', {selected: true, data: false, config: false}); renderPlugins()", context);
  const managerRow = descendants(elements["plugin-list"]).find(element => element.tagName === "tr" && element.textContent.includes("插件管家"));
  assert.match(managerRow.textContent, /不可用/);
  const managerCheckbox = descendants(managerRow).find(element => element.attributes["aria-label"] === "附带 astrbot_plugin_manager 的标准数据");
  assert.equal(managerCheckbox.disabled, true);
  elements["bulk-data"].checked = true; await elements["bulk-data"].trigger("change");
  assert.equal(vm.runInContext("choices.get('astrbot_plugin_manager').data", context), false);
  vm.runInContext(`preview = {job: {id: 'manager-zip', warnings: []}, astrbot_version: '4.27', plugins: [{name: 'astrbot_plugin_manager', directory: 'astrbot_plugin_manager', restore_available: false, exists: true, data: false, config: true, problem: '不能在线恢复插件管家。'}]};
    importChoices = new Map([['astrbot_plugin_manager', {code: false, config: false, data: false}]]); renderImportPreview();`, context);
  assert.deepEqual(descendants(elements["import-list"]).filter(element => element.tagName === "input").map(element => element.disabled), [true, true, true]);
  assert.match(elements["import-list"].textContent, /不能在线恢复插件管家/);
  assert.equal(elements["import-all-code"].disabled, true);
  assert.equal(elements["import-all-config"].disabled, true);
  assert.equal(descendants(elements["import-list"]).find(element => element.tagName === "img").src, "/favicon.svg");
  await elements["select-import-all"].trigger("click");
  assert.equal(vm.runInContext("JSON.stringify(importChoices.get('astrbot_plugin_manager'))", context), JSON.stringify({code: false, config: false, data: false}));
  vm.runInContext(`preview.plugins.push({name: 'blocked_new', directory: 'blocked_new', exists: false, config: true, data: true, problem: '依赖冲突'});
    importChoices.set('blocked_new', {code: false, config: false, data: false}); renderImportPreview();`, context);
  await elements["clear-import-selection"].trigger("click");
  await elements["select-import-all"].trigger("click");
  assert.equal(vm.runInContext("JSON.stringify(importChoices.get('blocked_new'))", context), JSON.stringify({code: false, config: false, data: false}));
  assert.equal(elements["import-all-data"].disabled, true, "Unavailable components cannot be selected in bulk");
  vm.runInContext("preview = null; renderImportPreview()", context);
  vm.runInContext("state.plugins.pop(); choices.delete('astrbot_plugin_manager')", context);

  // An unavailable market cannot silently select a working repository.
  vm.runInContext(`busy = false; requesting = false; installChoices.clear(); installSources.clear();
    manifestPreview = {job: {id: 'sources', warnings: []}, expires_at: 2000000000, plugins: [
      {id:'0',display_name:'双来源插件',market_plugin_id:'Alice/demo',repo_url:'https://github.com/alice/demo',default_source:'market',sources:[
        {kind:'market',status:'blocked',problem:'市场离线'}, {kind:'repository',status:'install',name:'demo',version:'1.0'}]},
      {id:'1',display_name:'仓库插件',market_plugin_id:'',repo_url:'https://github.com/alice/other',default_source:'repository',sources:[
        {kind:'repository',status:'install',name:'other',version:'2.0'}]}
    ]}; renderManifest();`, context);
  let sourceChecks = descendants(elements["manifest-list"]).filter(element => element.tagName === "input");
  assert.deepEqual(sourceChecks.map(element => element.disabled), [true, false]);
  const fallbackSelect = descendants(elements["manifest-list"]).find(element => element.tagName === "select");
  assert.equal(fallbackSelect.value, "market");
  fallbackSelect.value = "repository"; await fallbackSelect.trigger("change");
  assert.equal(vm.runInContext("installChoices.has('0')", context), true);
  sourceChecks = descendants(elements["manifest-list"]).filter(element => element.tagName === "input");
  assert.equal(sourceChecks[0].disabled, false);
  assert.match(elements["manifest-list"].textContent, /https:\/\/github.com\/alice\/demo/);

  // Expired preview records offer a new check rather than an unusable confirmation.
  state.jobs = [{...manifestResponse.job, id: 'expired-preview', preview_expired: true, status: 'preview'}];
  vm.runInContext("clearManifestPreview(); renderJobs(" + JSON.stringify(state.jobs) + ")", context);
  assert.match(elements["job-list"].textContent, /预检已过期/);
  assert.doesNotMatch(elements["job-list"].textContent, /等待确认|继续选择安装/);
  const recheckAction = descendants(elements["job-list"]).find(element => element.tagName === "button" && element.textContent === "重新预检");
  await recheckAction.trigger("click");
  assert.equal(posts.at(-1).endpoint, "manage/manifest-recheck");
  assert.equal(posts.at(-1).body.id, "expired-preview");
  assert.equal(vm.runInContext("pendingManifestId", context), "rechecked-id");
  assert.equal(elements["manifest-progress"].hidden, false);
  Object.assign(state.jobs[0], manifestResponse.job); state.busy = false;
  manifestResponse.draft = [{id:'0',source:'repository',selected:false}, {id:'1',source:'market',selected:false}, {id:'2',source:'market',selected:false}];
  // A page initialization reconnects to the latest owned unfinished preview.
  vm.runInContext("clearManifestPreview()", context);
  await vm.runInContext("initialize()", context);
  assert.equal(elements["import-panel"].hidden, false);
  assert.equal(elements["manifest-preview"].hidden, false);
  assert.equal(vm.runInContext("manifestPreview.job.id", context), "rechecked-id");
  assert.equal(vm.runInContext("installChoices.size", context), 0);
  assert.equal(vm.runInContext("selectedManifestSource(manifestPreview.plugins[0]).kind", context), "repository");
  // Recovery lives beside the retained import backup and requires scope confirmation.
  vm.runInContext("clearManifestPreview(); busy = false; requesting = false", context);
  state.busy = false;
  state.jobs = [{...recoveryResponse.job, id: "zip-in", kind: "import", status: "completed", backup_ready: true}];
  delete state.jobs[0].source_id;
  await vm.runInContext("pollJobs()", context);
  const restoreAction = () => descendants(elements["job-list"]).find(element => element.tagName === "button" && element.textContent === "还原到导入前");
  assert.ok(restoreAction());
  state.busy = true; await vm.runInContext("pollJobs()", context);
  assert.equal(restoreAction().disabled, true);
  state.busy = false; await vm.runInContext("pollJobs()", context);
  await restoreAction().trigger("click");
  assert.equal(elements["recovery-dialog"].open, true);
  assert.match(elements["recovery-list"].textContent, /恢复旧内容/);
  assert.match(elements["recovery-list"].textContent, /移除新增/);
  assert.equal(elements["execute-recovery"].disabled, true);
  const recoveryInputs = descendants(elements["recovery-list"]).filter(element => element.tagName === "input");
  recoveryInputs[1].checked = false; await recoveryInputs[1].trigger("change");
  elements["recovery-acknowledged"].checked = true; await elements["recovery-acknowledged"].trigger("change");
  assert.equal(elements["execute-recovery"].disabled, false);
  failRecovery = true; await elements["execute-recovery"].trigger("click");
  assert.equal(elements["recovery-dialog"].open, true);
  assert.equal(elements["recovery-message"].hidden, false);
  assert.match(elements["recovery-message"].textContent, /重新预检/);
  failRecovery = false; await elements["execute-recovery"].trigger("click");
  assert.equal(elements["recovery-dialog"].open, false);
  const restorePost = posts.filter(item => item.endpoint === "transfer/rollback").at(-1);
  assert.deepEqual(JSON.parse(JSON.stringify(restorePost.body.selections)), [{directory: "demo_dir", code: true, config: false, data: true}]);
  assert.match(elements["job-list"].textContent, /还原到导入前/);
  assert.equal(restoreAction().disabled, true);

  // A pending chip is an accessible action; confirmation never loads on cancel.
  state.busy = false; state.jobs = [];
  const pendingRecord = structuredClone(record);
  pendingRecord.local = {...record.local, name: "pending", directory: "pending_dir", status: "pending", problem: "", reserved: false};
  pendingRecord.revision = "pending-r1"; pendingRecord.identity_changed = false;
  const pendingPlugin = {...plugin, ...pendingRecord.local, plugin_id: "Alice/pending", blocked: "", logo: null};
  state.plugins = [pendingPlugin]; management.plugins = [pendingRecord];
  await refresh();
  const pendingChip = () => descendants(elements["plugin-list"]).find(element => element.tagName === "button" && element.textContent === "待加载");
  assert.ok(pendingChip());
  assert.equal(pendingChip().disabled, false);
  assert.match(pendingChip().attributes["aria-label"], /加载并启用/);
  vm.runInContext("managementStatus = 'loading'; renderPlugins()", context);
  assert.equal(pendingChip().disabled, true);
  vm.runInContext("managementStatus = 'ready'; busy = true; renderPlugins()", context);
  assert.equal(pendingChip().disabled, true);
  vm.runInContext("busy = false; renderPlugins()", context);
  const cancelledLoad = pendingChip().trigger("click"); await settle();
  assert.equal(elements.confirmation.open, true);
  assert.match(elements["confirmation-title"].textContent, /加载插件/);
  assert.match(elements["confirmation-notices"].textContent, /启用.*执行插件代码/);
  assert.equal(pendingChip().disabled, true);
  assert.equal(elements["confirmation-acknowledgement"].hidden, true);
  assert.equal(elements.acknowledged.checked, false);
  assert.equal(elements.acknowledged.required, false);
  assert.equal(elements.acknowledged.disabled, true);
  await elements["confirmation-cancel"].trigger("click"); await cancelledLoad;
  assert.ok(!posts.some(item => item.endpoint === "manage/load-plugin"));
  failLoad = true;
  const rejectedLoad = pendingChip().trigger("click"); await settle();
  await elements["confirmation-form"].trigger("submit"); await rejectedLoad;
  assert.match(elements.message.textContent, /状态已变化/);
  assert.equal(pendingChip().disabled, false);
  assert.equal(state.jobs.length, 0);
  failLoad = false;
  const loading = pendingChip().trigger("click"); await settle();
  await elements["confirmation-form"].trigger("submit"); await settle();
  assert.equal(elements.confirmation.open, false);
  const loadingChip = descendants(elements["plugin-list"]).find(element => element.tagName === "button" && element.textContent === "加载中…");
  assert.ok(loadingChip);
  assert.equal(loadingChip.disabled, true);
  assert.equal(state.jobs.length, 0);
  assert.doesNotMatch(elements["job-list"].textContent, /加载插件/);
  assert.doesNotMatch(elements.message.textContent + elements["source-message"].textContent, /插件加载任务已启动/);
  state.busy = false;
  pendingRecord.local.status = "enabled"; pendingPlugin.status = "enabled";
  pendingLoad.resolve({directory: "pending_dir", status: "enabled", message: "已加载并启用", warnings: []});
  await loading; await settle();
  assert.ok(!pendingChip());
  assert.match(elements["plugin-list"].textContent, /已打开/);
  assert.match(elements.message.textContent, /已加载并启用/);
  assert.equal(state.jobs.length, 0);
  assert.doesNotMatch(elements["job-list"].textContent, /加载插件/);

  // Other confirmations still require the acknowledgement checkbox.
  const otherConfirmation = vm.runInContext("confirmOperation('确认其他操作', [], '操作说明')", context);
  assert.equal(elements["confirmation-acknowledgement"].hidden, false);
  assert.equal(elements.acknowledged.required, true);
  assert.equal(elements.acknowledged.disabled, false);
  assert.equal(elements.acknowledged.checked, false);
  await elements["confirmation-form"].trigger("submit");
  assert.equal(elements.confirmation.open, true);
  await elements["confirmation-cancel"].trigger("click");
  assert.equal(await otherConfirmation, false);

  // A load in another page can make the manager busy without producing a job notice.
  vm.runInContext("dismissMessage(elements.message)", context);
  state.busy = true;
  await vm.runInContext("pollJobs()", context);
  state.busy = false;
  await vm.runInContext("pollJobs()", context);
  assert.equal(elements.message.hidden, true);
  assert.equal(state.jobs.length, 0);
}
main().catch(error => {console.error(error); process.exitCode = 1;});
