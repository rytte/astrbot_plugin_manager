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
let displayedJobs = [];
const downloadingJobs = new Set();
const jobMenus = new Map();
let openJobMenuId = null;
const notifications = new Map();
let confirmationResolve = null;
let deletionResolve = null;
let recoveryPreview = null;
const recoveryChoices = new Map();
let managementState = {plugins: []};
let updateState = {id: null, status: "idle", results: [], done: 0, total: 0};
let managementStatus = "loading", managementRequest = 0;
let selectedRecord = null, bindingPreview = null, manifestPreview = null;
let manifestSelections = new Set(), installChoices = new Set();
const installSources = new Map();
let pendingManifestId = null, manifestPreviewRequest = 0, manifestPreviewLoading = false;
let manifestDraftQueue = Promise.resolve(), manifestDraftVersion = 0, manifestDraftSaving = false, manifestDraftError = false;
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

function dismissMessage(target) {
  const current = notifications.get(target);
  if (current && current.timer !== null) window.clearTimeout(current.timer);
  notifications.delete(target);
  target.hidden = true;
  target.replaceChildren();
}

function scheduleMessage(target) {
  const current = notifications.get(target);
  if (!current || current.error || current.hovered || current.focused || current.timer !== null) return;
  current.startedAt = Date.now();
  current.timer = window.setTimeout(() => {
    if (notifications.get(target) === current) dismissMessage(target);
  }, current.remaining);
}

function pauseMessage(target, reason) {
  const current = notifications.get(target);
  if (!current) return;
  current[reason] = true;
  if (current.timer !== null) {
    window.clearTimeout(current.timer);
    current.timer = null;
    current.remaining = Math.max(0, current.remaining - (Date.now() - current.startedAt));
  }
}

function resumeMessage(target, reason) {
  const current = notifications.get(target);
  if (!current) return;
  current[reason] = false;
  scheduleMessage(target);
}

function message(text, error = false, {task = false} = {}) {
  const target = elements[elements["source-dialog"].open ? "source-message" : "message"];
  const other = target === elements.message ? elements["source-message"] : elements.message;
  dismissMessage(other);
  if (!text) {dismissMessage(target); return;}
  const previous = notifications.get(target);
  if (previous?.text === text && previous.error === error && previous.task === task) return;
  if (previous && previous.timer !== null) window.clearTimeout(previous.timer);
  notifications.set(target, {text, error, task, timer: null, remaining: 4000, startedAt: 0, hovered: previous?.hovered || false, focused: false});
  target.className = "message" + (error ? " error" : "");
  target.setAttribute("role", error ? "alert" : "status");
  target.setAttribute("aria-live", error ? "assertive" : "polite");
  const icon = node("span", "message-icon");
  icon.setAttribute("aria-hidden", "true");
  const content = node("div", "message-content");
  content.append(node("strong", "message-title", error ? "操作未完成" : "操作提示"), node("p", "message-text", text));
  if (task) {
    const action = node("button", "text-button message-action", "查看任务");
    action.type = "button";
    action.addEventListener("click", () => {
      if (elements["source-dialog"].open) elements["close-source"].click();
      message("");
      showTab("history-panel");
      elements["history-panel"].scrollIntoView({block: "start"});
    });
    content.append(action);
  }
  const close = node("button", "message-close");
  close.type = "button";
  close.title = "关闭提示";
  close.setAttribute("aria-label", "关闭提示");
  close.addEventListener("click", () => dismissMessage(target));
  target.replaceChildren(icon, content, close);
  target.hidden = false;
  scheduleMessage(target);
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
function official(record) {return record && record.bindings.some(binding => binding.kind === "market" && binding.registry_url === null && !["metadata_changed", "imported"].includes(binding.verification));}
function shareableSources(record) {return record ? record.bindings.filter(binding => !["metadata_changed", "imported"].includes(binding.verification) && (binding.kind === "market" && binding.registry_url === null || binding.kind === "repository" && /^https:\/\//i.test(binding.repo_url))) : [];}
function eligible(record) {return record && !record.local.reserved && !record.local.problem && !record.identity_changed && shareableSources(record).length > 0;}
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
function checkedSource(record, binding) {
  return updateState.results.find(result => result.directory === record.local.directory && result.binding_id === binding.id);
}
function outdatedCheck(record, result) {
  return result.revision !== record.revision || result.is_current !== (result.binding_id === record.active_binding_id);
}
function sourceUpdate(record, binding) {
  const result = checkedSource(record, binding);
  if (!result || result.status === "no_update") return null;
  const outdated = outdatedCheck(record, result) && !["updated", "updating"].includes(result.status);
  const container = node("div", "source-update " + (outdated ? "stale" : result.status));
  const versions = result.remote_version ? "本机 " + result.local_version + " → 来源 " + result.remote_version + " · " : "";
  container.append(node("p", "update-result", versions + result.message + (outdated ? "；信息已变化，请重新检查" : "")));
  if (result.can_update && result.is_current && !outdated && updateState.status === "completed") {
    const action = button("更新", () => updatePlugin(record, binding, result), managementStatus !== "ready");
    action.className = "text-button update-plugin";
    action.setAttribute("aria-label", "更新 " + record.local.name + " 至 " + result.remote_version);
    container.append(action);
  }
  return container;
}
function renderUpdateSummary() {
  const checking = updateState.status === "running";
  elements["check-updates"].disabled = busy || requesting || checking || managementStatus !== "ready" || !managementState.csrf || !managementState.plugins.some(record => record.bindings.length);
  elements["check-updates"].textContent = checking ? "检查中…" : "检查更新";
  let text = "";
  if (checking) text = "正在检查已关联来源：" + updateState.done + " / " + updateState.total;
  else if (updateState.status === "completed") {
    const currentUpdates = updateState.results.filter(result => result.is_current && result.status === "update_available").length;
    const errors = updateState.results.filter(result => result.status === "error").length;
    text = "已检查 " + updateState.total + " 个关联来源 · 当前更新源有新版本：" + currentUpdates + " 个插件" + (errors ? " · 失败：" + errors + " 个来源" : "") + " · " + sourceTime(updateState.checked_at);
  } else if (updateState.status === "interrupted") text = updateState.error;
  elements["update-summary"].textContent = text;
  elements["update-summary"].hidden = !text;
}
async function checkUpdates() {
  updateState = await managementPost("check-updates", {});
  renderPlugins(); renderSummary();
  if (selectedRecord) renderSource();
}
async function updatePlugin(record, binding, result) {
  const checkId = updateState.id;
  if (outdatedCheck(record, result) || !result.can_update || binding.id !== record.active_binding_id) throw new Error("更新来源或插件信息已变化，请重新检查。");
  if (!await confirmOperation("确认更新插件", [{...record.local, description: sourceText(binding) + " · " + result.local_version + " → " + result.remote_version}], "将通过 AstrBot 下载、处理依赖并加载插件，保持原来的启停状态。更新前会重新核对当前来源和版本。")) return;
  const job = await managementPost("update-plugin", {check_id: checkId, directory: record.local.directory, binding_id: binding.id, acknowledged: true});
  result.can_update = false; result.status = "updating"; result.message = "正在更新";
  busy = true;
  previousBusy = true;
  renderJobs([job, ...displayedJobs.filter(previous => previous.id !== job.id)]);
  message("插件更新任务已启动，可在任务记录中查看结果。", false, {task: true});
  await pollJobs();
}
function metadataNotice(plugin, record) {
  const changed = record && managementStatus === "ready" ? record.metadata_changed : plugin.metadata_changed;
  if (!changed) return null;
  const notice = node("div", "plugin-notice");
  notice.append(node("span", "", record?.metadata_notice || plugin.metadata_notice || "插件信息已变更。"));
  const update = button("更新信息", () => syncMetadata(plugin.directory), !record || !managementState.csrf || managementStatus !== "ready" || record.identity_changed || Boolean(record.local.problem));
  update.className = "text-button metadata-sync";
  update.title = "同步插件管家记录，不重载插件";
  update.setAttribute("aria-label", "更新 " + (plugin.name || plugin.directory) + " 的插件信息");
  notice.append(update);
  return notice;
}
function renderFormats() {
  const isManifest = manifestMode();
  document.documentElement.dataset.exportFormat = isManifest ? "manifest" : "zip";
  elements["zip-summary"].hidden = isManifest;
  elements["manifest-summary"].hidden = !isManifest;
  elements.export.title = isManifest ? "接收者可选择插件及安装来源；现有插件跳过。" : "按当前选择打包，读取导出时的实际内容。";
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
    if (manifestMode()) title.append(node("div", eligible(record) ? "plugin-detail" : "plugin-error", eligible(record) ? "可分享 · " + (official(record) ? "已关联官方市场" : "已关联代码仓库") : "暂不能分享 · 请在来源管理中校验并关联市场或仓库"));
    else if (plugin.blocked) title.append(node("div", "plugin-error", plugin.blocked));
    const notice = metadataNotice(plugin, record);
    if (notice) title.append(notice);
    const dataCell = node("td"); dataCell.dataset.zipOnly = "";
    const dataCheck = checkbox("附带 " + plugin.name + " 的标准数据", choice.data, !choice.selected || !plugin.data_available || Boolean(plugin.blocked), checked => {choice.data = checked; renderSummary();});
    if (!plugin.data_available) dataCheck.append(document.createTextNode("不可用"));
    dataCell.append(dataCheck, node("div", "range-label", plugin.data_available ? "文件 " + statisticSize(plugin, "data_size") : "归属未知或自身数据"));
    dataCell.title = (plugin.ranges || []).join("\n");
    if (plugin.data_available) dataCell.title += (dataCell.title ? "\n" : "") + "键值存储：" + (Number.isFinite(plugin.kv_count) ? plugin.kv_count + " 条记录（随标准数据备份）" : plugin.statistics_status === "pending" ? "统计中…" : "未完成统计，请刷新状态重试。");
    const configCell = node("td"); configCell.dataset.zipOnly = "";
    const configCheck = checkbox("附带 " + plugin.name + " 的当前配置", choice.config, !choice.selected || !plugin.config_available || Boolean(plugin.blocked), checked => {choice.config = checked; renderSummary();});
    if (!plugin.config_available) configCheck.append(document.createTextNode("无已保存配置"));
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
    if (binding) {
      const result = sourceUpdate(record, binding);
      if (result) sourceCell.append(result);
    }
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
  renderUpdateSummary();
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
  elements["rule-status"].textContent = state.rule.selections.length ? "已保存 " + state.rule.selections.length + " 个插件的规则" : "";
  elements["rule-status"].hidden = !state.rule.selections.length;
  elements["rule-status"].title = state.rule.updated_at ? "保存于 " + new Date(state.rule.updated_at * 1000).toLocaleString("zh-CN") : "";
  elements["apply-rule"].disabled = busy || requesting || manifestMode() || !state.rule.selections.length;
  elements["save-rule"].disabled = busy || requesting || manifestMode() || !dirty;
  elements.export.textContent = manifestMode() ? "导出清单 JSON" : "导出插件包 ↗";
  elements.export.disabled = busy || requesting || (manifestMode() ? !manifestSelections.size || Array.from(manifestSelections).some(directory => !eligible(sourceRecord(directory))) : !selected.length || selectedPlugins.some(plugin => plugin.blocked));
  elements["export-format"].disabled = requesting;
  elements["import-format"].disabled = requesting;
  elements["manifest-file"].disabled = busy || requesting || manifestDraftSaving || !managementState.csrf;
  elements["install-manifest"].disabled = busy || requesting || manifestDraftSaving || manifestDraftError || !manifestPreview || manifestPreviewExpired() || !installChoices.size;
  renderManifestDraftStatus();
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
  if (recoveryPreview) renderRecovery();
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
    row.append(node("strong", "", plugin.display_name || plugin.name), node("p", "", (plugin.directory || plugin.name) + " · " + plugin.description));
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
  renderJobs(displayedJobs);
  if (selectedRecord) {dismissMessage(elements["source-message"]); renderSource();}
  try { await operation(); }
  catch (error) { message(error.message || "操作失败，请检查服务端日志。", true); }
  finally { requesting = false; renderPlugins(); renderSummary(); renderJobs(displayedJobs); if (preview) renderImportPreview(); if (manifestPreview) renderManifest(); if (selectedRecord) renderSource(); if (recoveryPreview) renderRecovery(); }
}

async function openRecovery(job, resume = false) {
  if (busy || requesting || !state.csrf) return;
  return runRequest(async () => {
    const result = resume ? await bridge.apiGet("transfer/rollback-plan", {id: job.id}) : await post("rollback-preview", {id: job.id});
    recoveryPreview = result;
    recoveryChoices.clear();
    for (const plugin of result.plugins) recoveryChoices.set(plugin.directory, Object.fromEntries(["code", "config", "data"].map(key => [key, !plugin.problem && Boolean(plugin.components[key])])));
    elements["recovery-acknowledged"].checked = false;
    elements["recovery-message"].hidden = true;
    await pollJobs();
    renderRecovery();
    elements["recovery-dialog"].showModal();
  });
}

function renderRecovery() {
  if (!recoveryPreview) return;
  const expired = Date.now() / 1000 > recoveryPreview.expires_at;
  elements["recovery-expiry"].textContent = "原导入任务 " + recoveryPreview.job.source_id.slice(0, 8) + " · 预检有效至 " + new Date(recoveryPreview.expires_at * 1000).toLocaleString("zh-CN");
  const list = elements["recovery-list"]; list.replaceChildren();
  for (const plugin of recoveryPreview.plugins) {
    const row = node("tr"), title = titleCell(plugin);
    if (plugin.problem) title.append(node("div", "plugin-error", plugin.problem));
    row.append(title);
    for (const key of ["code", "config", "data"]) {
      const cell = node("td");
      const action = plugin.components[key];
      if (!action) cell.append(node("span", "muted", "未备份"));
      else {
        const label = action === "remove" ? key === "data" ? "还原键值并移除目录" : "移除新增" : "恢复旧内容";
        const control = checkbox(plugin.name + " 的" + {code: "代码", config: "配置", data: "标准数据"}[key], recoveryChoices.get(plugin.directory)[key], busy || requesting || expired || Boolean(plugin.problem), checked => {recoveryChoices.get(plugin.directory)[key] = checked; renderRecovery();});
        control.append(document.createTextNode(label)); cell.append(control);
      }
      row.append(cell);
    }
    list.append(row);
  }
  if (expired) {elements["recovery-message"].textContent = "预检已过期，请从原导入任务重新预检。"; elements["recovery-message"].hidden = false;}
  elements["recovery-acknowledged"].disabled = requesting || busy || expired;
  elements["cancel-recovery"].disabled = requesting;
  elements["execute-recovery"].disabled = requesting || busy || expired || !elements["recovery-acknowledged"].checked || !Array.from(recoveryChoices.values()).some(choice => choice.code || choice.config || choice.data);
}

async function executeRecovery() {
  if (!recoveryPreview || !elements["recovery-acknowledged"].checked || busy || requesting) return;
  return runRequest(async () => {
    try {
      const selections = Array.from(recoveryChoices, ([directory, choice]) => ({directory, ...choice})).filter(choice => choice.code || choice.config || choice.data);
      const job = await post("rollback", {id: recoveryPreview.job.id, selections, acknowledged: true});
      elements["recovery-dialog"].close(); recoveryPreview = null; recoveryChoices.clear();
      busy = previousBusy = true;
      renderJobs([job, ...displayedJobs.filter(previous => previous.id !== job.id)]);
      message("还原任务已启动，可在任务记录查看结果。", false, {task: true});
      await pollJobs();
    } catch (error) {
      elements["recovery-message"].textContent = error.message || "还原未能启动，请重新预检。";
      elements["recovery-message"].hidden = false;
    }
  });
}

function renderImportPreview() {
  elements["import-preview"].hidden = !preview;
  if (!preview) return;
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

async function downloadJob(job, kind) {
  const key = job.id + "/" + kind;
  if (job.expired || downloadingJobs.has(key)) return;
  downloadingJobs.add(key);
  message("");
  renderJobs(displayedJobs);
  const manifest = kind === "manifest";
  try {
    await bridge.download(manifest ? "manage/manifest-download" : "transfer/download", manifest ? {id: job.id} : {id: job.id, kind}, manifest ? "plugins.astrbot-plugin-manifest.json" : "astrbot-" + kind + "-" + job.id.slice(0, 8) + ".zip");
    message("已发起" + (manifest ? "清单 JSON" : kind === "package" ? "插件包" : "恢复资料") + "下载，请查看浏览器下载记录。");
  } catch (error) {
    message(error.message || "下载失败，请重试。", true);
  } finally {
    downloadingJobs.delete(key);
    renderJobs(displayedJobs);
  }
}

function confirmJobDeletion(job) {
  const labels = {export: "导出插件包", import: "导入插件包", rollback: "还原到导入前", manifest_export: "导出插件清单", manifest_install: "清单安装", plugin_update: "更新插件"};
  elements["delete-job-title"].textContent = (labels[job.kind] || job.kind) + " · " + job.id.slice(0, 8);
  elements["delete-job-time"].textContent = new Date(job.created_at * 1000).toLocaleString("zh-CN");
  elements["delete-recovery-warning"].hidden = !job.backup_ready && !["interrupted", "rollback_failed"].includes(job.status);
  elements["delete-confirmation"].showModal();
  return new Promise(resolve => {deletionResolve = resolve;});
}

function finishJobDeletion(accepted) {
  elements["delete-confirmation"].close();
  if (deletionResolve) deletionResolve(accepted);
  deletionResolve = null;
}

async function deleteJob(job) {
  if (requesting || busy || ["preparing", "queued", "running"].includes(job.status) || Array.from(downloadingJobs).some(key => key.startsWith(job.id + "/"))) return;
  return runRequest(async () => {
    if (!await confirmJobDeletion(job)) return;
    await post("delete-job", {id: job.id, acknowledged: true});
    if (preview?.job.id === job.id) {
      preview = null; importChoices.clear();
      elements["bundle-file"].value = "";
      elements["file-label"].textContent = "预检任务已删除，请重新选择插件包。";
      renderImportPreview();
    }
    if (manifestPreview?.job.id === job.id || pendingManifestId === job.id) {
      clearManifestPreview();
      elements["manifest-file"].value = "";
      renderManifest();
    }
    await pollJobs();
    message("任务记录及相关文件已删除。");
  });
}

function closeJobMenu(restoreFocus = false) {
  const menu = jobMenus.get(openJobMenuId);
  openJobMenuId = null;
  if (!menu) return;
  menu.panel.hidden = true;
  menu.toggle.setAttribute("aria-expanded", "false");
  if (restoreFocus) menu.toggle.focus();
}

function openJobMenu(identifier) {
  closeJobMenu();
  const menu = jobMenus.get(identifier);
  if (!menu) return;
  openJobMenuId = identifier;
  menu.panel.hidden = false;
  menu.toggle.setAttribute("aria-expanded", "true");
  if (!menu.remove.disabled) menu.remove.focus();
}

function renderJobs(jobs) {
  const focused = document.activeElement?.dataset;
  const focusedJob = focused?.jobId, focusedControl = focused?.jobControl;
  displayedJobs = jobs;
  if (!jobs.some(job => job.id === openJobMenuId)) openJobMenuId = null;
  jobMenus.clear();
  elements["job-count"].textContent = jobs.length;
  const list = elements["job-list"];
  list.replaceChildren();
  const labels = {preparing: "上传 / 预检中", preview: "等待确认", queued: "排队", running: "进行中", completed: "已完成", completed_with_warning: "完成 · 需检查", failed: "失败", interrupted: "中断 · 需检查", rollback_failed: "自动恢复失败"};
  for (const job of jobs) {
    const card = node("article", "job-card");
    const heading = node("div", "job-heading");
    const title = node("div", "job-heading-main");
    title.append(node("span", "job-title", (job.preview_task && ["queued", "running"].includes(job.status) ? "清单预检" : ({export: "导出插件包", import: "导入插件包", rollback: "还原到导入前", manifest_export: "导出插件清单", manifest_install: "清单安装", plugin_update: "更新插件"}[job.kind] || job.kind))), node("span", "pill job-status " + (job.preview_expired ? "expired" : job.status), job.preview_expired ? "预检已过期" : labels[job.status] || job.status));
    const menu = node("div", "job-menu");
    const toggle = node("button", "job-menu-toggle", "⋯");
    toggle.type = "button";
    toggle.title = "更多操作";
    toggle.dataset.jobId = job.id; toggle.dataset.jobControl = "toggle";
    toggle.setAttribute("aria-label", "任务 " + job.id.slice(0, 8) + " 的更多操作");
    toggle.setAttribute("aria-haspopup", "menu");
    toggle.setAttribute("aria-expanded", String(openJobMenuId === job.id));
    toggle.setAttribute("aria-controls", "job-menu-" + job.id);
    toggle.addEventListener("click", () => openJobMenuId === job.id ? closeJobMenu(true) : openJobMenu(job.id));
    toggle.addEventListener("keydown", event => {
      if (["ArrowDown", "ArrowUp"].includes(event.key)) {event.preventDefault(); openJobMenu(job.id);}
    });
    const panel = node("div", "job-menu-panel");
    panel.id = "job-menu-" + job.id;
    panel.hidden = openJobMenuId !== job.id;
    panel.setAttribute("role", "menu");
    panel.setAttribute("aria-label", "任务操作");
    const remove = node("button", "job-menu-item", "删除记录及文件");
    remove.type = "button";
    remove.dataset.jobId = job.id; remove.dataset.jobControl = "remove";
    remove.setAttribute("role", "menuitem");
    remove.disabled = requesting || busy || ["preparing", "queued", "running"].includes(job.status) || Array.from(downloadingJobs).some(key => key.startsWith(job.id + "/"));
    remove.title = ["preparing", "queued", "running"].includes(job.status) ? "任务正在处理，结束后可删除" : "删除这条任务记录及全部相关文件";
    remove.addEventListener("click", () => {closeJobMenu(); return deleteJob(job);});
    remove.addEventListener("keydown", event => {
      if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {event.preventDefault(); remove.focus();}
    });
    menu.addEventListener("focusout", event => {if (jobMenus.get(job.id)?.menu === menu && openJobMenuId === job.id && !menu.contains(event.relatedTarget)) closeJobMenu();});
    panel.append(remove); menu.append(toggle, panel);
    jobMenus.set(job.id, {menu, toggle, panel, remove});
    heading.append(title, menu);
    card.append(heading, node("div", "job-meta", new Date(job.created_at * 1000).toLocaleString("zh-CN") + " · " + job.id.slice(0, 8)), node("div", "job-phase", job.phase));
    if (job.results.length) {
      const results = node("ul", "job-results");
      for (const result of job.results) results.append(node("li", "", result.directory + "：" + (result.message || "快照完成")));
      card.append(results);
    }
    for (const warning of job.warnings) card.append(node("p", "job-error", warning));
    if (job.error) card.append(node("p", "job-error", job.error));
    const actions = node("div", "job-actions");
    if (job.preview_task && ["queued", "running", "preview", "failed", "interrupted"].includes(job.status)) {
      const recheck = job.preview_expired || ["failed", "interrupted"].includes(job.status);
      const resume = node("button", "button secondary", recheck ? "重新预检" : job.status === "preview" ? "继续选择安装" : "查看预检进度");
      resume.disabled = requesting || manifestDraftSaving || (recheck && (busy || !managementState.csrf));
      resume.dataset.jobId = job.id; resume.dataset.jobControl = "resume";
      resume.addEventListener("click", () => openManifestTask(job, recheck));
      jobMenus.get(job.id).resume = resume;
      actions.append(resume);
      if (job.preview_expired) card.append(node("p", "job-meta", "原预检结果已过期，点击重新预检即可使用已上传的清单。"));
    }
    for (const kind of ["package", "rollback", "manifest"]) {
      const available = kind === "package" ? job.kind === "export" && ["completed", "completed_with_warning"].includes(job.status) : kind === "manifest" ? job.kind === "manifest_export" && job.status === "completed" : job.backup_ready && !["queued", "running"].includes(job.status);
      if (!available) continue;
      const downloading = downloadingJobs.has(job.id + "/" + kind);
      const download = node("button", "button secondary", job.expired ? "文件已被旧版清理" : downloading ? "正在下载…" : kind === "manifest" ? "下载清单 JSON ↓" : kind === "package" ? "下载插件包 ↓" : "下载恢复资料 ↓");
      download.disabled = job.expired || downloading;
      download.addEventListener("click", () => downloadJob(job, kind));
      actions.append(download);
    }
    if ((job.kind === "import" && job.backup_ready && ["completed", "completed_with_warning", "failed", "interrupted", "rollback_failed"].includes(job.status) && !job.expired) || (job.kind === "rollback" && job.status === "preview")) {
      const resume = job.kind === "rollback";
      const restore = node("button", "button secondary", resume ? job.preview_expired ? "重新预检还原" : "继续选择还原" : "还原到导入前");
      restore.disabled = busy || requesting || !state.csrf;
      restore.dataset.jobId = job.id; restore.dataset.jobControl = "restore";
      restore.addEventListener("click", () => resume && job.preview_expired ? openRecovery({id: job.source_id}) : openRecovery(job, resume));
      jobMenus.get(job.id).restore = restore;
      actions.append(restore);
    }
    if (actions.childNodes.length) card.append(actions);
    list.append(card);
  }
  if (!jobs.length) list.append(node("p", "empty", "暂无任务。导出插件包或清单，或上传文件开始迁移。"));
  const menu = jobMenus.get(focusedJob);
  if (menu?.[focusedControl]) (menu[focusedControl].disabled ? menu.toggle : menu[focusedControl]).focus();
}

function bindEvents() {
  elements["recovery-acknowledged"].addEventListener("change", renderRecovery);
  elements["execute-recovery"].addEventListener("click", executeRecovery);
  const closeRecovery = () => {elements["recovery-dialog"].close(); recoveryPreview = null; recoveryChoices.clear();};
  elements["cancel-recovery"].addEventListener("click", closeRecovery);
  elements["recovery-dialog"].addEventListener("cancel", event => {event.preventDefault(); if (!requesting) closeRecovery();});
  elements["check-updates"].addEventListener("click", () => runRequest(checkUpdates));
  bindManagementEvents();
  document.addEventListener("click", event => {
    const menu = jobMenus.get(openJobMenuId);
    if (menu && !menu.menu.contains(event.target)) closeJobMenu();
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && openJobMenuId !== null) {event.preventDefault(); closeJobMenu(true);}
  });
  for (const target of [elements.message, elements["source-message"]]) {
    target.addEventListener("mouseenter", () => pauseMessage(target, "hovered"));
    target.addEventListener("mouseleave", () => resumeMessage(target, "hovered"));
    target.addEventListener("focusin", () => pauseMessage(target, "focused"));
    target.addEventListener("focusout", event => {if (!target.contains(event.relatedTarget)) resumeMessage(target, "focused");});
  }
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
  elements.refresh.addEventListener("click", () => runRequest(() => {
    message("");
    return loadState(true);
  }));
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
    message("导出任务已启动。任务在服务端继续运行，可以稍后下载。", false, {task: true});
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
    message("恢复任务已启动。请勿在任务运行期间手工修改插件文件。", false, {task: true});
    await pollJobs();
  }));
  elements["confirmation-form"].addEventListener("submit", event => {event.preventDefault(); if (elements.acknowledged.checked) finishConfirmation(true);});
  elements["confirmation-cancel"].addEventListener("click", () => finishConfirmation(false));
  elements.confirmation.addEventListener("cancel", event => {event.preventDefault(); finishConfirmation(false);});
  elements["delete-job-form"].addEventListener("submit", event => {event.preventDefault(); finishJobDeletion(true);});
  elements["cancel-delete-job"].addEventListener("click", () => finishJobDeletion(false));
  elements["delete-confirmation"].addEventListener("cancel", event => {event.preventDefault(); finishJobDeletion(false);});
}

async function pollJobs() {
  const response = await bridge.apiGet("transfer/jobs");
  if (updateState.status === "running") {
    updateState = await bridge.apiGet("manage/updates");
    renderPlugins();
    if (selectedRecord) renderSource();
  }
  const finishedJobs = response.jobs.filter(job => displayedJobs.some(previous => previous.id === job.id && ["queued", "running"].includes(previous.status)));
  busy = response.busy;
  renderJobs(response.jobs);
  renderSummary();
  if (previousBusy && !busy) {
    await loadState(true);
    const failed = finishedJobs.find(job => ["failed", "rollback_failed", "interrupted"].includes(job.status));
    const checked = finishedJobs.find(job => job.preview_task && job.status === "preview");
    message(failed ? "任务执行未完成：" + (failed.error || "请查看任务记录中的原因和处理结果。") : checked ? "清单预检完成，可继续选择插件及安装来源。" : "任务已结束，请查看任务记录、下载文件或按提示处理警告。", Boolean(failed), {task: true});
  }
  if (busy !== previousBusy) renderPlugins();
  if (selectedRecord) await loadManagement();
  previousBusy = busy;
  await syncManifestTask(response.jobs);
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
    const pending = state.jobs.find(job => job.preview_task && !job.preview_expired && ["queued", "running", "preview"].includes(job.status));
    if (pending) await openManifestTask(pending);
    window.setInterval(() => {if (!requesting && !document.hidden) pollJobs().catch(error => message(error.message || "任务状态读取失败，请刷新页面。", true));}, 2000);
  } catch (error) {
    elements["boot-error"].hidden = false;
    elements["boot-error"].textContent = error.message || "插件页面初始化失败。";
  }
}


function updateManagement(updated) {
  managementState = updated;
  updateState = updated.updates;
  for (const plugin of state.plugins) {
    const record = sourceRecord(plugin.directory);
    if (record && record.local.name === plugin.name) {
      plugin.metadata_changed = record.metadata_changed;
      plugin.metadata_notice = record.metadata_notice;
    }
  }
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
async function syncMetadata(directory) {
  const record = sourceRecord(directory);
  if (!record) throw new Error("插件信息尚未读取完成，请稍后重试。");
  await managementPost("metadata-sync", {directory, revision: record.revision});
  bindingPreview = null;
  await loadState(true);
  await loadManagement();
  message("插件管家记录已更新，插件运行状态保持不变。");
}
async function openSource(directory) {
  await loadManagement();
  selectedRecord = managementState.plugins.find(record => record.local.directory === directory); bindingPreview = null;
  if (!selectedRecord) throw new Error("该插件暂无可管理的来源记录，请刷新列表。");
  message("");
  elements["source-install-details"].open = false; elements["source-history-details"].open = false;
  elements["binding-value"].value = "";
  renderSource(); elements["source-dialog"].showModal();
}
function renderSource() {
  if (!selectedRecord) return;
  const record = selectedRecord, local = record.local, current = active(record);
  const imported = record.bindings.some(binding => binding.verification === "imported");
  elements["source-title"].textContent = local.display_name || local.name || local.directory;
  elements["source-identity"].textContent = [local.name || local.directory, local.version && "v" + local.version.replace(/^v/i, ""), local.directory !== local.name && "目录：" + local.directory].filter(Boolean).join(" · ");
  elements["source-current-kind"].textContent = sourceKind(current);
  elements["source-current-kind"].className = "source-type" + (current ? "" : " unbound");
  const summary = elements["source-summary"]; summary.replaceChildren();
  summary.append(sourceAddress("source-current-value", current ? (current.kind === "market" ? current.market_plugin_id : current.repo_url) : "未绑定更新源"));
  if (!current) summary.append(node("p", "source-current-note", imported ? "来源信息已从备份恢复，请在下方校验后设为更新源。" : "可在下方关联来源，并将其设为更新源。"));
  if (local.problem || record.identity_changed) summary.append(node("p", "plugin-error", local.problem || "目录身份已变化，旧来源记录已隔离。"));
  const notice = metadataNotice(local, record);
  if (notice) summary.append(notice);
  elements["source-install-method"].textContent = sourceLabels[record.provenance.method] || record.provenance.method;
  const provenance = elements["source-provenance"]; provenance.replaceChildren();
  const facts = [
    ["安装记录", sourceLabels[record.provenance.method] || record.provenance.method],
    ["记录依据", record.provenance.evidence === "manager_operation" ? "管家实际执行安装" : record.provenance.evidence === "core_record_observed" ? "首次观察到 AstrBot 绑定记录；不能据此还原最初安装历史" : record.provenance.evidence === "bundle_record" ? "备份中的安装来源记录，仅供参考" : "缺少安装证据"],
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
    else if (binding.imported_active) heading.append(node("span", "badge", "备份时的更新源"));
    content.append(heading, sourceAddress("binding-address", binding.kind === "market" ? binding.market_plugin_id : binding.repo_url));
    if (binding.kind === "market" && binding.repo_url) content.append(sourceAddress("binding-repo", binding.repo_url));
    const verified = binding.verification === "identity_matched";
    content.append(node("p", "detail", verified ? "名称与作者已匹配" + (binding.checked_at ? " · " + sourceTime(binding.checked_at) : "") : binding.verification === "metadata_changed" ? "作者信息已变更 · 请重新校验后分享清单" : binding.verification === "imported" ? "从备份恢复 · 待校验" : "AstrBot 来源记录 · 尚未重新校验"));
    const result = sourceUpdate(record, binding);
    if (result) content.append(result);
    card.append(content);
    if (!isCurrent && (binding.kind === "repository" || binding.registry_url === null)) card.append(button(binding.verification === "imported" ? "校验并设为更新源" : "设为更新源", async () => {
      if (!await confirmOperation("切换更新源", [{name: local.name, directory: local.directory, description: sourceText(binding)}], "将重新校验该来源并同步到 AstrBot，后续原生 WebUI 更新使用此来源。最初安装记录保留。")) return;
      const result = await managementPost("source-select", {directory: local.directory, binding_id: binding.id, revision: record.revision, acknowledged: true});
      if (selectedRecord && selectedRecord.local.directory === local.directory) selectedRecord = result;
      bindingPreview = null; await loadManagement(); message("当前更新源已切换并同步到 AstrBot。");
    }));
    list.append(card);
  }
  if (!record.bindings.length) list.append(node("p", "source-empty", "尚未关联来源，可在下方添加。"));
  const blocked = local.reserved || local.problem || record.identity_changed;
  for (const id of ["binding-value", "binding-kind"]) elements[id].disabled = requesting || busy || Boolean(blocked);
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
  const labels = {installed: "管家安装", updated: "更新插件", uninstalled: "管家卸载", binding_added: "保存来源关联", active_changed: "切换当前更新源", core_observed: "观察到 AstrBot 来源变更", metadata_synced: "同步插件信息", bundle_imported: "从迁移包恢复代码", bundle_sources_imported: "恢复来源信息，等待重新校验"};
  for (const event of record.history.slice().reverse()) {const row = node("div", "history-row"); row.append(node("span", "history-event", labels[event.action] || event.action), node("span", "history-meta", sourceTime(event.at) + " · " + event.actor)); history.append(row);}
  if (!record.history.length) history.append(node("p", "source-empty", "暂无管家操作记录。"));
}

async function exportManifest() {
  const job = await managementPost("manifest-export", {directories: Array.from(manifestSelections)});
  await pollJobs();
  message("插件分享清单已导出，接收者可选择插件及安装来源。清单不包含配置和数据。");
  await downloadJob(job, "manifest");
}
function manifestPreviewExpired() {return manifestPreview && Date.now() / 1000 > manifestPreview.expires_at;}
function renderManifestDraftStatus() {
  elements["manifest-selection-status"].textContent = manifestPreviewExpired() ? "预检已过期，请从任务记录重新预检。" : manifestDraftSaving ? "正在保存选择…" : manifestDraftError ? "选择尚未保存，请重试；保存后才能安装。" : manifestPreview?.draft ? "选择已保存" : "修改选择后自动保存";
  elements["retry-manifest-draft"].hidden = !manifestDraftError || manifestPreviewExpired();
  elements["retry-manifest-draft"].disabled = requesting || manifestDraftSaving;
}
function clearManifestPreview() {
  ++manifestPreviewRequest; ++manifestDraftVersion;
  pendingManifestId = null; manifestPreview = null;
  installChoices.clear(); installSources.clear();
  manifestDraftSaving = false; manifestDraftError = false;
  elements["manifest-progress"].hidden = true;
  renderManifest();
}
function applyManifestResult(result) {
  if (result.ready === false) {
    pendingManifestId = result.job.id;
    elements["manifest-progress"].hidden = false;
    elements["manifest-progress"].textContent = result.job.phase || "正在解析清单来源…";
    return;
  }
  pendingManifestId = null;
  elements["manifest-progress"].hidden = true;
  manifestPreview = result;
  installChoices.clear(); installSources.clear();
  if (result.draft) {
    for (const choice of result.draft) {
      installSources.set(choice.id, choice.source);
      if (choice.selected) installChoices.add(choice.id);
    }
  } else for (const plugin of result.plugins) if (selectedManifestSource(plugin).status === "install") installChoices.add(plugin.id);
  renderManifest(); renderSummary();
}
function saveManifestDraft() {
  if (!manifestPreview || manifestPreviewExpired()) return Promise.resolve();
  const identifier = manifestPreview.job.id, version = ++manifestDraftVersion;
  const draft = manifestPreview.plugins.map(plugin => ({id: plugin.id, source: selectedManifestSource(plugin).kind, selected: installChoices.has(plugin.id)}));
  manifestDraftSaving = true; manifestDraftError = false; renderSummary();
  const operation = manifestDraftQueue.then(() => managementPost("manifest-draft", {id: identifier, draft}));
  manifestDraftQueue = operation.catch(() => {});
  return operation.then(() => {
    if (version === manifestDraftVersion && manifestPreview?.job.id === identifier) manifestPreview.draft = draft;
  }).catch(error => {
    if (version === manifestDraftVersion && manifestPreview?.job.id === identifier) {
      manifestDraftError = true;
      message(error.message || "安装选择保存失败，请重试。", true);
    }
  }).finally(() => {
    if (version === manifestDraftVersion) {manifestDraftSaving = false; renderSummary(); renderJobs(displayedJobs);}
  });
}
async function openManifestTask(job, recheck = false) {
  if (manifestDraftSaving) return;
  return runRequest(async () => {
    elements["import-format"].value = "manifest";
    showTab("import-panel"); renderFormats();
    if (!recheck && manifestPreview?.job.id === job.id && !manifestPreviewExpired()) {
      elements["manifest-preview"].scrollIntoView({block: "start"}); return;
    }
    clearManifestPreview();
    const current = manifestPreviewRequest;
    const result = recheck ? await managementPost("manifest-recheck", {id: job.id}) : await bridge.apiGet("manage/manifest-preview", {id: job.id});
    if (current !== manifestPreviewRequest) return;
    applyManifestResult(result);
    await pollJobs();
    message(result.ready === false ? "清单正在后台预检，可离开页面，稍后从任务记录继续。" : "已恢复清单预检及保存的选择，请核对后安装。");
  });
}
async function syncManifestTask(jobs) {
  if (manifestPreview) {
    const current = jobs.find(job => job.id === manifestPreview.job.id);
    if (!current || current.status !== "preview") {clearManifestPreview(); renderSummary();}
  }
  if (!pendingManifestId || manifestPreviewLoading) return;
  const job = jobs.find(job => job.id === pendingManifestId);
  if (!job || !["queued", "running", "preview"].includes(job.status)) {
    clearManifestPreview(); renderSummary();
    message(job?.error || "清单预检任务已结束或已删除，请从任务记录检查结果。", true, {task: true}); return;
  }
  if (job.status !== "preview") {
    elements["manifest-progress"].textContent = job.phase || "正在解析清单来源…"; return;
  }
  if (job.preview_expired) {clearManifestPreview(); renderSummary(); message("清单预检已过期，请从任务记录重新预检。", true, {task: true}); return;}
  const current = manifestPreviewRequest;
  manifestPreviewLoading = true;
  try {
    const result = await bridge.apiGet("manage/manifest-preview", {id: job.id});
    if (current === manifestPreviewRequest) {applyManifestResult(result); message("清单预检完成，请选择要安装的插件并核对安装来源。");}
  } finally {manifestPreviewLoading = false;}
}
function selectedManifestSource(plugin) {return plugin.sources.find(source => source.kind === (installSources.get(plugin.id) || plugin.default_source));}
function renderManifest() {
  elements["manifest-preview"].hidden = !manifestPreview;
  if (!manifestPreview) return;
  elements["preview-expiry"].textContent = "有效至 " + new Date(manifestPreview.expires_at * 1000).toLocaleString("zh-CN");
  const body = elements["manifest-list"]; body.replaceChildren();
  for (const plugin of manifestPreview.plugins) {
    const source = selectedManifestSource(plugin);
    const row = node("tr"), choice = node("td"), status = node("td"), locator = node("td", "manifest-source");
    const display = plugin.display_name || source.name || plugin.market_plugin_id || plugin.repo_url;
    choice.append(checkbox("安装 " + display, installChoices.has(plugin.id), source.status !== "install" || manifestPreviewExpired(), checked => {checked ? installChoices.add(plugin.id) : installChoices.delete(plugin.id); return saveManifestDraft();}));
    const title = titleCell({...source, display_name: display, name: source.name || "身份未确认", directory: source.directory || source.name || "身份未确认", version: source.version || "", status: ""});
    if (source.author) title.append(node("div", "plugin-detail", "作者：" + source.author));
    const select = node("select", "manifest-source-select");
    select.setAttribute("aria-label", "选择 " + display + " 的安装来源");
    for (const candidate of plugin.sources) {const option = node("option", "", candidate.kind === "market" ? "官方市场" : "代码仓库"); option.value = candidate.kind; select.append(option);}
    select.value = source.kind; select.disabled = busy || requesting || manifestPreviewExpired() || plugin.sources.length < 2;
    select.addEventListener("change", () => {
      const previouslyAvailable = source.status === "install";
      installSources.set(plugin.id, select.value);
      const updated = selectedManifestSource(plugin);
      if (updated.status !== "install") installChoices.delete(plugin.id);
      else if (!previouslyAvailable) installChoices.add(plugin.id);
      renderManifest(); renderSummary();
      return saveManifestDraft();
    });
    locator.append(select, sourceAddress("plugin-detail manifest-locator", source.kind === "market" ? plugin.market_plugin_id : plugin.repo_url));
    status.append(node("div", source.status === "blocked" ? "plugin-error" : "plugin-detail", {install: "可安装 · 自动加载", skip: "本机已有 · 跳过", blocked: "无法安装"}[source.status]));
    if (source.problem) status.append(node("div", "plugin-detail", source.problem));
    row.append(choice, title, locator, status); body.append(row);
  }
  const warnings = manifestPreview.job.warnings || [];
  elements["manifest-warnings"].hidden = !warnings.length;
  elements["manifest-warnings"].textContent = warnings.join("\n");
  renderManifestDraftStatus();
}
function bindManagementEvents() {
  const closeSource = () => {dismissMessage(elements["source-message"]); elements["source-dialog"].close(); selectedRecord = null; bindingPreview = null;};
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
    const result = await managementPost("source-bind", {id: bindingPreview.id, activate: true, acknowledged: true});
    if (selectedRecord && selectedRecord.local.directory === directory) selectedRecord = result;
    bindingPreview = null; await loadManagement(); message("来源关联已保存并设为当前更新源。");
  }));
  elements["manifest-file"].addEventListener("change", () => runRequest(async () => {
    clearManifestPreview();
    const file = elements["manifest-file"].files[0]; if (!file) return;
    if (file.size > 8 * 1024 * 1024) throw new Error("清单超过 8 MiB 限制。");
    elements["manifest-progress"].hidden = false;
    elements["manifest-progress"].textContent = "正在上传并解析清单来源，仓库安装包将先下载预检…";
    try {
      applyManifestResult(await bridge.upload("manage/manifest-upload/" + encodeURIComponent(managementState.csrf), file));
      await pollJobs();
      if (pendingManifestId) message("清单正在后台预检，可离开页面，稍后从任务记录继续。");
    } catch (error) {clearManifestPreview(); throw error;}
  }));
  elements["retry-manifest-draft"].addEventListener("click", saveManifestDraft);
  elements["install-manifest"].addEventListener("click", () => runRequest(async () => {
    if (!manifestPreview || manifestDraftSaving || manifestDraftError || manifestPreviewExpired() || !installChoices.size) return;
    const selected = manifestPreview.plugins.filter(plugin => installChoices.has(plugin.id));
    if (!await confirmOperation("安装 " + selected.length + " 个新插件", selected.map(plugin => {const source = selectedManifestSource(plugin); return {...source, display_name: plugin.display_name, description: (source.kind === "market" ? "官方市场 · " + plugin.market_plugin_id : "代码仓库 · " + plugin.repo_url) + " · 预览版本 " + source.version};}), "由 AstrBot 下载安装包、处理依赖并加载插件，会执行插件代码。清单不锁定版本，实际版本以安装结果为准。已有插件不覆盖；单项失败会记录结果，请在 WebUI 检查可能留下的变更。")) return;
    await managementPost("manifest-install", {id: manifestPreview.job.id, selections: selected.map(plugin => ({id: plugin.id, source: selectedManifestSource(plugin).kind})), acknowledged: true});
    clearManifestPreview(); busy = true;
    showTab("history-panel"); await pollJobs(); message("清单安装任务已启动，请在任务记录查看逐项结果。", false, {task: true});
  }));
}

initialize();
