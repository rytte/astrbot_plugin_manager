"use strict";

const elements = Object.fromEntries(Array.from(document.querySelectorAll("[id]")).map(element => [element.id, element]));
let bridge;
let state;
let stateRequest = 0;
let choices = new Map();
const pluginIcons = new Map();
let preview = null;
let importChoices = new Map();
let busy = false;
let requesting = false;
let previousBusy = false;
let confirmationResolve = null;
let managementState = {plugins: []};
let managementStatus = "loading", managementRequest = 0;
let selectedRecord = null, bindingPreview = null, manifestPreview = null;
let manifestSelections = new Set(), installChoices = new Set();
const sourceLabels = {market: "插件市场", repository: "代码仓库", upload: "上传安装", bundle: "迁移包导入", unknown: "来源未知", builtin: "AstrBot 内置"};

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function formatSize(bytes) {
  if (!Number.isFinite(bytes)) return "—";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KiB";
  if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " MiB";
  return (bytes / 1024 / 1024 / 1024).toFixed(2) + " GiB";
}

function statisticSize(plugin, field) {
  if (plugin.statistics_status === "pending") return "统计中…";
  if (plugin.statistics_status === "failed") return "统计失败";
  if (plugin.statistics_status === "unavailable") return "—";
  return formatSize(plugin[field]);
}

function message(text, error = false) {
  elements.message.textContent = text;
  elements.message.classList.toggle("error", error);
  elements.message.hidden = !text;
  if (elements["source-dialog"].open) {
    elements["source-message"].textContent = text;
    elements["source-message"].hidden = !text;
    elements["source-message"].classList.toggle("error", error);
  }
}

function selections() {
  return Array.from(choices, ([directory, choice]) => ({directory, config: choice.config, data: choice.data})).filter(selection => choices.get(selection.directory).selected).sort((first, second) => first.directory.localeCompare(second.directory));
}

function savedSelections() {
  return state.rule.selections.map(({directory, config, data}) => ({directory, config, data})).sort((first, second) => first.directory.localeCompare(second.directory));
}

function isDirty() {
  return JSON.stringify(selections()) !== JSON.stringify(savedSelections());
}

function applySavedRule() {
  if (busy || requesting || manifestMode() || !state.rule.selections.length) return;
  const problems = [];
  for (const selection of state.rule.selections) {
    const plugin = state.plugins.find(plugin => plugin.directory === selection.directory);
    const problem = !plugin ? "插件不存在" : plugin.name !== selection.name || plugin.plugin_id !== selection.plugin_id ? "插件身份已变化" : plugin.blocked ? "插件暂不可导出" : selection.config && !plugin.config_available ? "已保存配置不存在" : selection.data && !plugin.data_available ? "标准数据不可用" : "";
    if (problem) problems.push(selection.directory + "（" + problem + "）");
  }
  if (problems.length) {message("规则未应用：" + problems.join("、") + "。请重新选择插件并保存规则。", true); return;}
  choices = new Map(state.plugins.map(plugin => [plugin.directory, {selected: false, config: false, data: false}]));
  for (const selection of state.rule.selections) choices.set(selection.directory, {selected: true, config: selection.config, data: selection.data});
  renderPlugins(); renderSummary();
  message("已应用保存的规则，可继续调整本次导出的内容。");
}

function manifestMode() {return elements["export-format"].value === "manifest";}
function sourceRecord(directory) {return managementState.plugins.find(record => record.local.directory === directory);}
function active(record) {return record.bindings.find(binding => binding.id === record.active_binding_id);}
function official(record) {return record && record.bindings.some(binding => binding.kind === "market" && binding.registry_url === null);}
function eligible(record) {return record && !record.local.reserved && !record.local.problem && !record.identity_changed && official(record);}
function sourceText(binding) {return binding ? (binding.kind === "market" ? (binding.registry_url ? "自定义市场 / " : "官方市场 / ") + binding.market_plugin_id : binding.repo_url) : "未绑定更新源";}
function sourceKind(binding) {return !binding ? "未绑定" : binding.kind === "market" ? (binding.registry_url ? "自定义市场" : "官方市场") : "Git 仓库";}
function sourceTime(value) {
  if (!value) return "未知";
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value.replace(/-/g, "/");
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", {year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false});
}
function sourceAddress(className, value) {
  const element = node("p", className), parts = String(value || "").split("/");
  parts.forEach((part, index) => {
    element.append(document.createTextNode(part + (index < parts.length - 1 ? "/" : "")));
    if (index < parts.length - 1) element.append(node("wbr"));
  });
  return element;
}
function button(text, action, disabled = false) {
  const element = node("button", "button secondary source-action", text);
  element.disabled = requesting || busy || disabled;
  element.addEventListener("click", () => runRequest(action));
  return element;
}
function renderFormats() {
  const isManifest = manifestMode();
  document.documentElement.dataset.exportFormat = isManifest ? "manifest" : "zip";
  elements["zip-summary"].hidden = isManifest;
  elements["manifest-summary"].hidden = !isManifest;
  elements["export-hint"].textContent = isManifest ? "接收者从官方市场安装当前版本；现有插件跳过。" : "按当前选择打包，读取导出时的实际内容。";
  const importingManifest = elements["import-format"].value === "manifest";
  elements["bundle-import"].hidden = importingManifest;
  elements["manifest-import"].hidden = !importingManifest;
}
function visiblePlugins() {
  const term = elements.search.value.trim().toLowerCase();
  const filter = elements["status-filter"].value;
  return state.plugins.filter(plugin => (!term || [plugin.name, plugin.display_name, plugin.directory].some(value => (value || "").toLowerCase().includes(term))) && (filter === "all" || plugin.status === filter));
}

function checkbox(label, checked, disabled, changed) {
  const wrapper = node("label", "check-label");
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = checked;
  input.disabled = disabled || busy || requesting;
  input.setAttribute("aria-label", label);
  input.addEventListener("change", () => changed(input.checked));
  wrapper.append(input);
  return wrapper;
}

function titleCell(plugin, showIcon = false) {
  const cell = node("td");
  const details = node("div", "plugin-copy");
  const heading = node("div", "plugin-heading");
  heading.append(node("span", "plugin-title", plugin.display_name || plugin.name));
  const statuses = {enabled: "已打开", disabled: "已关闭", failed: "加载失败", pending: "待加载"};
  if (statuses[plugin.status]) {
    const status = node("span", "plugin-status " + plugin.status);
    const icon = node("span", "plugin-status-icon");
    icon.setAttribute("aria-hidden", "true");
    status.append(icon, document.createTextNode(statuses[plugin.status]));
    if (plugin.status === "pending") status.title = "需要重启 AstrBot 扫描插件";
    heading.append(status);
  }
  const metadata = node("div", "plugin-meta");
  metadata.append(node("span", "plugin-id", plugin.name + (plugin.directory !== plugin.name ? " / " + plugin.directory : "")));
  if (plugin.version) metadata.append(node("span", "plugin-version", "v" + plugin.version.replace(/^v/i, "")));
  details.append(heading, metadata);
  if (showIcon) {
    const url = plugin.logo || "/favicon.svg";
    let cached = pluginIcons.get(plugin.directory);
    // Keep image nodes across redraws: AstrBot's file tokens are single use.
    if (!cached || cached.url !== url) {
      const icon = node("span", "plugin-icon");
      icon.setAttribute("aria-hidden", "true");
      icon.append(node("span", "plugin-icon-placeholder"));
      const image = node("img");
      image.alt = ""; image.width = 40; image.height = 40;
      image.loading = "lazy"; image.decoding = "async"; image.referrerPolicy = "no-referrer";
      let fallback = !plugin.logo;
      image.addEventListener("error", () => {
        if (fallback) image.hidden = true;
        else {fallback = true; image.src = "/favicon.svg";}
      });
      image.src = url;
      icon.append(image);
      cached = {url, icon}; pluginIcons.set(plugin.directory, cached);
    }
    const summary = node("div", "plugin-summary");
    summary.append(cached.icon, details); cell.append(summary);
  } else cell.append(details);
  return cell;
}

function renderPlugins() {
  const body = elements["plugin-list"];
  body.replaceChildren();
  const plugins = visiblePlugins();
  elements["plugin-count"].textContent = state.plugins.length + " 个插件";
  const selectable = plugins.filter(plugin => (manifestMode() ? eligible(sourceRecord(plugin.directory)) : !plugin.blocked) || (manifestMode() ? manifestSelections.has(plugin.directory) : choices.get(plugin.directory).selected));
  const selectedVisible = selectable.filter(plugin => manifestMode() ? manifestSelections.has(plugin.directory) : choices.get(plugin.directory).selected).length;
  elements["select-all"].checked = selectable.length > 0 && selectedVisible === selectable.length;
  elements["select-all"].indeterminate = selectedVisible > 0 && selectedVisible < selectable.length;
  elements["select-all"].disabled = busy || requesting || !selectable.length;
  for (const plugin of plugins) {
    const record = sourceRecord(plugin.directory);
    const blocked = manifestMode() ? !eligible(record) : Boolean(plugin.blocked);
    const choice = manifestMode() ? {selected: manifestSelections.has(plugin.directory), data: false, config: false} : choices.get(plugin.directory);
    const row = node("tr", choice.selected ? "selected" : "");
    const selectCell = node("td", "check-cell");
    selectCell.append(checkbox("选择插件 " + plugin.name, choice.selected, blocked && !choice.selected, checked => {if (manifestMode()) {checked ? manifestSelections.add(plugin.directory) : manifestSelections.delete(plugin.directory);} else choice.selected = checked; renderPlugins(); renderSummary();}));
    const title = titleCell(plugin, true);
    if (manifestMode()) title.append(node("div", eligible(record) ? "plugin-detail" : "plugin-error", eligible(record) ? "可分享 · 已关联官方市场" : "暂不能分享 · 请在来源管理中校验并关联官方市场"));
    else if (plugin.blocked) title.append(node("div", "plugin-error", plugin.blocked));
    const dataCell = node("td"); dataCell.dataset.zipOnly = "";
    const dataCheck = checkbox("附带 " + plugin.name + " 的标准数据", choice.data, !choice.selected || !plugin.data_available || Boolean(plugin.blocked), checked => {choice.data = checked; renderSummary();});
    dataCheck.append(document.createTextNode(plugin.data_available ? "标准数据" : "不可用"));
    dataCell.append(dataCheck, node("div", "range-label", plugin.data_available ? "文件 " + statisticSize(plugin, "data_size") : "归属未知或自身数据"));
    dataCell.title = (plugin.ranges || []).join("\n");
    if (plugin.data_available) dataCell.title += (dataCell.title ? "\n" : "") + "键值存储：" + (Number.isFinite(plugin.kv_count) ? plugin.kv_count + " 条记录（随标准数据备份）" : plugin.statistics_status === "pending" ? "统计中…" : "未完成统计，请刷新状态重试。");
    const configCell = node("td"); configCell.dataset.zipOnly = "";
    const configCheck = checkbox("附带 " + plugin.name + " 的当前配置", choice.config, !choice.selected || !plugin.config_available || Boolean(plugin.blocked), checked => {choice.config = checked; renderSummary();});
    configCheck.append(document.createTextNode(plugin.config_available ? "当前配置" : "无已保存配置"));
    configCell.append(configCheck);
    const sizeCell = node("td", "numeric", statisticSize(plugin, "code_size")); sizeCell.dataset.zipOnly = "";
    sizeCell.title = plugin.statistics_error || plugin.blocked || "";
    const sourceCell = node("td", "plugin-source");
    const binding = record && active(record);
    const sourceLabel = node("span", "source-label" + (binding ? "" : " unbound"), binding ? (binding.kind === "market" ? (binding.registry_url ? "自定义市场" : "官方市场") : "Git 仓库") : record ? "未绑定更新源" : managementStatus === "failed" ? "来源读取失败" : managementStatus === "loading" ? "来源读取中…" : "暂无来源记录");
    sourceLabel.title = record ? sourceText(binding) + "\n安装记录：" + (sourceLabels[record.provenance.method] || record.provenance.method) : managementStatus === "failed" ? "来源读取失败，请刷新状态重试。" : managementStatus === "loading" ? "正在读取来源记录。" : "该插件暂无来源记录。";
    if (record && managementStatus === "failed") sourceLabel.title += "\n来源刷新失败，当前显示上次读取的记录。";
    const manageButton = button("来源管理", () => openSource(plugin.directory), !record);
    manageButton.className = "text-button source-manage";
    manageButton.setAttribute("aria-label", "管理 " + plugin.name + " 的插件来源");
    sourceCell.append(sourceLabel, manageButton);
    row.append(selectCell, title, dataCell, configCell, sizeCell, sourceCell);
    body.append(row);
  }
  if (!plugins.length) {
    const row = node("tr");
    const cell = node("td", "empty", "没有匹配的插件。");
    cell.colSpan = manifestMode() ? 3 : 6;
    row.append(cell);
    body.append(row);
  }
}

function renderSummary() {
  renderFormats();
  const selected = manifestMode() ? Array.from(manifestSelections, directory => ({directory, config: false, data: false})) : selections();
  const selectedPlugins = state.plugins.filter(plugin => choices.get(plugin.directory)?.selected);
  const dirty = isDirty();
  elements["selected-count"].textContent = selected.length;
  elements["selection-label"].textContent = "已选 " + selected.length + " 个插件";
  elements["clear-selection"].disabled = busy || requesting || !selected.length;
  elements["data-count"].textContent = selected.filter(selection => selection.data).length + " 个";
  elements["config-count"].textContent = selected.filter(selection => selection.config).length + " 个";
  const sizes = selectedPlugins.flatMap(plugin => [plugin.code_size, ...(choices.get(plugin.directory).data ? [plugin.data_size] : []), ...(choices.get(plugin.directory).config ? [plugin.config_size] : [])]);
  elements["selected-size"].textContent = selectedPlugins.some(plugin => plugin.statistics_status === "pending") ? "统计中…" : sizes.every(Number.isFinite) && !selectedPlugins.some(plugin => ["failed", "unavailable"].includes(plugin.statistics_status)) ? formatSize(sizes.reduce((total, size) => total + size, 0)) : "暂不可用";
  elements["rule-status"].textContent = state.rule.selections.length ? "已保存 " + state.rule.selections.length + " 个插件的规则" : "可保存常用选择，下次一键应用";
  elements["rule-status"].title = state.rule.updated_at ? "保存于 " + new Date(state.rule.updated_at * 1000).toLocaleString("zh-CN") : "";
  elements["apply-rule"].disabled = busy || requesting || manifestMode() || !state.rule.selections.length;
  elements["save-rule"].disabled = busy || requesting || manifestMode() || !dirty;
  elements.export.textContent = manifestMode() ? "导出清单 JSON" : "导出插件包 ↗";
  elements.export.disabled = busy || requesting || (manifestMode() ? !manifestSelections.size || Array.from(manifestSelections).some(directory => !eligible(sourceRecord(directory))) : !selected.length || selectedPlugins.some(plugin => plugin.blocked));
  elements["export-format"].disabled = requesting;
  elements["import-format"].disabled = requesting;
  elements["manifest-file"].disabled = busy || requesting || !managementState.csrf;
  elements["install-manifest"].disabled = busy || requesting || !manifestPreview || !installChoices.size;
  elements["bundle-file"].disabled = busy || requesting;
  elements["execute-import"].disabled = busy || requesting || !preview || !Array.from(importChoices.values()).some(choice => choice.code || choice.config || choice.data);
  for (const kind of ["data", "config"]) {
    const available = selectedPlugins.filter(plugin => plugin[kind + "_available"] && !plugin.blocked);
    const included = available.filter(plugin => choices.get(plugin.directory)[kind]).length;
    const bulk = elements["bulk-" + kind];
    bulk.checked = available.length > 0 && included === available.length;
    bulk.indeterminate = included > 0 && included < available.length;
    bulk.disabled = busy || requesting || manifestMode() || !available.length;
  }
}

function showTab(identifier) {
  document.documentElement.dataset.pageTab = identifier;
  elements["export-sidebar"].hidden = identifier !== "export-panel";
  for (const button of document.querySelectorAll("[data-tab]")) {
    const selected = button.dataset.tab === identifier;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-pressed", String(selected));
    elements[button.dataset.tab].hidden = !selected;
  }
}

function confirmOperation(title, plugins, notices) {
  elements["confirmation-title"].textContent = title;
  elements["confirmation-list"].replaceChildren();
  for (const plugin of plugins) {
    const row = node("div", "confirmation-row");
    row.append(node("strong", "", plugin.display_name || plugin.name), node("p", "", plugin.directory + " · " + plugin.description));
    elements["confirmation-list"].append(row);
  }
  elements["confirmation-notices"].textContent = notices;
  elements.acknowledged.checked = false;
  elements.confirmation.showModal();
  return new Promise(resolve => { confirmationResolve = resolve; });
}

function finishConfirmation(result) {
  elements.confirmation.close();
  if (confirmationResolve) confirmationResolve(result);
  confirmationResolve = null;
}

async function post(action, body = {}) {
  return bridge.apiPost("transfer/" + action, {csrf: state.csrf, ...body});
}

async function loadState(preserve = false) {
  const current = ++stateRequest;
  const updated = await bridge.apiGet("transfer/state");
  if (current !== stateRequest) return;
  const previousState = state;
  state = updated;
  managementStatus = "loading";
  busy = state.busy;
  const retained = new Map();
  for (const plugin of state.plugins) {
    const previous = previousState?.plugins.find(entry => entry.directory === plugin.directory);
    const unchanged = previous && previous.name === plugin.name && previous.plugin_id === plugin.plugin_id;
    retained.set(plugin.directory, preserve && unchanged && choices.has(plugin.directory) ? choices.get(plugin.directory) : {selected: false, config: false, data: false});
  }
  if (preserve && Array.from(choices).some(([directory, choice]) => choice.selected && !retained.get(directory)?.selected)) message("部分已选插件已不存在或身份变化，已取消选择。请核对本次导出。", true);
  choices = retained;
  elements["upload-limit"].textContent = "最大 " + formatSize(state.limits.archive_bytes) + " · 不接受普通插件安装 ZIP";
  renderPlugins();
  renderSummary();
  renderJobs(state.jobs);
  if (selectedRecord) renderSource();
  // Neither storage scans nor source reads hold up the basic plugin list.
  void loadStatistics(state);
  void loadManagement(state).catch(() => {});
}

async function loadStatistics(snapshot) {
  try {
    const response = await bridge.apiGet("transfer/statistics");
    if (state !== snapshot) return;
    const entries = new Map(response.plugins.map(plugin => [plugin.directory, plugin]));
    for (const plugin of snapshot.plugins) {
      if (plugin.statistics_status !== "pending") continue;
      const entry = entries.get(plugin.directory);
      if (!entry || ["name", "plugin_id", "version"].some(field => plugin[field] !== entry[field])) {
        plugin.statistics_status = "unavailable";
        plugin.blocked = "插件信息已变化，请刷新状态。";
        continue;
      }
      for (const field of ["code_size", "config_size", "data_size", "kv_count", "statistics_status", "blocked", "ranges", "data_available", "config_available"]) plugin[field] = entry[field];
    }
  } catch (error) {
    if (state !== snapshot) return;
    for (const plugin of snapshot.plugins) if (plugin.statistics_status === "pending") {
      plugin.statistics_status = "failed";
      plugin.statistics_error = "大小与存储统计失败，请刷新状态重试。";
    }
  }
  renderPlugins();
  renderSummary();
}

async function runRequest(operation) {
  if (requesting) return;
  requesting = true;
  renderPlugins();
  renderSummary();
  if (selectedRecord) {elements["source-message"].hidden = true; renderSource();}
  try { await operation(); }
  catch (error) { message(error.message || "操作失败，请检查服务端日志。", true); }
  finally { requesting = false; renderPlugins(); renderSummary(); if (preview) renderImportPreview(); if (manifestPreview) renderManifest(); if (selectedRecord) renderSource(); }
}

function renderImportPreview() {
  elements["import-preview"].hidden = false;
  elements["preview-source"].textContent = "来源 AstrBot " + preview.astrbot_version + " · 预检一小时内有效";
  const body = elements["import-list"];
  body.replaceChildren();
  for (const plugin of preview.plugins) {
    const choice = importChoices.get(plugin.directory);
    const row = node("tr");
    row.append(titleCell({...plugin, status: ""}));
    for (const kind of ["code", "config", "data"]) {
      const cell = node("td");
      const disabled = kind === "code" ? Boolean(plugin.problem) : !plugin[kind];
      cell.append(checkbox("恢复 " + plugin.name + " 的" + {code: "代码", config: "配置", data: "标准数据"}[kind], choice[kind], disabled, checked => {choice[kind] = checked; renderSummary();}));
      row.append(cell);
    }
    const details = node("td");
    details.append(node("div", "plugin-detail", plugin.exists ? "本机已有 · 默认跳过" : "新插件 · 必须包含代码"));
    if (plugin.problem) details.append(node("div", "plugin-error", plugin.problem));
    row.append(details);
    body.append(row);
  }
  elements["import-warnings"].hidden = !preview.job.warnings.length;
  elements["import-warnings"].textContent = preview.job.warnings.join("\n");
}

function renderJobs(jobs) {
  elements["job-count"].textContent = jobs.length;
  const list = elements["job-list"];
  list.replaceChildren();
  const labels = {preview: "等待确认", queued: "排队", running: "进行中", completed: "已完成", completed_with_warning: "完成 · 需检查", failed: "失败", interrupted: "中断 · 需检查", rollback_failed: "自动恢复失败"};
  for (const job of jobs) {
    const card = node("article", "job-card");
    const heading = node("div", "job-heading");
    heading.append(node("span", "job-title", ({export: "导出插件包", import: "导入插件包", manifest_export: "导出插件清单", manifest_install: "清单安装"}[job.kind] || job.kind)), node("span", "pill", labels[job.status] || job.status));
    card.append(heading, node("div", "job-meta", new Date(job.created_at * 1000).toLocaleString("zh-CN") + " · " + job.id.slice(0, 8)), node("div", "job-phase", job.phase));
    if (job.results.length) {
      const results = node("ul", "job-results");
      for (const result of job.results) results.append(node("li", "", result.directory + "：" + (result.message || "快照完成")));
      card.append(results);
    }
    for (const warning of job.warnings) card.append(node("p", "job-error", warning));
    if (job.error) card.append(node("p", "job-error", job.error));
    const actions = node("div", "job-actions");
    for (const kind of ["package", "rollback"]) {
      const available = kind === "package" ? job.kind === "export" && ["completed", "completed_with_warning"].includes(job.status) : job.backup_ready && !["queued", "running"].includes(job.status);
      if (!available) continue;
      const button = node("button", "button secondary", job.expired ? "文件已过保留期" : kind === "package" ? "下载插件包 ↓" : "下载恢复资料 ↓");
      button.disabled = job.expired;
      button.addEventListener("click", async () => {
        button.disabled = true;
        try { await bridge.download("transfer/download", {id: job.id, kind}, "astrbot-" + kind + "-" + job.id.slice(0, 8) + ".zip"); }
        catch (error) { message(error.message || "下载失败。", true); }
        finally { button.disabled = false; }
      });
      actions.append(button);
    }
    if (job.kind === "manifest_export" && job.status === "completed") {
      const download = button(job.expired ? "文件已过保留期" : "下载清单 JSON ↓", () => bridge.download("manage/manifest-download", {id: job.id}, "plugins.astrbot-plugin-manifest.json"), job.expired);
      actions.append(download);
    }
    if (actions.childNodes.length) card.append(actions);
    list.append(card);
  }
  if (!jobs.length) list.append(node("p", "empty", "暂无任务。导出插件包或清单，或上传文件开始迁移。"));
}

function bindEvents() {
  bindManagementEvents();
  elements["export-format"].addEventListener("change", () => {renderPlugins(); renderSummary();});
  elements["import-format"].addEventListener("change", renderFormats);
  for (const button of document.querySelectorAll("[data-tab]")) button.addEventListener("click", () => showTab(button.dataset.tab));
  elements.search.addEventListener("input", renderPlugins);
  elements["status-filter"].addEventListener("change", renderPlugins);
  elements["select-all"].addEventListener("change", event => {
    if (busy || requesting) return;
    for (const plugin of visiblePlugins()) {
      if (manifestMode()) {if (!event.target.checked) manifestSelections.delete(plugin.directory); else if (eligible(sourceRecord(plugin.directory))) manifestSelections.add(plugin.directory);}
      else if (!event.target.checked || !plugin.blocked) choices.get(plugin.directory).selected = event.target.checked;
    }
    renderPlugins(); renderSummary();
  });
  elements["clear-selection"].addEventListener("click", () => {if (busy || requesting) return; if (manifestMode()) manifestSelections.clear(); else for (const choice of choices.values()) choice.selected = false; renderPlugins(); renderSummary();});
  for (const kind of ["data", "config"]) elements["bulk-" + kind].addEventListener("change", event => {if (busy || requesting || manifestMode()) return; for (const plugin of state.plugins) if (choices.get(plugin.directory).selected && plugin[kind + "_available"] && !plugin.blocked) choices.get(plugin.directory)[kind] = event.target.checked; renderPlugins(); renderSummary();});
  elements.refresh.addEventListener("click", () => runRequest(() => loadState(true)));
  elements["apply-rule"].addEventListener("click", applySavedRule);
  elements["save-rule"].addEventListener("click", () => runRequest(async () => {state.rule = await post("rule", {selections: selections()}); message(state.rule.selections.length ? "当前选择已保存为规则，下次可点击「应用已保存规则」恢复。" : "已清空保存的导出规则。");}));
  elements.export.addEventListener("click", () => runRequest(async () => {
    if (manifestMode()) {await exportManifest(); return;}
    const checked = await post("export-preview", {selections: selections()});
    const accepted = await confirmOperation("检查本次导出", checked.plugins.map(plugin => ({...plugin, description: "代码" + (plugin.config ? " + 当前配置" : "") + (plugin.data ? " + 标准数据 / KV" : "") + (plugin.pause_required ? " · 将短暂暂停" : "")})), "配置和数据按原样备份，不会自动脱敏。附带数据时会暂停相关插件并尝试恢复原运行状态；外部写入可能导致快照失败。");
    if (!accepted) return;
    await post("export", {selections: checked.selections, acknowledged: true});
    busy = true;
    showTab("history-panel");
    message("导出任务已启动。任务在服务端继续运行，可以稍后下载。");
    await pollJobs();
  }));
  elements["bundle-file"].addEventListener("change", () => runRequest(async () => {
    const file = elements["bundle-file"].files[0];
    if (!file) return;
    preview = null;
    elements["import-preview"].hidden = true;
    if (file.size > state.limits.archive_bytes) throw new Error("文件超过上传限制，请减小插件包。");
    elements["file-label"].textContent = "正在上传并校验：" + file.name;
    preview = await bridge.upload("transfer/upload/" + encodeURIComponent(state.csrf), file);
    importChoices = new Map(preview.plugins.map(plugin => [plugin.directory, {code: !plugin.exists && !plugin.problem, config: false, data: false}]));
    elements["file-label"].textContent = "预检完成：" + file.name + " · " + formatSize(file.size);
    renderImportPreview();
    message("插件包已通过结构校验。请逐项选择恢复内容，现有插件默认跳过。");
  }));
  elements["execute-import"].addEventListener("click", () => runRequest(async () => {
    const selected = Array.from(importChoices, ([directory, choice]) => ({directory, ...choice})).filter(choice => choice.code || choice.config || choice.data);
    const accepted = await confirmOperation("确认恢复所选内容", selected.map(choice => {const plugin = preview.plugins.find(entry => entry.directory === choice.directory); return {...plugin, description: [choice.code && "替换代码", choice.config && "整体替换配置", choice.data && "整体替换标准数据 / KV"].filter(Boolean).join(" + ")};}), "恢复前强制备份当前受影响内容。空数据也会清空目标数据，不做合并。一个插件失败会停止后续恢复。导入后保持停用，需要重启 AstrBot 后手动启用。");
    if (!accepted) return;
    await post("import", {id: preview.job.id, selections: selected, acknowledged: true});
    preview = null;
    elements["import-preview"].hidden = true;
    busy = true;
    showTab("history-panel");
    message("恢复任务已启动。请勿在任务运行期间手工修改插件文件。");
    await pollJobs();
  }));
  elements["confirmation-form"].addEventListener("submit", event => {event.preventDefault(); if (elements.acknowledged.checked) finishConfirmation(true);});
  elements["confirmation-cancel"].addEventListener("click", () => finishConfirmation(false));
  elements.confirmation.addEventListener("cancel", event => {event.preventDefault(); finishConfirmation(false);});
}

async function pollJobs() {
  const response = await bridge.apiGet("transfer/jobs");
  busy = response.busy;
  renderJobs(response.jobs);
  renderSummary();
  if (previousBusy && !busy) {
    await loadState(true);
    message("任务已结束，请查看任务记录、下载文件或按提示处理警告。");
  }
  if (busy !== previousBusy) renderPlugins();
  if (selectedRecord) await loadManagement();
  previousBusy = busy;
}

async function initialize() {
  try {
    bridge = window.AstrBotPluginPage;
    if (!bridge || typeof bridge.upload !== "function" || typeof bridge.download !== "function") throw new Error("当前环境缺少原生插件页面桥接接口，请从 AstrBot WebUI 的插件页面打开。");
    await bridge.ready();
    bridge.onContext(context => {document.documentElement.dataset.theme = context.isDark ? "dark" : "light";});
    await loadState();
    bindEvents();
    showTab("export-panel");
    window.setInterval(() => {if (!requesting && !document.hidden) pollJobs().catch(error => message(error.message || "任务状态读取失败，请刷新页面。", true));}, 2000);
  } catch (error) {
    elements["boot-error"].hidden = false;
    elements["boot-error"].textContent = error.message || "插件页面初始化失败。";
  }
}


function updateManagement(updated) {
  managementState = updated;
  manifestSelections = new Set(Array.from(manifestSelections).filter(directory => eligible(sourceRecord(directory))));
  if (!selectedRecord) return;
  const refreshed = sourceRecord(selectedRecord.local.directory);
  if (!refreshed) {elements["source-dialog"].close(); selectedRecord = null; bindingPreview = null;}
  else {if (refreshed.revision !== selectedRecord.revision) bindingPreview = null; selectedRecord = refreshed;}
}
async function loadManagement(snapshot = state) {
  const current = ++managementRequest;
  managementStatus = "loading";
  try {
    const updated = await bridge.apiGet("manage/state");
    if (state !== snapshot || current !== managementRequest) return;
    updateManagement(updated);
    managementStatus = "ready";
    renderPlugins(); renderSummary();
    if (selectedRecord) renderSource();
  } catch (error) {
    if (state === snapshot && current === managementRequest) {
      managementStatus = "failed";
      renderPlugins(); renderSummary();
    }
    throw error;
  }
}
async function managementPost(action, body) {return bridge.apiPost("manage/" + action, {csrf: managementState.csrf, ...body});}
async function openSource(directory) {
  await loadManagement();
  selectedRecord = managementState.plugins.find(record => record.local.directory === directory); bindingPreview = null;
  if (!selectedRecord) throw new Error("该插件暂无可管理的来源记录，请刷新列表。");
  elements["source-message"].hidden = true;
  elements["source-install-details"].open = false; elements["source-history-details"].open = false;
  elements["binding-value"].value = ""; elements["activate-binding"].checked = false;
  renderSource(); elements["source-dialog"].showModal();
}
function renderSource() {
  if (!selectedRecord) return;
  const record = selectedRecord, local = record.local, current = active(record);
  elements["source-title"].textContent = local.display_name || local.name || local.directory;
  elements["source-identity"].textContent = [local.name || local.directory, local.version && "v" + local.version.replace(/^v/i, ""), local.directory !== local.name && "目录：" + local.directory].filter(Boolean).join(" · ");
  elements["source-current-kind"].textContent = sourceKind(current);
  elements["source-current-kind"].className = "source-type" + (current ? "" : " unbound");
  const summary = elements["source-summary"]; summary.replaceChildren();
  summary.append(sourceAddress("source-current-value", current ? (current.kind === "market" ? current.market_plugin_id : current.repo_url) : "未绑定更新源"), node("p", "source-current-note", current ? "AstrBot 更新时使用此来源。" : "可在下方关联来源，并将其设为更新源。"));
  if (local.problem || record.identity_changed) summary.append(node("p", "plugin-error", local.problem || "目录身份已变化，旧来源记录已隔离。"));
  elements["source-install-method"].textContent = sourceLabels[record.provenance.method] || record.provenance.method;
  const provenance = elements["source-provenance"]; provenance.replaceChildren();
  const facts = [
    ["安装记录", sourceLabels[record.provenance.method] || record.provenance.method],
    ["记录依据", record.provenance.evidence === "manager_operation" ? "管家实际执行安装" : record.provenance.evidence === "core_record_observed" ? "首次观察到 AstrBot 绑定记录；不能据此还原最初安装历史" : "缺少安装证据"],
    ["安装时间", sourceTime(record.provenance.installed_at)],
    ["首次观察", sourceTime(record.provenance.observed_at)]
  ];
  if (record.provenance.reported_installed_at) facts.push(["AstrBot 记录时间", sourceTime(record.provenance.reported_installed_at) + "（仅供参考）"]);
  for (const [label, value] of facts) {const row = node("div"); row.append(node("dt", "", label), node("dd", "", value)); provenance.append(row);}
  elements["binding-count"].textContent = record.bindings.length;
  const list = elements["binding-list"]; list.replaceChildren();
  for (const binding of record.bindings) {
    const isCurrent = binding.id === record.active_binding_id;
    const card = node("article", "binding-card" + (isCurrent ? " current" : ""));
    const content = node("div", "binding-content"), heading = node("div", "binding-heading");
    heading.append(node("strong", "binding-type", sourceKind(binding)));
    if (isCurrent) heading.append(node("span", "badge", "当前更新源"));
    content.append(heading, sourceAddress("binding-address", binding.kind === "market" ? binding.market_plugin_id : binding.repo_url));
    if (binding.kind === "market" && binding.repo_url) content.append(sourceAddress("binding-repo", binding.repo_url));
    const verified = binding.verification === "identity_matched";
    content.append(node("p", "detail", verified ? "名称与作者已匹配" + (binding.checked_at ? " · " + sourceTime(binding.checked_at) : "") : "AstrBot 来源记录 · 尚未重新校验"));
    card.append(content);
    if (!isCurrent && (binding.kind === "repository" || binding.registry_url === null)) card.append(button("设为更新源", async () => {
      if (!await confirmOperation("切换更新源", [{name: local.name, directory: local.directory, description: sourceText(binding)}], "将重新校验该来源并同步到 AstrBot，后续原生 WebUI 更新使用此来源。最初安装记录保留。")) return;
      const result = await managementPost("source-select", {directory: local.directory, binding_id: binding.id, revision: record.revision, acknowledged: true});
      if (selectedRecord && selectedRecord.local.directory === local.directory) selectedRecord = result;
      bindingPreview = null; await loadManagement(); message("当前更新源已切换并同步到 AstrBot。");
    }));
    list.append(card);
  }
  if (!record.bindings.length) list.append(node("p", "source-empty", "尚未关联来源，可在下方添加。"));
  const blocked = local.reserved || local.problem || record.identity_changed;
  for (const id of ["binding-value", "binding-kind", "activate-binding"]) elements[id].disabled = requesting || busy || Boolean(blocked);
  elements["preview-binding"].disabled = requesting || busy || Boolean(blocked);
  elements["save-binding"].disabled = requesting || busy || Boolean(blocked) || !bindingPreview;
  const repository = elements["binding-kind"].value === "repository";
  elements["binding-value-label"].textContent = repository ? "仓库地址" : "市场 ID";
  elements["binding-value"].placeholder = repository ? "https://github.com/作者/仓库名" : "作者/astrbot_plugin_example";
  elements["binding-preview"].hidden = !bindingPreview;
  elements["binding-preview"].replaceChildren();
  if (bindingPreview) {
    const checked = bindingPreview.binding;
    elements["binding-preview"].append(node("strong", "", "作者与插件名一致"), sourceAddress("binding-address", sourceText(checked)));
    if (checked.kind === "market" && checked.repo_url) elements["binding-preview"].append(sourceAddress("binding-repo", checked.repo_url));
    elements["binding-preview"].append(node("p", "detail", "仅表示元数据匹配，不代表代码安全。"));
    if (bindingPreview.notice) elements["binding-preview"].append(node("p", "detail", bindingPreview.notice));
  }
  const history = elements["source-history"]; history.replaceChildren();
  elements["source-history-count"].textContent = record.history.length;
  const labels = {installed: "管家安装", uninstalled: "管家卸载", binding_added: "保存来源关联", active_changed: "切换当前更新源", core_observed: "观察到 AstrBot 来源变更", bundle_imported: "迁移包覆盖代码，原更新源已清除"};
  for (const event of record.history.slice().reverse()) {const row = node("div", "history-row"); row.append(node("span", "history-event", labels[event.action] || event.action), node("span", "history-meta", sourceTime(event.at) + " · " + event.actor)); history.append(row);}
  if (!record.history.length) history.append(node("p", "source-empty", "暂无管家操作记录。"));
}

async function exportManifest() {
  const job = await managementPost("manifest-export", {directories: Array.from(manifestSelections)});
  await pollJobs();
  await bridge.download("manage/manifest-download", {id: job.id}, "plugins.astrbot-plugin-manifest.json");
  message("插件清单已导出，可分享给对方从官方市场安装。清单不包含配置和数据。");
}
function renderManifest() {
  elements["manifest-preview"].hidden = !manifestPreview;
  if (!manifestPreview) return;
  elements["preview-expiry"].textContent = "有效至 " + new Date(manifestPreview.expires_at * 1000).toLocaleString("zh-CN");
  const body = elements["manifest-list"]; body.replaceChildren();
  for (const plugin of manifestPreview.plugins) {
    const row = node("tr"), choice = node("td"), status = node("td");
    choice.append(checkbox("安装 " + plugin.name, installChoices.has(plugin.market_plugin_id), plugin.status !== "install", checked => {checked ? installChoices.add(plugin.market_plugin_id) : installChoices.delete(plugin.market_plugin_id); renderSummary();}));
    const title = titleCell({...plugin, version: "", status: ""});
    title.append(node("div", "plugin-id", plugin.market_plugin_id));
    status.append(node("div", plugin.status === "blocked" ? "plugin-error" : "plugin-detail", {install: "可安装 · 保持停用", skip: "本机已有 · 跳过", blocked: "无法安装"}[plugin.status]), node("div", "plugin-detail", plugin.problem || "下载后再次核对包内身份与依赖"));
    row.append(choice, title, node("td", "", (plugin.observed_version || "未声明") + " / " + (plugin.version || "未知")), status); body.append(row);
  }
  const warnings = manifestPreview.job.warnings || [];
  elements["manifest-warnings"].hidden = !warnings.length;
  elements["manifest-warnings"].textContent = warnings.join("\n");
}
function bindManagementEvents() {
  const closeSource = () => {elements["source-dialog"].close(); selectedRecord = null; bindingPreview = null;};
  elements["close-source"].addEventListener("click", closeSource);
  elements["source-dialog"].addEventListener("cancel", closeSource);
  const invalidate = () => {bindingPreview = null; if (selectedRecord) renderSource();};
  elements["binding-value"].addEventListener("input", invalidate); elements["binding-kind"].addEventListener("change", invalidate);
  elements["binding-form"].addEventListener("submit", event => {
    event.preventDefault(); return runRequest(async () => {
      if (!selectedRecord) return;
      const directory = selectedRecord.local.directory;
      const preview = await managementPost("source-preview", {directory, kind: elements["binding-kind"].value, value: elements["binding-value"].value.trim()});
      if (selectedRecord && selectedRecord.local.directory === directory) bindingPreview = preview;
    });
  });
  elements["save-binding"].addEventListener("click", () => runRequest(async () => {
    if (!selectedRecord || !bindingPreview) return;
    const directory = selectedRecord.local.directory;
    const result = await managementPost("source-bind", {id: bindingPreview.id, activate: elements["activate-binding"].checked, acknowledged: true});
    if (selectedRecord && selectedRecord.local.directory === directory) selectedRecord = result;
    bindingPreview = null; await loadManagement(); message("来源关联已保存，安装历史保留。");
  }));
  elements["manifest-file"].addEventListener("change", () => runRequest(async () => {
    manifestPreview = null; installChoices.clear(); renderManifest();
    const file = elements["manifest-file"].files[0]; if (!file) return;
    if (file.size > 8 * 1024 * 1024) throw new Error("清单超过 8 MiB 限制。");
    manifestPreview = await bridge.upload("manage/manifest-upload/" + encodeURIComponent(managementState.csrf), file);
    installChoices = new Set(manifestPreview.plugins.filter(plugin => plugin.status === "install").map(plugin => plugin.market_plugin_id));
    renderManifest(); message("清单预检完成，请核对要从官方市场安装的新插件。");
  }));
  elements["install-manifest"].addEventListener("click", () => runRequest(async () => {
    if (!manifestPreview || !installChoices.size) return;
    const selected = manifestPreview.plugins.filter(plugin => installChoices.has(plugin.market_plugin_id));
    if (!await confirmOperation("安装 " + selected.length + " 个新插件", selected.map(plugin => ({...plugin, description: plugin.market_plugin_id + " · 市场版本 " + plugin.version})), "仅从官方市场安装新插件，不覆盖已有插件，不自动安装依赖或启用。单项失败会记录结果；安装后请重启扫描并手动启用。")) return;
    await managementPost("manifest-install", {id: manifestPreview.job.id, ids: Array.from(installChoices), acknowledged: true});
    manifestPreview = null; installChoices.clear(); renderManifest(); busy = true;
    showTab("history-panel"); await pollJobs(); message("清单安装任务已启动，请在任务记录查看逐项结果。");
  }));
}

initialize();
