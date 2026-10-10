"use strict";

const elements = Object.fromEntries(Array.from(document.querySelectorAll("[id]")).map(element => [element.id, element]));
let bridge;
let state;
let choices = new Map();
let preview = null;
let importChoices = new Map();
let busy = false;
let requesting = false;
let previousBusy = false;
let confirmationResolve = null;

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function formatSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KiB";
  if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " MiB";
  return (bytes / 1024 / 1024 / 1024).toFixed(2) + " GiB";
}

function message(text, error = false) {
  elements.message.textContent = text;
  elements.message.classList.toggle("error", error);
  elements.message.hidden = !text;
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

function titleCell(plugin) {
  const cell = node("td");
  const symbols = {enabled: "✅", disabled: "⏸️", failed: "❌", pending: "◌"};
  const marker = symbols[plugin.status] || "";
  cell.append(node("span", "plugin-title", marker + " " + (plugin.display_name || plugin.name)));
  cell.append(node("span", "plugin-id", plugin.name + (plugin.directory !== plugin.name ? " / " + plugin.directory : "")));
  if (plugin.version) cell.append(node("div", "plugin-detail", "版本 " + plugin.version));
  return cell;
}

function renderPlugins() {
  const body = elements["plugin-list"];
  body.replaceChildren();
  const plugins = visiblePlugins();
  elements["plugin-count"].textContent = state.plugins.length + " 个插件";
  for (const plugin of plugins) {
    const choice = choices.get(plugin.directory);
    const row = node("tr", choice.selected ? "selected" : "");
    const selectCell = node("td", "check-cell");
    selectCell.append(checkbox("选择插件 " + plugin.name, choice.selected, Boolean(plugin.blocked) && !choice.selected, checked => {choice.selected = checked; renderPlugins(); renderSummary();}));
    const title = titleCell(plugin);
    if (plugin.blocked) title.append(node("div", "plugin-error", plugin.blocked));
    const dataCell = node("td");
    const dataCheck = checkbox("附带 " + plugin.name + " 的标准数据", choice.data, !choice.selected || !plugin.data_available || Boolean(plugin.blocked), checked => {choice.data = checked; renderSummary();});
    dataCheck.append(document.createTextNode(plugin.data_available ? "标准数据" : "不可用"));
    dataCell.append(dataCheck, node("div", "range-label", plugin.data_available ? formatSize(plugin.data_size) + " · " + plugin.kv_count + " 个 KV" : "归属未知或自身数据"));
    dataCell.title = (plugin.ranges || []).join("\n");
    const configCell = node("td");
    const configCheck = checkbox("附带 " + plugin.name + " 的当前配置", choice.config, !choice.selected || !plugin.config_available || Boolean(plugin.blocked), checked => {choice.config = checked; renderSummary();});
    configCheck.append(document.createTextNode(plugin.config_available ? "当前配置" : "无已保存配置"));
    configCell.append(configCheck);
    row.append(selectCell, title, dataCell, configCell, node("td", "numeric", formatSize(plugin.code_size)));
    body.append(row);
  }
  if (!plugins.length) {
    const row = node("tr");
    const cell = node("td", "empty", "没有匹配的插件。");
    cell.colSpan = 5;
    row.append(cell);
    body.append(row);
  }
}

function renderSummary() {
  const selected = selections();
  const selectedPlugins = state.plugins.filter(plugin => choices.get(plugin.directory)?.selected);
  const dirty = isDirty();
  elements["selected-count"].textContent = selected.length;
  elements["data-count"].textContent = selected.filter(selection => selection.data).length + " 个";
  elements["config-count"].textContent = selected.filter(selection => selection.config).length + " 个";
  elements["selected-size"].textContent = formatSize(selectedPlugins.reduce((total, plugin) => total + plugin.code_size + (choices.get(plugin.directory).data ? plugin.data_size : 0) + (choices.get(plugin.directory).config ? plugin.config_size : 0), 0));
  elements["rule-status"].textContent = dirty ? "有未保存改动 · 请先保存规则" : state.rule.updated_at ? "规则已保存 · " + new Date(state.rule.updated_at * 1000).toLocaleString("zh-CN") : "尚未保存导出规则";
  elements["rule-status"].classList.toggle("dirty", dirty);
  elements["save-rule"].disabled = busy || requesting || !dirty;
  elements.export.disabled = busy || requesting || dirty || !state.rule.selections.length;
  elements["bundle-file"].disabled = busy || requesting;
  elements["execute-import"].disabled = busy || requesting || !preview || !Array.from(importChoices.values()).some(choice => choice.code || choice.config || choice.data);
  for (const identifier of ["bulk-data", "bulk-config", "select-visible", "clear-visible"]) elements[identifier].disabled = busy || requesting;
}

function showTab(identifier) {
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
  const updated = await bridge.apiGet("transfer/state");
  state = updated;
  busy = state.busy;
  if (!preserve) {
    choices = new Map(state.plugins.map(plugin => [plugin.directory, {selected: false, config: false, data: false}]));
    for (const selection of state.rule.selections) {
      choices.set(selection.directory, {selected: true, config: selection.config, data: selection.data});
    }
  }
  for (const plugin of state.plugins) if (!choices.has(plugin.directory)) choices.set(plugin.directory, {selected: false, config: false, data: false});
  for (const selection of state.rule.selections) {
    if (!choices.has(selection.directory)) choices.set(selection.directory, {selected: false, config: false, data: false});
    if (!state.plugins.some(plugin => plugin.directory === selection.directory)) state.plugins.push({directory: selection.directory, name: selection.name, display_name: "", version: "", status: "failed", blocked: "已保存规则中的插件不存在。请取消选择并重新保存规则。", data_available: false, config_available: false, code_size: 0, config_size: 0, data_size: 0, kv_count: 0});
  }
  elements["upload-limit"].textContent = "最大 " + formatSize(state.limits.archive_bytes) + " · 不接受普通插件安装 ZIP";
  renderPlugins();
  renderSummary();
  renderJobs(state.jobs);
}

async function runRequest(operation) {
  if (requesting) return;
  requesting = true;
  renderPlugins();
  renderSummary();
  try { await operation(); }
  catch (error) { message(error.message || "操作失败，请检查服务端日志。", true); }
  finally { requesting = false; renderPlugins(); renderSummary(); if (preview) renderImportPreview(); }
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
    heading.append(node("span", "job-title", job.kind === "export" ? "导出插件包" : "导入插件包"), node("span", "pill", labels[job.status] || job.status));
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
    if (actions.childNodes.length) card.append(actions);
    list.append(card);
  }
  if (!jobs.length) list.append(node("p", "empty", "暂无任务。保存规则或导入一个插件包，开始你的第一次迁移。"));
}

function bindEvents() {
  for (const button of document.querySelectorAll("[data-tab]")) button.addEventListener("click", () => showTab(button.dataset.tab));
  elements.search.addEventListener("input", renderPlugins);
  elements["status-filter"].addEventListener("change", renderPlugins);
  elements["select-visible"].addEventListener("click", () => {for (const plugin of visiblePlugins()) if (!plugin.blocked) choices.get(plugin.directory).selected = true; renderPlugins(); renderSummary();});
  elements["clear-visible"].addEventListener("click", () => {for (const plugin of visiblePlugins()) choices.get(plugin.directory).selected = false; renderPlugins(); renderSummary();});
  for (const kind of ["data", "config"]) elements["bulk-" + kind].addEventListener("change", event => {for (const plugin of state.plugins) if (choices.get(plugin.directory).selected && plugin[kind + "_available"] && !plugin.blocked) choices.get(plugin.directory)[kind] = event.target.checked; renderPlugins(); renderSummary();});
  elements.refresh.addEventListener("click", () => runRequest(() => loadState(true)));
  elements["save-rule"].addEventListener("click", () => runRequest(async () => {state.rule = await post("rule", {selections: selections()}); message("导出规则已保存。导出按钮将读取这套规则。");}));
  elements.export.addEventListener("click", () => runRequest(async () => {
    const checked = await post("export-preview");
    const accepted = await confirmOperation("检查本次导出", checked.plugins.map(plugin => ({...plugin, description: "代码" + (plugin.config ? " + 当前配置" : "") + (plugin.data ? " + 标准数据 / KV" : "") + (plugin.pause_required ? " · 将短暂暂停" : "")})), "配置和数据按原样备份，不会自动脱敏。附带数据时会暂停相关插件并尝试恢复原运行状态；外部写入可能导致快照失败。");
    if (!accepted) return;
    await post("export", {rule_updated_at: checked.rule_updated_at, acknowledged: true});
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
    preview = await bridge.upload("transfer/upload?csrf=" + encodeURIComponent(state.csrf), file);
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
    window.setInterval(() => {if (!requesting && !document.hidden) pollJobs().catch(error => message(error.message || "任务状态读取失败，请刷新页面。", true));}, 2000);
  } catch (error) {
    elements["boot-error"].hidden = false;
    elements["boot-error"].textContent = error.message || "插件页面初始化失败。";
  }
}

initialize();
