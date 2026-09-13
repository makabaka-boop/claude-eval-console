const UI_VERSION = "20260913.1";
const EXPORT_REFRESH_INTERVAL_MS = 5 * 60 * 1000;
const TABLE_PAGE_SIZE = 20;

const state = {
  runs: [],
  selectedId: null,
  health: null,
  detail: null,
  detailDisclosureState: new Map(),
  detailInteractionUntil: 0,
  timer: null,
  durationTimer: null,
  iterationTaskTypes: {},
  iterationJobs: {},
  iterationPollers: {},
  filters: { query: "", taskType: "", category: "", status: "" },
  runPage: 1,
  completedTurns: [],
  exportPage: 1,
  exportLastLoadedAt: 0,
  selectedExportTurns: new Set(),
  expandedExportPrompts: new Set(),
  exportEvaluationDrafts: new Map(),
  exportEvaluationBusy: new Set(),
  exportFilters: {
    query: "",
    taskType: "",
    difficulty: "",
    readiness: "",
    soloQaState: "",
    dateFrom: "",
    dateTo: "",
  },
  exportPreflight: null,
  exportPreflightBusy: false,
  exportDeleteBusy: false,
  hourlyAnalytics: null,
  analyticsDate: "",
  analyticsChartType: "bar",
  analyticsBusy: false,
  analyticsLastLoadedAt: 0,
  soloQaBridgeReady: false,
  soloQaBridgeVersion: "",
  soloQaBusy: false,
  soloQaLastMessage: "",
  soloQaRequests: new Map(),
  modelInputDirty: false,
  sortKey: "updated_at",
  sortDirection: "desc",
};

const $ = (selector) => document.querySelector(selector);
const recordsView = $("#records-view");
const detailView = $("#detail-view");
const exportView = $("#export-view");
const analyticsView = $("#analytics-view");
const pageTitle = $("#page-title");
const pageDescription = $("#page-description");
const newRunButton = $("#new-run-button");
const notice = $("#notice");

const phaseInfo = {
  generation_queued: ["题目生成排队中", "running"],
  generation_running: ["题目生成中", "running"],
  iteration_generation_running: ["题面生成中", "running"],
  queued: ["等待启动", "running"],
  first_retry_queued: ["等待重启第一轮", "running"],
  creating_repo: ["创建仓库", "running"],
  first_starting: ["等待容器终端", "running"],
  first_running: ["第一轮运行中", "running"],
  first_idle: ["第一轮完成／会话空闲", "ready"],
  review_queued: ["第一轮已推送／等待找 Bug", "ready"],
  review_running: ["会话空闲／找 Bug 中", "running"],
  awaiting_second: ["等待修复轮", "ready"],
  second_queued: ["修复待发送／会话空闲", "ready"],
  second_starting: ["正在发送修复题面", "running"],
  second_running: ["Bug 修复进行中", "running"],
  second_idle: ["本轮完成／会话空闲", "ready"],
  final_review_queued: ["本轮已推送／等待复查", "ready"],
  final_review_running: ["会话空闲／复查中", "running"],
  complete: ["任务已完成", "complete"],
  turn_limit: ["已到 10 轮上限", "warning"],
  manual_review: ["待人工确认", "warning"],
  interrupted: ["会话已中断", "warning"],
  failed: ["运行失败", "failed"],
  stopped: ["已终止", "failed"],
};

const soloQaStateInfo = {
  not_submitted: ["未提交", "not_submitted"],
  submitting: ["提交中", "submitting"],
  qc_pending: ["质检中", "qc_pending"],
  qc_passed: ["质检通过", "qc_passed"],
  needs_fix: ["需要返修", "needs_fix"],
  discarded: ["已废弃", "discarded"],
  failed: ["提交失败", "failed"],
  remote_missing: ["远端未找到", "remote_missing"],
  local_changed: ["本地数据已变化", "local_changed"],
  not_ready: ["待补资料", "not_ready"],
};

function exportSoloQaState(turn) {
  const soloQa = turn.solo_qa || {};
  if (soloQa.remote_id) return soloQa.state || "qc_pending";
  return turn.solo_qa_ready ? (soloQa.state || "not_submitted") : "not_ready";
}

function filteredCompletedTurns() {
  const filters = state.exportFilters;
  const query = filters.query.trim().toLocaleLowerCase("zh-CN");
  return state.completedTurns.filter((turn) => {
    const haystack = [turn.project_number, turn.repo_name, turn.run_id, turn.task_type]
      .join(" ")
      .toLocaleLowerCase("zh-CN");
    const completedDate = String(turn.completed_at || "").match(/^\d{4}-\d{2}-\d{2}/)?.[0] || "";
    const readiness = turn.export_ready ? "ready" : "blocked";
    return (!query || haystack.includes(query))
      && (!filters.taskType || turn.task_type === filters.taskType)
      && (!filters.difficulty || (turn.task_difficulty || "未记录") === filters.difficulty)
      && (!filters.readiness || readiness === filters.readiness)
      && (!filters.soloQaState || exportSoloQaState(turn) === filters.soloQaState)
      && (!filters.dateFrom || completedDate >= filters.dateFrom)
      && (!filters.dateTo || completedDate <= filters.dateTo);
  });
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function paginateItems(items, requestedPage) {
  const totalItems = items.length;
  const totalPages = Math.max(1, Math.ceil(totalItems / TABLE_PAGE_SIZE));
  const page = Math.min(totalPages, Math.max(1, Number(requestedPage) || 1));
  const startIndex = (page - 1) * TABLE_PAGE_SIZE;
  return {
    items: items.slice(startIndex, startIndex + TABLE_PAGE_SIZE),
    page,
    totalPages,
    totalItems,
    startItem: totalItems ? startIndex + 1 : 0,
    endItem: Math.min(totalItems, startIndex + TABLE_PAGE_SIZE),
  };
}

function paginationPageButtons(scope, page, totalPages) {
  const pages = [...new Set([1, page - 1, page, page + 1, totalPages])]
    .filter((value) => value >= 1 && value <= totalPages)
    .sort((first, second) => first - second);
  let previous = 0;
  return pages.map((value) => {
    const gap = value - previous > 1 ? '<span class="pagination-gap" aria-hidden="true">…</span>' : "";
    previous = value;
    return `${gap}<button class="pagination-page${value === page ? " current" : ""}" type="button" data-page-scope="${escapeHtml(scope)}" data-page-target="${value}" ${value === page ? 'aria-current="page"' : ""}>${value}</button>`;
  }).join("");
}

function renderTablePagination(scope, pagination) {
  const disabled = pagination.totalPages <= 1 ? "disabled" : "";
  const summary = pagination.totalItems
    ? `第 ${pagination.startItem}–${pagination.endItem} 条，共 ${pagination.totalItems} 条 · 每页 ${TABLE_PAGE_SIZE} 条`
    : `共 0 条 · 每页 ${TABLE_PAGE_SIZE} 条`;
  const controls = `<span class="pagination-summary">${summary}</span>
    <span class="pagination-actions">
      <button type="button" data-page-scope="${escapeHtml(scope)}" data-page-target="1" ${pagination.page <= 1 ? "disabled" : ""}>首页</button>
      <button type="button" data-page-scope="${escapeHtml(scope)}" data-page-target="${pagination.page - 1}" ${pagination.page <= 1 ? "disabled" : ""}>上一页</button>
      <span class="pagination-pages">${paginationPageButtons(scope, pagination.page, pagination.totalPages)}</span>
      <span class="pagination-position">第 ${pagination.page} / ${pagination.totalPages} 页</span>
      <button type="button" data-page-scope="${escapeHtml(scope)}" data-page-target="${pagination.page + 1}" ${pagination.page >= pagination.totalPages ? "disabled" : ""}>下一页</button>
      <button type="button" data-page-scope="${escapeHtml(scope)}" data-page-target="${pagination.totalPages}" ${pagination.page >= pagination.totalPages ? "disabled" : ""}>末页</button>
    </span>`;
  document.querySelectorAll(`[data-table-pagination="${scope}"]`).forEach((element) => {
    element.innerHTML = controls;
    element.classList.toggle("single-page", Boolean(disabled));
  });
}

function changeTablePage(scope, requestedPage) {
  if (scope === "runs") {
    state.runPage = requestedPage;
    renderRunList();
    return;
  }
  if (scope === "exports") {
    state.exportPage = requestedPage;
    renderExportPage();
  }
}

function renderTableTimestamp(value, tone = "neutral") {
  const text = String(value || "").trim();
  if (!text) return '<span class="table-timestamp empty">—</span>';
  const parts = text.match(/^(\d{4}-\d{2}-\d{2})[T\s]+(\d{2}:\d{2})(?::\d{2})?/);
  if (!parts) {
    return `<time class="table-timestamp ${escapeHtml(tone)}" title="${escapeHtml(text)}">${escapeHtml(text)}</time>`;
  }
  return `<time class="table-timestamp ${escapeHtml(tone)}" datetime="${escapeHtml(text)}" title="${escapeHtml(text)}" aria-label="${escapeHtml(parts[1])} ${escapeHtml(parts[2])}"><span class="timestamp-date">${escapeHtml(parts[1])}</span><strong class="timestamp-clock">${escapeHtml(parts[2])}</strong></time>`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  let data = null;
  try { data = await response.json(); } catch { data = {}; }
  if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
  return data;
}

function showNotice(message) {
  notice.textContent = message;
  notice.classList.remove("hidden");
  window.clearTimeout(showNotice.timer);
  showNotice.timer = window.setTimeout(() => notice.classList.add("hidden"), 7000);
}

function phaseLabel(phase) {
  return phaseInfo[phase] || [phase || "未知", ""];
}

function runNeedsAttention(run) {
  return String(run?.status_detail || "").includes("等待人工确认");
}

function displayedRunPhase(run) {
  return runNeedsAttention(run)
    ? ["等待人工确认", "failed"]
    : phaseLabel(run.phase);
}

function isRunning(phase) {
  return ["generation_queued", "generation_running", "iteration_generation_running", "queued", "first_retry_queued", "creating_repo", "first_starting", "first_running", "first_idle", "review_queued", "review_running", "awaiting_second", "second_queued", "second_starting", "second_running", "second_idle", "final_review_queued", "final_review_running"].includes(phase);
}

function renderNewRunButtonState() {
  const generating = state.runs.find((run) => ["generation_queued", "generation_running"].includes(run.phase));
  newRunButton.disabled = Boolean(generating);
  newRunButton.innerHTML = generating
    ? `${escapeHtml(generating.project_number || "新任务")} · 题目生成中…`
    : '<span>＋</span> 新建任务';
}

function runMatchesStatus(run, status) {
  if (!status) return true;
  if (status === "active") return isRunning(run.phase);
  if (status === "complete") return run.phase === "complete";
  if (status === "attention") return runNeedsAttention(run) || ["turn_limit", "manual_review", "interrupted", "failed", "stopped"].includes(run.phase);
  return true;
}

function filteredRuns() {
  const query = state.filters.query.trim().toLocaleLowerCase("zh-CN");
  return state.runs.filter((run) => {
    const taskType = run.current_task_type || run.task_type || "未记录";
    const haystack = [
      run.project_number,
      run.repo_name,
      run.id,
      taskType,
      run.project_category,
      run.language_framework,
      run.current_model || run.model,
    ].join(" ").toLocaleLowerCase("zh-CN");
    return (!query || haystack.includes(query))
      && (!state.filters.taskType || taskType === state.filters.taskType)
      && (!state.filters.category || run.project_category === state.filters.category)
      && runMatchesStatus(run, state.filters.status);
  });
}

function iterationTypeDescription(taskType) {
  if (taskType === "0-1 代码生成") {
    return "在当前项目中构建此前不存在、可独立验收的完整纵向模块。";
  }
  if (taskType === "Bug 修复") {
    return "检查当前项目并整理 3 至 4 个已复现问题，第一轮只写问题表现，不提供解决方法。";
  }
  return "复用现有核心对象，对已有流程、状态机、接口或页面做平滑扩展。";
}

function formatAutoRefillRemaining(seconds) {
  const totalMinutes = Math.max(0, Math.ceil(Number(seconds || 0) / 60));
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (!hours) return `${minutes} 分钟`;
  if (!minutes) return `${hours} 小时`;
  return `${hours} 小时 ${minutes} 分钟`;
}

function renderHealth() {
  const card = $("#environment-card");
  if (!state.health) return;
  const versionMatches = state.health.app_version === UI_VERSION;
  const ready = state.health.ready && versionMatches;
  const importBaselineButton = $("#open-import-baseline");
  if (importBaselineButton) {
    const supported = Boolean(state.health.imported_baseline_supported) && versionMatches;
    importBaselineButton.disabled = !supported;
    importBaselineButton.title = supported ? "" : "等待控制台安全重启后启用";
  }
  card.innerHTML = `
    <span class="status-dot ${ready ? "" : "bad"}"></span>
    <div>
      <strong>${versionMatches ? (ready ? "本机环境已就绪" : "本机环境需要检查") : "控制台前后端版本不一致，请安全重启服务"}</strong>
      <small>GitHub: ${escapeHtml(state.health.github_account || "未登录")} · Harness: ${escapeHtml(state.health.harness_version || "未识别")} · 并行 ${state.health.active_jobs}/${state.health.max_parallel}${state.health.queued_jobs ? ` · 排队 ${state.health.queued_jobs}` : ""}</small>
    </div>`;
  const modelInput = $("#global-model");
  if (modelInput && !state.modelInputDirty) {
    const options = state.health.model_options || (state.health.models || []).map((model) => ({ value: model, label: model }));
    if (state.health.model && !options.some((option) => option.value === state.health.model)) {
      options.push({ value: state.health.model, label: state.health.model });
    }
    modelInput.innerHTML = options.map((option) =>
      `<option value="${escapeHtml(option.value)}">${escapeHtml(option.label)}</option>`
    ).join("");
    modelInput.value = state.health.model;
  }
  const directoryOptions = $("#project-directory-options");
  const directoryInput = $("#project-directory");
  if (directoryOptions) {
    directoryOptions.innerHTML = (state.health.project_directories || []).map((directory) =>
      `<option value="${escapeHtml(directory)}"></option>`
    ).join("");
  }
  if (directoryInput && !directoryInput.value) {
    directoryInput.value = state.health.default_project_directory || "zzzz";
  }
  renderDirectoryPreview();
  $("#parallel-status").textContent = `并行 ${state.health.active_jobs} / ${state.health.max_parallel}${state.health.queued_jobs ? ` · 排队 ${state.health.queued_jobs}` : ""}`;
  const refill = state.health.auto_refill || {};
  const refillRow = $("#auto-refill-row");
  const refillButton = $("#auto-refill-toggle");
  const hasStartSchedule = Boolean(!refill.enabled && refill.enable_at);
  const hasStopSchedule = Boolean(refill.enabled && refill.disable_at);
  if (refillRow) {
    refillRow.classList.toggle("enabled", Boolean(refill.enabled));
    refillRow.classList.toggle("scheduled", hasStartSchedule);
    refillRow.classList.toggle("failed", Boolean(refill.error));
  }
  if (refillButton) {
    refillButton.disabled = false;
    refillButton.classList.toggle("enabled", Boolean(refill.enabled));
    refillButton.setAttribute("aria-pressed", refill.enabled ? "true" : "false");
    refillButton.textContent = `自动补题：${refill.enabled ? "开启" : (hasStartSchedule ? "等待开始" : "关闭")}`;
  }
  const refillDetail = $("#auto-refill-detail");
  if (refillDetail) {
    refillDetail.textContent = refill.error
      ? `${refill.detail}：${refill.error}`
      : (refill.detail || `并行不足 ${state.health.max_parallel} 时自动补位；每链最多 ${refill.max_iterations_per_root || 6} 轮，并混合安排 Feature、Bug 修复和最多 ${refill.max_new_modules_per_root || 2} 次完整模块。`);
    refillDetail.title = refillDetail.textContent;
  }
  const refillDeadline = $("#auto-refill-deadline");
  const refillSchedule = $("#auto-refill-schedule");
  const refillClear = $("#auto-refill-clear");
  const refillStartSchedule = $("#auto-refill-start-schedule");
  const refillStartClear = $("#auto-refill-start-clear");
  const refillStartControls = $("#auto-refill-start-controls");
  const refillStopControls = $("#auto-refill-stop-controls");
  refillStartControls?.classList.toggle(
    "hidden",
    Boolean(refill.enabled) || !refill.scheduled_start_supported,
  );
  refillStopControls?.classList.toggle(
    "hidden",
    !refill.enabled || !refill.scheduled_shutdown_supported,
  );
  if (refillDeadline) {
    refillDeadline.classList.toggle("hidden", !hasStartSchedule && !hasStopSchedule);
    refillDeadline.textContent = hasStartSchedule
      ? `预约开始：${new Date(refill.enable_at).toLocaleString("zh-CN", { hour12: false })}（剩余 ${formatAutoRefillRemaining(refill.start_remaining_seconds)}）`
      : (hasStopSchedule
        ? `定时关闭：${new Date(refill.disable_at).toLocaleString("zh-CN", { hour12: false })}（剩余 ${formatAutoRefillRemaining(refill.remaining_seconds)}）`
        : "");
  }
  if (refillSchedule) {
    refillSchedule.disabled = false;
    refillSchedule.textContent = hasStopSchedule
      ? "更新定时"
      : "设置定时关闭";
  }
  if (refillClear) {
    refillClear.disabled = false;
    refillClear.classList.toggle("hidden", !hasStopSchedule);
  }
  if (refillStartSchedule) {
    refillStartSchedule.disabled = false;
    refillStartSchedule.textContent = hasStartSchedule ? "更新开始时间" : "预约开始";
  }
  if (refillStartClear) {
    refillStartClear.disabled = false;
    refillStartClear.classList.toggle("hidden", !hasStartSchedule);
  }
  renderSoloQaControls();
}

function renderDirectoryPreview() {
  const preview = $("#directory-preview");
  const directoryInput = $("#project-directory");
  if (!preview || !directoryInput) return;
  const root = state.health?.projects_root || "/Users/zhangxinyu/claude code";
  const directory = directoryInput.value.trim() || state.health?.default_project_directory || "zzzz";
  const selectedPath = directory.startsWith("/") ? directory.replace(/\/$/, "") : `${root}/${directory.replace(/^\.\//, "")}`;
  preview.textContent = `新任务：${selectedPath}/0001-项目名（0001–2999） · 导入基线：3000–3999 · 难度由完成后的轨迹和产物评定`;
}

function renderRunList() {
  const list = $("#run-list");
  const visibleRuns = filteredRuns();
  const backgroundCount = state.runs.filter((run) => run.background_generation).length;
  const persistentCount = state.runs.length - backgroundCount;
  renderNewRunButtonState();
  $("#record-count").textContent = visibleRuns.length === state.runs.length
    ? `${persistentCount} 条记录${backgroundCount ? ` · ${backgroundCount} 个后台生成` : ""}`
    : `${visibleRuns.length} / ${state.runs.length} 项`;
  if (!state.runs.length) {
    state.runPage = 1;
    renderTablePagination("runs", paginateItems([], state.runPage));
    list.innerHTML = '<tr><td colspan="8" class="table-empty">还没有运行记录</td></tr>';
    return;
  }
  if (!visibleRuns.length) {
    state.runPage = 1;
    renderTablePagination("runs", paginateItems([], state.runPage));
    list.innerHTML = '<tr><td colspan="8" class="table-empty">没有符合筛选条件的运行记录</td></tr>';
    return;
  }
  const sortedRuns = [...visibleRuns].sort((first, second) => {
    const numericSort = ["current_turn", "project_number"].includes(state.sortKey);
    const firstValue = numericSort
      ? Number(first[state.sortKey] || 0)
      : String(first[state.sortKey] || first.updated_at || "");
    const secondValue = numericSort
      ? Number(second[state.sortKey] || 0)
      : String(second[state.sortKey] || second.updated_at || "");
    const result = firstValue < secondValue ? -1 : firstValue > secondValue ? 1 : 0;
    return state.sortDirection === "asc" ? result : -result;
  });
  const pagination = paginateItems(sortedRuns, state.runPage);
  state.runPage = pagination.page;
  renderTablePagination("runs", pagination);
  list.innerHTML = pagination.items.map((run) => {
    const [label, tone] = run.imported_baseline
      ? ["基线已导入", "complete"]
      : displayedRunPhase(run);
    const taskType = run.current_task_type || run.task_type || "未记录";
    const category = run.project_category && run.project_category !== "未记录" ? run.project_category : "";
    const categoryTone = category === "纯前端" ? "frontend" : category === "纯后端" ? "backend" : category === "全栈" ? "fullstack" : "neutral";
    const targetRunId = run.background_generation ? run.source_run_id : run.id;
    const recordMeta = run.background_generation
      ? `${run.background_kind || "后台任务"} · 来源 ${run.source_project_number || "—"}`
      : run.id;
    const actions = run.background_generation
      ? `<span class="record-actions"><button class="record-open" type="button" data-run-id="${escapeHtml(targetRunId)}">查看来源</button><span class="background-job-readonly">只读</span></span>`
      : `<span class="record-actions"><button class="record-open" type="button" data-run-id="${escapeHtml(targetRunId)}">查看详情</button><button class="record-delete" type="button" data-run-action="delete" data-run-id="${escapeHtml(targetRunId)}" ${isRunning(run.phase) ? 'disabled title="运行中不可删除"' : ""}>删除</button></span>`;
    const showStatusDetail = run.background_generation || runNeedsAttention(run);
    const statusContent = `<span class="run-status-stack"><span class="table-phase ${tone}" title="${escapeHtml(run.status_detail || label)}"><i aria-hidden="true"></i>${escapeHtml(label)}</span>${showStatusDetail ? `<small class="background-job-stage${runNeedsAttention(run) ? " attention-detail" : ""}">${escapeHtml(run.status_detail || "正在生成题面")}</small>` : ""}</span>`;
    return `<tr class="run-row${run.background_generation ? " background-generation" : ""} ${state.selectedId === run.id ? "active" : ""}" data-run-id="${escapeHtml(targetRunId)}" tabindex="0">
      <td data-label="编号"><span class="number-badge">${escapeHtml(run.project_number || "—")}</span></td>
      <td data-label="项目 / 仓库"><span><button class="record-name" type="button" data-run-id="${escapeHtml(targetRunId)}">${escapeHtml(run.repo_name)}</button><small>${escapeHtml(recordMeta)}</small></span></td>
      <td data-label="当前对话轮次"><span class="turn-badge">${escapeHtml(run.turn_label || `第 ${run.current_turn || 1} 轮`)}</span></td>
      <td data-label="任务类型"><span class="task-tags"><span class="task-type-badge">${escapeHtml(taskType)}</span>${category ? `<span class="category-badge ${categoryTone}">${escapeHtml(category)}</span>` : ""}</span></td>
      <td data-label="开始时间" class="time-column">${renderTableTimestamp(run.created_at, "started")}</td>
      <td data-label="当前状态">${statusContent}</td>
      <td data-label="更新时间" class="time-column">${renderTableTimestamp(run.updated_at || run.created_at, "updated")}</td>
      <td data-label="操作">${actions}</td>
    </tr>`;
  }).join("");
  document.querySelectorAll("[data-sort-key]").forEach((button) => {
    const active = button.dataset.sortKey === state.sortKey;
    button.classList.toggle("active", active);
    button.querySelector("span").textContent = active ? (state.sortDirection === "asc" ? "↑" : "↓") : "↕";
  });
}

function progressState(run) {
  const order = ["generation", "repo", "first", "review", "second", "final"];
  let current = 1;
  if (["first_retry_queued", "first_starting", "first_running"].includes(run.phase)) current = 2;
  if (["first_idle", "review_queued", "review_running", "awaiting_second"].includes(run.phase)) current = 3;
  if (["second_queued", "second_starting", "second_running"].includes(run.phase)) current = 4;
  if (["second_idle", "final_review_queued", "final_review_running", "manual_review"].includes(run.phase)) current = 5;
  if (["complete", "turn_limit"].includes(run.phase)) current = run.turn_count > 1 ? 6 : 4;
  if (["failed", "stopped", "interrupted"].includes(run.phase)) {
    if (run.second_prompt_id) current = 5;
    else if (run.first_prompt_id) current = run.second_prompt ? 4 : 3;
    else if (run.base_sha) current = 2;
  }
  return order.map((key, index) => {
    const timingStatus = run.stage_timings?.[key]?.status;
    return {
      key,
      done: timingStatus ? timingStatus === "done" : index < current,
      current: timingStatus ? timingStatus === "current" : index === current && current < order.length,
    };
  });
}

function parseServerTime(value) {
  if (!value) return null;
  const normalized = String(value)
    .replace(/^(\d{4}-\d{2}-\d{2}) /, "$1T")
    .replace(/ ([+-]\d{2})(\d{2})$/, "$1:$2");
  const timestamp = Date.parse(normalized);
  return Number.isNaN(timestamp) ? null : timestamp;
}

function stageSeconds(timing) {
  if (!timing) return 0;
  let seconds = Number(timing.elapsed_seconds || 0);
  if (timing.status === "current") {
    const measuredAt = parseServerTime(timing.measured_at);
    if (measuredAt) seconds += Math.max(0, (Date.now() - measuredAt) / 1000);
  }
  return seconds;
}

function formatDuration(seconds) {
  const totalMinutes = Math.floor(Math.max(0, seconds) / 60);
  if (totalMinutes < 1) return "不足 1 分钟";
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const minutes = totalMinutes % 60;
  if (days) return `${days} 天 ${hours} 小时`;
  if (hours) return `${hours} 小时 ${minutes} 分钟`;
  return `${minutes} 分钟`;
}

function stageDurationLabel(run, stage) {
  const timing = run.stage_timings?.[stage];
  if (run.phase === "complete" && !run.second_prompt && ["second", "final"].includes(stage)) return "无需执行";
  if (!timing || timing.status === "pending") return "尚未开始";
  const prefix = timing.status === "current" ? "已进行" : "用时";
  return `${prefix} ${formatDuration(stageSeconds(timing))}`;
}

function updateStageDurations() {
  if (!state.detail) return;
  detailView.querySelectorAll("[data-stage-key]").forEach((element) => {
    element.textContent = stageDurationLabel(state.detail, element.dataset.stageKey);
  });
}

function detailDisclosureId(runId, key) {
  return `${runId}:${key}`;
}

function detailOpenAttribute(runId, key, defaultOpen = false) {
  const id = detailDisclosureId(runId, key);
  const open = state.detailDisclosureState.has(id)
    ? state.detailDisclosureState.get(id)
    : defaultOpen;
  return open ? "open" : "";
}

function captureDetailDisclosureState(runId) {
  detailView.querySelectorAll("details[data-detail-key]").forEach((details) => {
    state.detailDisclosureState.set(
      detailDisclosureId(runId, details.dataset.detailKey),
      details.open,
    );
  });
}

function detailInteractionInProgress() {
  if (Date.now() < state.detailInteractionUntil) return true;
  const selection = window.getSelection?.();
  if (selection && !selection.isCollapsed) {
    const anchor = selection.anchorNode?.nodeType === Node.TEXT_NODE
      ? selection.anchorNode.parentElement
      : selection.anchorNode;
    const focus = selection.focusNode?.nodeType === Node.TEXT_NODE
      ? selection.focusNode.parentElement
      : selection.focusNode;
    if ((anchor && detailView.contains(anchor)) || (focus && detailView.contains(focus))) {
      return true;
    }
  }
  const active = document.activeElement;
  return Boolean(
    active
    && detailView.contains(active)
    && active.matches("input, textarea, select, [contenteditable='true']"),
  );
}

function showDetailRefreshHeld() {
  const status = document.getElementById("detail-refresh-status");
  if (!status) return;
  status.classList.add("held");
  status.textContent = "正在保留文字选择，完成复制后继续刷新";
}

function copyField(id) {
  const element = document.getElementById(id);
  if (!element) return;
  navigator.clipboard.writeText(element.dataset.copy || element.textContent || "")
    .then(() => showNotice("已复制"))
    .catch(() => showNotice("复制失败，请手动选择文本"));
}

function metadataItem(label, value, id, href = "") {
  if (!value) return `<div class="meta-item"><span>${label}</span><b>等待生成</b></div>`;
  const rendered = href
    ? `<a id="${id}" data-copy="${escapeHtml(value)}" href="${escapeHtml(href)}" target="_blank" rel="noreferrer">${escapeHtml(value)}</a>`
    : `<b id="${id}" data-copy="${escapeHtml(value)}">${escapeHtml(value)}</b>`;
  return `<div class="meta-item"><span>${label}</span><div class="copy-row">${rendered}<button class="icon-button" data-copy-id="${id}" title="复制" type="button">⧉</button></div></div>`;
}

function validationHtml(results, title) {
  if (!results?.length) return "";
  return `<div class="validation"><h3>${escapeHtml(title)}</h3>${results.map((item) => `
    <div class="validation-item">
      <div><b>${escapeHtml(item.command)}</b><span class="${item.exit_code === 0 ? "pass" : "fail"}">${item.exit_code === 0 ? "PASS" : `EXIT ${item.exit_code}`}</span></div>
      ${item.output ? `<pre>${escapeHtml(item.output)}</pre>` : ""}
    </div>`).join("")}</div>`;
}

function reviewHtml(review, title = "自动检查结果") {
  if (!review || (!review.summary && !review.bugs?.length && !review.quality_gaps?.length)) return "";
  return `<div class="review-panel">
    <div class="review-heading"><h3>${escapeHtml(title)}</h3><span>${review.bugs?.length || 0} 个已确认 Bug</span></div>
    ${review.summary ? `<p>${escapeHtml(review.summary)}</p>` : ""}
    ${(review.bugs || []).length ? `<div class="review-bugs">${review.bugs.map((bug) => `
      <div class="review-bug">
        <div><span>${escapeHtml(bug.severity || "待确认")}</span><b>${escapeHtml(bug.title || "未命名问题")}</b></div>
        <p><b>复现：</b>${escapeHtml(bug.reproduction || "")}</p>
        <p><b>实际：</b>${escapeHtml(bug.actual || "")}</p>
        <p><b>预期：</b>${escapeHtml(bug.expected || "")}</p>
        <p><b>证据：</b>${escapeHtml(bug.evidence || "")}</p>
        <small>${escapeHtml(bug.fix || "")}</small>
      </div>`).join("")}</div>` : ""}
    ${(review.quality_gaps || []).length ? `<div class="review-bugs">${review.quality_gaps.map((gap) => `
      <div class="review-bug">
        <div><span>质量建议</span><b>${escapeHtml(gap.title || "未命名建议")}</b></div>
        <p>${escapeHtml(gap.evidence || "")}</p>
        <small>${escapeHtml(gap.recommendation || "")}</small>
      </div>`).join("")}</div>` : ""}
  </div>`;
}

function evaluationHtml(evaluation, title) {
  if (!evaluation) return "";
  const dimensions = [
    ["交付完整性", evaluation.delivery],
    ["指令遵循", evaluation.instruction_following],
    ["任务规划", evaluation.planning],
    ["推理能力", evaluation.reasoning],
    ["执行能力", evaluation.execution],
  ];
  return `<div class="review-panel evaluation-panel">
    <div class="review-heading"><h3>${escapeHtml(title)}</h3><span>${escapeHtml(evaluation.task_type || "待判定")}</span></div>
    <p>${escapeHtml(evaluation.task_difficulty || "未记录")} · ${escapeHtml(evaluation.language_framework || "未记录")} · ${escapeHtml(evaluation.environment_reproducibility || "未记录")}</p>
    <div class="review-bugs">${dimensions.map(([label, item]) => `
      <div class="review-bug score-item">
        <div><span>${escapeHtml(item?.score || "-")} 分</span><b>${label}</b></div>
        <p>${escapeHtml(item?.description || "等待评分")}</p>
      </div>`).join("")}</div>
    ${evaluation.other_issues ? `<p><b>其他问题：</b>${escapeHtml(evaluation.other_issues)}</p>` : ""}
  </div>`;
}

function turnDeliveryRows(run, turn) {
  const turnNumber = Number(turn?.turn_number || 1);
  const evaluation = turn?.review_result?.evaluation;
  const value = (field, fallback = "") => evaluation?.[field] || fallback;
  const score = (field) => evaluation?.[field]?.score || "";
  const description = (field) => evaluation?.[field]?.description || "";
  return [
    ["User Prompt", turn?.prompt || (turnNumber === 1 ? run.first_prompt || "" : "")],
    ["SessionID", run.session_id || ""],
    ["TurnID/PromptID", turn?.prompt_id || ""],
    ["当前对话轮次排序", turnNumber],
    ["本轮 Git Commit", turn?.commit_sha || ""],
    ["初始环境快照", run.snapshot_url || ""],
    ["轨迹文件", turn?.trajectory_path || run.trajectory_path || ""],
    ["环境可复现等级", value("environment_reproducibility")],
    ["Harness", "Claude Code"],
    ["Harness 版本", run.harness_version || state.health?.harness_version || ""],
    ["操作系统", "MacOS/Linux"],
    ["任务类型", value("task_type", turn?.intent_type || (turnNumber === 1 ? "0-1 代码生成" : "Bug 修复"))],
    ["任务难度", value("task_difficulty", turnNumber === 1 ? run.task_difficulty || "" : "")],
    ["语言/框架", value("language_framework", run.language_framework || "")],
    ["交付完整性", score("delivery")],
    ["交付完整性 - 描述", description("delivery")],
    ["指令遵循", score("instruction_following")],
    ["指令遵循 - 描述", description("instruction_following")],
    ["任务规划", score("planning")],
    ["任务规划 - 描述", description("planning")],
    ["推理能力", score("reasoning")],
    ["推理能力 - 描述", description("reasoning")],
    ["执行能力", score("execution")],
    ["执行能力 - 描述", description("execution")],
    ["其他问题", ""],
    ["提交人", state.health?.submitter || "张鑫宇"],
  ];
}

function buildDeliveryText(run) {
  const turns = run.turns?.length ? run.turns : [{
    turn_number: 1,
    prompt: run.first_prompt,
    prompt_id: run.first_prompt_id,
    intent_type: "0-1 代码生成",
    review_result: run.review_result,
  }];
  return turns.map((turn) => {
    const rows = turnDeliveryRows(run, turn);
    return [`第 ${turn.turn_number} 轮`, ...rows.map(([key, value]) => `${key}\t${value}`)].join("\n");
  }).join("\n\n");
}

function turnHistoryHtml(runId, turns) {
  if (!turns?.length) return '<div class="side-empty">暂无逐轮记录</div>';
  return `<div class="turn-history">${turns.map((turn) => {
    const review = turn.review_result || {};
    const bugs = review.bugs || review.remaining_bugs || [];
    const reviewRecord = review.summary ? { ...review, bugs } : null;
    const statusLabels = {
      queued: "排队中",
      running: "Claude 执行中",
      reviewing: "GPT 检查中",
      complete: "本轮已归档",
      failed: "本轮失败",
      stopped: "已终止",
    };
    return `<section class="turn-record">
      <div class="turn-record-heading">
        <div><span>TURN ${escapeHtml(turn.turn_number)}</span><h3>第 ${escapeHtml(turn.turn_number)} 轮 · ${escapeHtml(turn.intent_type || "未记录")}</h3></div>
        <b>${escapeHtml(statusLabels[turn.status] || turn.status || "未记录")}</b>
      </div>
      <details class="turn-block turn-prompt-block" data-detail-key="turn-${turn.turn_number}-prompt" ${detailOpenAttribute(runId, `turn-${turn.turn_number}-prompt`, true)}>
        <summary>本轮题面（User Prompt） <span>${turn.prompt_id ? "已发送" : "待发送"}</span></summary>
        <div class="turn-content">${escapeHtml(turn.prompt || "")}</div>
      </details>
      <div class="meta-grid turn-meta-grid">
        ${metadataItem("PromptID", turn.prompt_id, `turn-${turn.turn_number}-prompt-id`)}
        ${metadataItem("Git Commit", turn.commit_sha, `turn-${turn.turn_number}-commit`)}
        ${metadataItem("轨迹检查点", turn.trajectory_path, `turn-${turn.turn_number}-trajectory`)}
        ${metadataItem("轨迹 SHA-256", turn.trajectory_sha256, `turn-${turn.turn_number}-trajectory-sha`)}
        ${metadataItem("Claude 模型", turn.model, `turn-${turn.turn_number}-model`)}
        ${metadataItem("本轮任务类型", review.evaluation?.task_type || turn.intent_type, `turn-${turn.turn_number}-type`)}
        ${metadataItem("后台任务", turn.agent_id, `turn-${turn.turn_number}-agent`)}
      </div>
      ${turn.result ? `<details class="turn-block" data-detail-key="turn-${turn.turn_number}-result" ${detailOpenAttribute(runId, `turn-${turn.turn_number}-result`)}><summary>Claude 执行结果 <span>展开查看</span></summary><div class="turn-content">${escapeHtml(turn.result)}</div></details>` : ""}
      ${validationHtml(turn.verification, `第 ${turn.turn_number} 轮 Docker 验收`)}
      ${reviewRecord ? reviewHtml(reviewRecord, `第 ${turn.turn_number} 轮 GPT 检查`) : ""}
      ${evaluationHtml(review.evaluation, `第 ${turn.turn_number} 轮评分`)}
    </section>`;
  }).join("")}</div>`;
}

function renderDetail() {
  const run = state.detail;
  if (!run) {
    detailView.innerHTML = $("#empty-detail-template").innerHTML;
    return;
  }
  const [label, tone] = run.imported_baseline
    ? ["基线已导入", "complete"]
    : displayedRunPhase(run);
  const progress = progressState(run);
  const steps = ["生成题目", "初始仓库", "第一轮开发", "首轮检查", "后续轮开发", "逐轮复查"];
  const canStop = ["generation_queued", "generation_running", "queued", "first_retry_queued", "creating_repo", "first_starting", "first_running", "first_idle", "review_queued", "review_running", "second_queued", "second_starting", "second_running", "second_idle", "final_review_queued", "final_review_running"].includes(run.phase);
  const canRetryFirst = ["interrupted", "stopped"].includes(run.phase)
    && run.container_cleaned && run.repo_url && run.base_sha && !run.retry_run_id;
  const canRetryGeneration = run.phase === "failed" && run.repo_name === "题目生成中"
    && !run.repo_url && !run.first_prompt_id;
  const canRetryStage = ["failed", "manual_review"].includes(run.phase) && Boolean(run.stage_retry_name);
  const turns = run.turns?.length ? run.turns : [];
  const latestTurn = turns.length ? turns[turns.length - 1] : null;
  const turnCount = Number(run.turn_count ?? 1);
  const eventCount = (run.events || []).length;
  const latestEvent = eventCount ? run.events[eventCount - 1] : null;
  const maxTurns = Number(state.health?.max_turns || 10);
  const canStartIteration = run.phase === "complete" && run.container_cleaned
    && run.repo_url && (run.first_prompt_id || run.imported_baseline);
  const selectedIterationTaskType = state.iterationTaskTypes[run.id] || "Feature 迭代";
  const iterationJob = state.iterationJobs[run.id] || null;
  const iterationPending = ["starting", "generating"].includes(iterationJob?.status);
  let iterationStatus = "";
  if (iterationPending) {
    const pollWarning = iterationJob.last_poll_error
      ? ` 状态查询暂时失败，系统会继续重试：${iterationJob.last_poll_error}`
      : " 页面自动刷新不会中断任务，完成后会自动进入新会话。";
    const stage = iterationJob.stage ? ` · ${iterationJob.stage}` : "";
    iterationStatus = `<div class="iteration-job-status running" role="status" aria-live="polite"><b>${escapeHtml(iterationJob.task_type || selectedIterationTaskType)}正在生成并复核${escapeHtml(stage)}</b><span>${escapeHtml(pollWarning)}</span><button class="danger-button compact-button" id="cancel-auto-iteration" type="button">取消生成</button></div>`;
  } else if (iterationJob?.status === "failed") {
    iterationStatus = `<div class="iteration-job-status failed" role="alert"><b>${escapeHtml(iterationJob.task_type || selectedIterationTaskType)}生成失败</b><span>${escapeHtml(iterationJob.error || "后台出题失败，可以保留当前项目后重试。")}</span></div>`;
  } else if (iterationJob?.status === "complete" && iterationJob.created_run_id) {
    iterationStatus = `<div class="iteration-job-status complete" role="status"><b>${escapeHtml(iterationJob.task_type || selectedIterationTaskType)}已经生成</b><span>独立会话已创建。</span><a href="#run/${escapeHtml(iterationJob.created_run_id)}">进入新会话 →</a></div>`;
  }
  if (detailView.dataset.runId === run.id) captureDetailDisclosureState(run.id);
  detailView.dataset.runId = run.id;
  detailView.innerHTML = `
    <div class="detail-navigation">
      <button class="back-button" id="back-to-list" type="button">← 返回运行记录</button>
      <span>任务详情</span>
    </div>
    <div class="run-summary">
      <div>
        <span class="phase-badge ${tone}">${escapeHtml(label)}</span>
        <h2>${escapeHtml(run.repo_name)}</h2>
        <p>${escapeHtml(run.status_detail || "")}</p>
      </div>
      <div class="summary-actions">
        ${run.imported_baseline ? "" : '<button class="secondary-button" id="copy-delivery" type="button">复制交付信息</button>'}
        ${run.repo_url ? `<a class="secondary-button" href="${escapeHtml(run.repo_url)}" target="_blank" rel="noreferrer">打开 GitHub ↗</a>` : ""}
        ${canRetryGeneration ? '<button class="primary-button" id="retry-generation" type="button">沿用原编号重新生成题面</button>' : ""}
        ${canRetryStage ? `<button class="primary-button" id="retry-stage" type="button">重试${escapeHtml(run.stage_retry_name)}</button>` : ""}
        ${canRetryFirst ? '<button class="primary-button" id="retry-first" type="button">用新会话重跑</button>' : ""}
        ${canStop ? '<button class="danger-button" id="stop-run" type="button">终止会话</button>' : ""}
      </div>
    </div>

    <nav class="detail-quickbar" aria-label="详情快速定位">
      <b>快速查看</b>
      <button type="button" data-detail-target="detail-overview">当前状态</button>
      <button type="button" data-detail-target="detail-task">题面信息</button>
      <button type="button" data-detail-target="detail-session">会话与快照</button>
      <button type="button" data-detail-target="detail-turns">逐轮记录（${escapeHtml(turnCount)}）</button>
      <button type="button" data-detail-target="detail-events">运行轨迹（${escapeHtml(eventCount)}）</button>
      <span id="detail-refresh-status">每 3 秒自动更新</span>
    </nav>

    ${canStartIteration ? `
      <section class="second-turn-box prominent-iteration-box" aria-labelledby="auto-iteration-title">
        <h3 id="auto-iteration-title">新建独立迭代会话</h3>
        <p><strong>${escapeHtml(state.health?.iteration_generation_model || "gpt-5.6-sol")}</strong> 会在后台读取当前项目，按照你选择的类型生成一段可直接开发的跨模块需求；不预设难度，完成后根据真实轨迹和产物评定。生成成功后从当前 commit 快照创建独立目录（沿用源编号并追加 -1、-2 迭代序号）、新容器、新终端和新 SessionID，并立即执行。</p>
        <div class="iteration-type-control">
          <label for="iteration-task-type">产出类型</label>
          <select id="iteration-task-type" ${iterationPending ? "disabled" : ""}>
            <option value="Feature 迭代" ${selectedIterationTaskType === "Feature 迭代" ? "selected" : ""}>平滑扩展（Feature 迭代）</option>
            <option value="0-1 代码生成" ${selectedIterationTaskType === "0-1 代码生成" ? "selected" : ""}>完整模块构建（0-1 代码生成）</option>
            <option value="Bug 修复" ${selectedIterationTaskType === "Bug 修复" ? "selected" : ""}>现有问题整理（Bug 修复）</option>
          </select>
          <small id="iteration-type-description">${escapeHtml(iterationTypeDescription(selectedIterationTaskType))}</small>
        </div>
        ${iterationStatus}
        <div class="second-actions"><button class="primary-button" id="auto-iteration-button" type="button" ${iterationPending ? "disabled" : ""}>${iterationPending ? `${escapeHtml(state.health?.iteration_generation_model || "gpt-5.6-sol")} 正在生成并复核…` : "生成迭代需求并新建独立会话 →"}</button></div>
      </section>` : ""}

    ${run.imported_baseline ? `
    <div class="progress-card imported-baseline-note" id="detail-overview">
      <b>这是一条外部完成基线</b>
      <span>控制台只核对了本地 Git、GitHub main 与 Compose 配置；没有在这里执行 0-1 开发，所以不展示虚构的开发耗时或对话轮次。</span>
    </div>` : `
    <div class="progress-card" id="detail-overview">
      <div class="progress-caption"><b>环节耗时</b><span>每分钟更新</span></div>
      <div class="progress-track">
        ${progress.map((step, index) => `<div class="progress-step ${step.done ? "done" : ""} ${step.current ? "current" : ""}"><b>${step.done ? "✓" : index}</b><span><strong>${steps[index]}</strong><small data-stage-key="${step.key}">${escapeHtml(stageDurationLabel(run, step.key))}</small></span></div>`).join("")}
      </div>
    </div>`}

    ${run.error ? `<div class="error-panel">${escapeHtml(run.error)}</div>` : ""}

    <div class="detail-grid">
        <details class="detail-card detail-section" id="detail-task" data-detail-key="task" ${detailOpenAttribute(run.id, "task", true)}>
          <summary class="detail-section-summary">
            <div><span>FIRST TURN</span><h3>题面信息</h3></div>
            <small>${escapeHtml(run.project_number || "未编号")} · ${escapeHtml(run.task_type || "0-1 项目开发")}</small>
          </summary>
          <div class="detail-section-body">
          <div class="meta-grid">
            ${metadataItem("项目编号", run.project_number, "project-number-value")}
            ${metadataItem("任务类型", run.task_type, "task-type-value")}
            ${metadataItem("项目类别", run.project_category, "project-category-value")}
            ${metadataItem("任务难度", run.task_difficulty, "task-difficulty-value")}
            ${metadataItem("语言 / 框架", run.language_framework, "framework-value")}
            ${metadataItem("GitHub 仓库名", run.repo_name, "repo-name-value")}
            ${metadataItem("本地保存分组", run.project_directory, "project-directory-value")}
            ${metadataItem("本题运行目录", run.run_directory, "run-directory-value")}
            ${metadataItem("本地项目目录", run.repo_path, "repo-path-value")}
            ${metadataItem("当前对话轮次", run.turn_label, "turn-value")}
          </div>
          <div class="problem-statement">
            <span>${run.imported_baseline ? "原始 0-1 User Prompt" : "第一轮 User Prompt"}</span>
            <div>${escapeHtml(run.first_prompt)}</div>
          </div>
          <div class="acceptance-block">
            <span>验收命令</span>
            <div class="acceptance-commands">
              ${(run.verification_commands || []).length
                ? run.verification_commands.map((command) => `<code>${escapeHtml(command)}</code>`).join("")
                : "<em>未设置验收命令</em>"}
            </div>
          </div>
          </div>
        </details>

        <details class="detail-card detail-section" id="detail-session" data-detail-key="session" ${detailOpenAttribute(run.id, "session")}>
          <summary class="detail-section-summary">
            <div><span>SESSION</span><h3>会话与快照</h3></div>
            <small>${escapeHtml(run.imported_baseline ? "导入基线，无 Claude 会话" : (run.session_id || "等待 SessionID"))}</small>
          </summary>
          <div class="detail-section-body">
          <div class="meta-grid">
            ${metadataItem("初始环境快照", run.snapshot_url, "snapshot-value", run.snapshot_url)}
            ${metadataItem("SessionID", run.session_id, "session-value")}
            ${metadataItem("第一轮 PromptID", run.first_prompt_id, "first-prompt-id")}
            ${metadataItem("最近一轮 PromptID", latestTurn?.prompt_id, "latest-prompt-id")}
            ${metadataItem("第一轮模型", run.model, "first-model")}
            ${metadataItem("题目生成模型", state.health?.task_generation_model, "task-generation-model")}
            ${metadataItem("迭代出题模型", state.health?.iteration_generation_model, "iteration-generation-model")}
            ${metadataItem("检查模型", run.review_model || state.health?.review_model, "review-model")}
            ${metadataItem("最近一轮 Claude 模型", latestTurn?.model || run.model, "latest-model")}
            ${metadataItem("上下文上限", state.health?.claude_context_tokens ? `${state.health.claude_context_tokens} tokens` : "", "context-window")}
            ${metadataItem("容器镜像", state.health?.docker_image, "docker-image")}
            ${metadataItem("容器名称", run.container_name, "container-name")}
            ${metadataItem("终端会话", run.screen_name || latestTurn?.agent_id, "agent-value")}
            ${metadataItem("来源任务", run.source_run_id, "source-run-id")}
            ${metadataItem("最终工作目录", run.workspace_path || run.repo_path, "workspace-value")}
            ${metadataItem("轨迹文件", run.trajectory_path, "trajectory-path")}
          </div>
          </div>
        </details>

        <details class="detail-card detail-section" id="detail-turns" data-detail-key="turns" ${detailOpenAttribute(run.id, "turns")}>
          <summary class="detail-section-summary">
            <div><span>TURNS</span><h3>逐轮记录</h3></div>
            <small>${escapeHtml(turnCount)} 轮 · ${escapeHtml(latestTurn?.status || "等待开始")}</small>
          </summary>
          <div class="detail-section-body detail-turns-body">
            ${turnHistoryHtml(run.id, turns)}
          </div>
        </details>

      <details class="detail-card detail-section" id="detail-events" data-detail-key="events" ${detailOpenAttribute(run.id, "events")}>
        <summary class="detail-section-summary">
          <div><span>EVENTS</span><h3>运行轨迹</h3></div>
          <small>${escapeHtml(eventCount)} 条${latestEvent?.message ? ` · ${escapeHtml(latestEvent.message)}` : ""}</small>
        </summary>
        <div class="detail-section-body">
        <div class="timeline">
          ${(run.events || []).length ? run.events.map((event) => `<div class="event ${escapeHtml(event.level)}"><b>${escapeHtml(event.message)}</b><span>${escapeHtml(event.created_at)}</span></div>`).join("") : '<div class="side-empty">等待第一条状态更新</div>'}
        </div>
        </div>
      </details>
    </div>`;

  updateStageDurations();
  $("#copy-delivery")?.addEventListener("click", () => {
    navigator.clipboard.writeText(buildDeliveryText(run)).then(() => showNotice("交付信息已复制"));
  });
  $("#back-to-list")?.addEventListener("click", () => navigateTo("#runs"));
  $("#stop-run")?.addEventListener("click", stopSelectedRun);
  $("#retry-generation")?.addEventListener("click", retryAutomaticGeneration);
  $("#retry-stage")?.addEventListener("click", retryControlStage);
  $("#retry-first")?.addEventListener("click", retryFirstTurn);
  $("#auto-iteration-button")?.addEventListener("click", startAutomaticIteration);
  $("#cancel-auto-iteration")?.addEventListener("click", cancelAutomaticIteration);
  $("#iteration-task-type")?.addEventListener("change", (event) => {
    state.iterationTaskTypes[run.id] = event.target.value;
    delete state.iterationJobs[run.id];
    renderDetail();
    loadAutomaticIterationStatus(run.id, event.target.value);
  });
  detailView.querySelectorAll("[data-copy-id]").forEach((button) => {
    button.addEventListener("click", () => copyField(button.dataset.copyId));
  });
  detailView.querySelectorAll("details[data-detail-key]").forEach((details) => {
    details.addEventListener("toggle", () => {
      state.detailDisclosureState.set(
        detailDisclosureId(run.id, details.dataset.detailKey),
        details.open,
      );
    });
  });
  detailView.querySelectorAll("[data-detail-target]").forEach((button) => {
    button.addEventListener("click", () => {
      const target = document.getElementById(button.dataset.detailTarget);
      if (!target) return;
      if (target instanceof HTMLDetailsElement) target.open = true;
      target.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });
}

async function loadHealth() {
  try {
    state.health = await api("/api/health");
    renderHealth();
  } catch (error) {
    showNotice(error.message);
  }
}

async function loadRuns(keepSelection = true) {
  try {
    const [runs, backgroundJobs] = await Promise.all([
      api("/api/runs"),
      api("/api/runs/background-jobs"),
    ]);
    state.runs = [...runs, ...backgroundJobs];
    if (!keepSelection && state.runs.length) state.selectedId = state.runs[0].id;
    renderRunList();
  } catch (error) {
    showNotice(error.message);
  }
}

async function loadDetail() {
  if (!state.selectedId) return;
  try {
    const detail = await api(`/api/runs/${state.selectedId}`);
    state.detail = detail;
    if (detailInteractionInProgress()) {
      showDetailRefreshHeld();
      updateStageDurations();
      return;
    }
    renderDetail();
  } catch (error) {
    showNotice(error.message);
  }
}

function soloQaSubmittable(turn) {
  const stateName = turn.solo_qa?.state || "not_submitted";
  return Boolean(turn.solo_qa_ready) && ["not_submitted", "failed", "remote_missing"].includes(stateName);
}

function soloQaConversationKey(turn) {
  return String(turn.session_id || turn.run_id || "");
}

function orderSoloQaTurns(turns) {
  const groups = new Map();
  turns.forEach((turn) => {
    const key = soloQaConversationKey(turn);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(turn);
  });
  return [...groups.values()].flatMap((group) =>
    [...group].sort((first, second) => Number(first.turn_number) - Number(second.turn_number))
  );
}

function missingSoloQaPredecessor(turns) {
  const selectedKeys = new Set(turns.map((turn) => turn.key));
  for (const turn of turns) {
    const earlierTurns = state.completedTurns
      .filter((candidate) =>
        soloQaConversationKey(candidate) === soloQaConversationKey(turn)
        && Number(candidate.turn_number) < Number(turn.turn_number)
      )
      .sort((first, second) => Number(first.turn_number) - Number(second.turn_number));
    const missing = earlierTurns.find((candidate) =>
      !selectedKeys.has(candidate.key) && !candidate.solo_qa?.remote_id
    );
    if (missing) return { turn, missing };
  }
  return null;
}

function renderSoloQaControls() {
  const bridgeStatus = $("#solo-qa-bridge-status");
  const detail = $("#solo-qa-status-detail");
  const helperPath = $("#solo-qa-helper-path");
  const syncButton = $("#solo-qa-sync");
  const submitButton = $("#solo-qa-submit");
  if (!bridgeStatus || !detail || !syncButton || !submitButton) return;
  if (helperPath) {
    helperPath.textContent = state.health?.solo_qa?.helper_path || "尚未取得提交助手路径";
  }
  bridgeStatus.className = "solo-qa-bridge-status";
  if (state.soloQaBusy) {
    bridgeStatus.classList.add("busy");
    bridgeStatus.textContent = "正在处理";
  } else if (state.soloQaBridgeReady) {
    bridgeStatus.classList.add("connected");
    bridgeStatus.textContent = `提交助手已连接${state.soloQaBridgeVersion ? ` · v${state.soloQaBridgeVersion}` : ""}`;
  } else {
    bridgeStatus.classList.add("disconnected");
    bridgeStatus.textContent = "提交助手未连接";
  }
  detail.textContent = state.soloQaLastMessage || (state.soloQaBridgeReady
    ? "历史状态按需手动同步；提交时只核对所选轮次，并自动上传对应轨迹。"
    : "安装一次 Chrome 提交助手后，可同步历史提交并自动上传轨迹。");
  const selected = state.completedTurns.filter((turn) =>
    state.selectedExportTurns.has(turn.key) && soloQaSubmittable(turn)
  ).length;
  syncButton.disabled = !state.soloQaBridgeReady || state.soloQaBusy;
  submitButton.disabled = !state.soloQaBridgeReady || state.soloQaBusy || state.exportPreflightBusy || selected === 0;
  submitButton.textContent = selected > 0 ? `提交所选轮次（${selected}）` : "提交所选轮次";
}

function pingSoloQaBridge() {
  window.postMessage(
    { source: "claude-eval-console", type: "SOLO_QA_BRIDGE_PING" },
    window.location.origin,
  );
}

function requestSoloQaBridge(type, payload = {}, timeoutMs = 10 * 60 * 1000) {
  if (!state.soloQaBridgeReady) return Promise.reject(new Error("Chrome 提交助手未连接"));
  const requestId = window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => {
      state.soloQaRequests.delete(requestId);
      reject(new Error("提交助手等待超时；请同步我的提交确认远端是否已收到"));
    }, timeoutMs);
    state.soloQaRequests.set(requestId, { resolve, reject, timer });
    window.postMessage(
      { source: "claude-eval-console", type, requestId, payload },
      window.location.origin,
    );
  });
}

async function syncSoloQa({ silent = false } = {}) {
  if (!state.soloQaBridgeReady || state.soloQaBusy) return;
  state.soloQaBusy = true;
  state.soloQaLastMessage = "正在读取 SOLO-QA 的我的提交…";
  renderSoloQaControls();
  try {
    const result = await requestSoloQaBridge("SOLO_QA_SYNC", {}, 3 * 60 * 1000);
    state.soloQaLastMessage = `已同步远端 ${result.remote_total || 0} 条；匹配本地 ${result.matched || 0} 条${result.unmatched ? `，${result.unmatched} 条在本地未找到` : ""}${result.remote_missing ? `，${result.remote_missing} 条远端已不存在` : ""}${result.partial ? "；远端超过 500 条，本次仅同步最近 500 条" : ""}`;
    await loadCompletedTurns();
    if (!silent) showNotice(state.soloQaLastMessage);
  } catch (error) {
    state.soloQaLastMessage = error.message;
    if (!silent) showNotice(error.message);
  } finally {
    state.soloQaBusy = false;
    renderSoloQaControls();
  }
}

async function submitSelectedToSoloQa() {
  if (state.soloQaBusy) return;
  const turns = orderSoloQaTurns(
    state.completedTurns.filter((turn) =>
      state.selectedExportTurns.has(turn.key) && soloQaSubmittable(turn)
    ),
  );
  if (!turns.length) {
    showNotice("所选轮次都已提交，或尚未满足 SOLO-QA 提交条件");
    return;
  }
  const predecessor = missingSoloQaPredecessor(turns);
  if (predecessor) {
    showNotice(
      `${predecessor.turn.project_number || predecessor.turn.repo_name} 第 ${predecessor.turn.turn_number} 轮提交前，需先提交或同时选择第 ${predecessor.missing.turn_number} 轮`,
    );
    return;
  }
  if (!await selectedTurnsPassPreflight(turns.map((turn) => turn.key))) return;
  const preview = turns.slice(0, 6).map((turn) =>
    `${turn.project_number || "—"} ${turn.repo_name} · 第 ${turn.turn_number} 轮`
  ).join("\n");
  const extra = turns.length > 6 ? `\n另有 ${turns.length - 6} 条` : "";
  if (!window.confirm(`将向 SOLO-QA 提交以下 ${turns.length} 个轮次，并上传对应轨迹文件：\n\n${preview}${extra}\n\n提交后会自动执行远端质检。确认继续吗？`)) return;
  state.soloQaBusy = true;
  state.soloQaLastMessage = `正在逐条提交 ${turns.length} 个轮次…`;
  renderSoloQaControls();
  try {
    const result = await requestSoloQaBridge(
      "SOLO_QA_SUBMIT",
      { turn_keys: turns.map((turn) => turn.key) },
    );
    const submitted = (result.results || []).filter((item) => item.outcome === "submitted").length;
    const recovered = (result.results || []).filter((item) => item.outcome === "recovered").length;
    const skipped = (result.results || []).filter((item) => item.outcome === "skipped").length;
    const failed = (result.results || []).find((item) => item.outcome === "failed");
    state.soloQaLastMessage = failed
      ? `已提交 ${submitted} 条、找回 ${recovered} 条；在 ${failed.turn_key} 停止：${failed.error}${result.remaining ? `，剩余 ${result.remaining} 条未处理` : ""}`
      : `提交完成：新增 ${submitted} 条${recovered ? `，找回已有 ${recovered} 条` : ""}${skipped ? `，跳过 ${skipped} 条` : ""}`;
    await loadCompletedTurns();
    showNotice(state.soloQaLastMessage);
  } catch (error) {
    state.soloQaLastMessage = error.message;
    showNotice(error.message);
    await loadCompletedTurns();
  } finally {
    state.soloQaBusy = false;
    renderSoloQaControls();
  }
}

async function copySoloQaHelperPath() {
  const path = state.health?.solo_qa?.helper_path || $("#solo-qa-helper-path")?.textContent || "";
  if (!path) return;
  try {
    await navigator.clipboard.writeText(path);
    showNotice("已复制提交助手目录");
  } catch {
    showNotice(`提交助手目录：${path}`);
  }
}

window.addEventListener("message", (event) => {
  if (event.source !== window || event.origin !== window.location.origin) return;
  const message = event.data;
  if (!message || message.source !== "solo-qa-helper") return;
  if (message.type === "SOLO_QA_BRIDGE_READY") {
    state.soloQaBridgeReady = true;
    state.soloQaBridgeVersion = String(message.payload?.version || "");
    renderSoloQaControls();
    return;
  }
  if (!["SOLO_QA_BRIDGE_RESULT", "SOLO_QA_BRIDGE_ERROR"].includes(message.type)) return;
  const pending = state.soloQaRequests.get(message.requestId);
  if (!pending) return;
  window.clearTimeout(pending.timer);
  state.soloQaRequests.delete(message.requestId);
  if (message.type === "SOLO_QA_BRIDGE_ERROR") {
    pending.reject(new Error(message.payload?.error || "提交助手执行失败"));
  } else {
    pending.resolve(message.payload || {});
  }
});

async function loadCompletedTurns() {
  try {
    state.completedTurns = await api("/api/exports/turns");
    state.exportLastLoadedAt = Date.now();
    const validKeys = new Set(state.completedTurns.map((turn) => turn.key));
    state.selectedExportTurns = new Set(
      [...state.selectedExportTurns].filter((key) => validKeys.has(key))
    );
    state.expandedExportPrompts = new Set(
      [...state.expandedExportPrompts].filter((key) => validKeys.has(key))
    );
    renderExportPage();
  } catch (error) {
    showNotice(error.message);
    $("#export-turn-list").innerHTML = '<tr><td colspan="11" class="table-empty">已完成轮次读取失败</td></tr>';
  }
}

function exportPreflightByKey() {
  return new Map((state.exportPreflight?.results || []).map((result) => [result.key, result]));
}

function renderExportPreflightSummary() {
  const panel = $("#export-preflight-panel");
  if (!panel) return;
  if (state.exportPreflightBusy) {
    panel.className = "export-preflight-panel checking";
    panel.innerHTML = "<strong>正在核对轨迹与标识…</strong><span>检查过程只读取本地数据库和 JSONL 文件。</span>";
    return;
  }
  const result = state.exportPreflight;
  if (!result) {
    panel.className = "export-preflight-panel";
    panel.innerHTML = "<strong>尚未执行提交前检查</strong><span>未勾选时检查列表中的全部完成轮次；勾选后只检查所选轮次。</span>";
    return;
  }
  const summary = result.summary || {};
  const failed = Number(summary.failed || 0);
  const warning = Number(summary.warning || 0);
  panel.className = `export-preflight-panel ${failed ? "failed" : (warning ? "warning" : "passed")}`;
  panel.innerHTML = `
    <strong>检查完成：通过 ${escapeHtml(summary.passed || 0)}，提醒 ${escapeHtml(warning)}，不通过 ${escapeHtml(failed)}</strong>
    <span>${escapeHtml(result.checked_at || "")} · 不通过项会阻止导出和 SOLO-QA 提交。</span>`;
}

function updateExportSelectionControls() {
  const selectedCount = state.selectedExportTurns.size;
  const visibleTurns = filteredCompletedTurns();
  const visibleKeys = new Set(visibleTurns.map((turn) => turn.key));
  const visibleSelectedCount = [...state.selectedExportTurns]
    .filter((key) => visibleKeys.has(key)).length;
  const selectedTurns = state.completedTurns.filter((turn) => state.selectedExportTurns.has(turn.key));
  const selectedReady = selectedTurns.filter((turn) => turn.export_ready).length;
  $("#export-selection-count").textContent = `已选 ${selectedCount} 项${selectedCount !== visibleSelectedCount ? `（当前筛选内 ${visibleSelectedCount}）` : ""}`;
  const download = $("#download-export");
  download.disabled = selectedCount === 0
    || selectedReady !== selectedCount
    || state.exportPreflightBusy
    || state.exportDeleteBusy;
  download.title = selectedReady !== selectedCount ? "所选轮次中有待补资料项" : "";
  const deleteButton = $("#delete-selected-export-turns");
  deleteButton.disabled = selectedCount === 0 || state.exportDeleteBusy || state.exportPreflightBusy;
  deleteButton.textContent = state.exportDeleteBusy ? "正在删除…" : `删除所选${selectedCount ? `（${selectedCount}）` : ""}`;
  const preflightButton = $("#preflight-export");
  const selectPassedButton = $("#select-preflight-passed");
  preflightButton.disabled = state.exportPreflightBusy || state.exportDeleteBusy || visibleTurns.length === 0;
  preflightButton.textContent = state.exportPreflightBusy
    ? "正在检查…"
    : (selectedCount ? `检查所选轮次（${selectedCount}）` : `检查筛选结果（${visibleTurns.length}）`);
  selectPassedButton.disabled = state.exportPreflightBusy || !state.exportPreflight;
  const selectAll = $("#select-all-export-turns");
  selectAll.checked = visibleTurns.length > 0 && visibleSelectedCount === visibleTurns.length;
  selectAll.indeterminate = visibleSelectedCount > 0 && visibleSelectedCount < visibleTurns.length;
  renderExportPreflightSummary();
  renderSoloQaControls();
}

async function runExportPreflight(turnKeys = null, { announce = true } = {}) {
  if (state.exportPreflightBusy) return null;
  state.exportPreflightBusy = true;
  updateExportSelectionControls();
  try {
    const result = await api("/api/exports/preflight", {
      method: "POST",
      body: JSON.stringify(turnKeys?.length ? { turn_keys: turnKeys } : {}),
    });
    state.exportPreflight = result;
    renderExportPage();
    if (announce) {
      const summary = result.summary || {};
      showNotice(`检查完成：通过 ${summary.passed || 0}，提醒 ${summary.warning || 0}，不通过 ${summary.failed || 0}`);
    }
    return result;
  } catch (error) {
    state.exportPreflight = null;
    showNotice(error.message);
    return null;
  } finally {
    state.exportPreflightBusy = false;
    updateExportSelectionControls();
  }
}

async function selectedTurnsPassPreflight(turnKeys) {
  const result = await runExportPreflight(turnKeys, { announce: false });
  if (!result) return false;
  const failed = (result.results || []).find((item) => !item.eligible);
  if (!failed) return true;
  const detail = (failed.blockers || []).join("；") || "检查未通过";
  showNotice(`${failed.project_number || failed.key} 第 ${failed.turn_number} 轮：${detail}`);
  return false;
}

function renderExportPage() {
  const list = $("#export-turn-list");
  if (!state.completedTurns.length) {
    state.exportPage = 1;
    renderTablePagination("exports", paginateItems([], state.exportPage));
    list.innerHTML = '<tr><td colspan="11" class="table-empty">还没有已完成轮次</td></tr>';
    updateExportSelectionControls();
    return;
  }
  const visibleTurns = filteredCompletedTurns();
  if (!visibleTurns.length) {
    state.exportPage = 1;
    renderTablePagination("exports", paginateItems([], state.exportPage));
    list.innerHTML = '<tr><td colspan="11" class="table-empty">没有符合筛选条件的完成轮次</td></tr>';
    updateExportSelectionControls();
    return;
  }
  const pagination = paginateItems(visibleTurns, state.exportPage);
  state.exportPage = pagination.page;
  renderTablePagination("exports", pagination);
  const preflightByKey = exportPreflightByKey();
  list.innerHTML = pagination.items.map((turn) => {
    const exportIssues = Array.isArray(turn.export_issues) ? turn.export_issues : [];
    const exportIssueLabels = exportIssues.map((issue) =>
      String(issue).replace(/^缺少\s*/, "").replace(/^评分缺少\s*/, "评分：")
    );
    const exportIssueDetails = !turn.export_ready && exportIssueLabels.length
      ? `<span class="export-issues-inline">：${escapeHtml(exportIssueLabels.join("；"))}</span>`
      : "";
    const soloQa = turn.solo_qa || {};
    const soloQaState = exportSoloQaState(turn);
    const [soloQaLabel, soloQaTone] = soloQaStateInfo[soloQaState] || [soloQaState, "not_submitted"];
    const soloQaDetail = soloQa.error || soloQa.qc_summary || (turn.solo_qa_issues || []).join("；");
    const soloQaLink = soloQa.detail_url
      ? `<a href="${escapeHtml(soloQa.detail_url)}" target="_blank" rel="noopener noreferrer">#${escapeHtml(soloQa.remote_id)} 查看</a>`
      : "";
    const preflight = preflightByKey.get(turn.key);
    const preflightIssues = preflight
      ? [...(preflight.blockers || []), ...(preflight.warnings || [])]
      : [];
    const preflightTone = preflight?.status || "";
    const preflightLabel = preflightTone === "passed"
      ? "检查通过"
      : (preflightTone === "warning"
        ? `检查通过（${preflight.warnings?.length || 0} 项提醒）`
        : (preflightTone === "failed"
          ? `检查不通过（${preflight.blockers?.length || 0} 项）`
          : ""));
    const readinessContent = preflight
      ? `<span class="preflight-result ${escapeHtml(preflightTone)}">${escapeHtml(preflightLabel)}</span>${preflightIssues.length ? `<span class="export-issues-inline">：${escapeHtml(preflightIssues.join("；"))}</span>` : ""}`
      : `<span class="export-readiness ${turn.export_ready ? "ready" : "blocked"}">${turn.export_ready ? "可导出 · 尚未深度检查" : `待补资料（${escapeHtml(exportIssues.length)}）`}</span>${exportIssueDetails}`;
    const promptExpanded = state.expandedExportPrompts.has(turn.key);
    const promptRowId = `export-prompt-${turn.run_id}-${turn.turn_number}`;
    const evaluationEditor = promptExpanded ? exportEvaluationEditorHtml(turn) : "";
    return `
    <tr class="export-turn-row${promptExpanded ? " prompt-expanded" : ""}">
      <td data-label="详情"><button class="export-prompt-toggle" type="button" data-export-prompt-key="${escapeHtml(turn.key)}" aria-expanded="${promptExpanded}" aria-controls="${escapeHtml(promptRowId)}" title="${promptExpanded ? "收起题面与评分" : "展开题面与评分"}"><span aria-hidden="true">›</span><span class="sr-only">${promptExpanded ? "收起" : "展开"} ${escapeHtml(turn.repo_name)} 第 ${escapeHtml(turn.turn_number)} 轮题面与评分</span></button></td>
      <td data-label="选择"><input class="export-turn-checkbox" type="checkbox" data-export-key="${escapeHtml(turn.key)}" aria-label="选择 ${escapeHtml(turn.repo_name)} 第 ${escapeHtml(turn.turn_number)} 轮" ${state.selectedExportTurns.has(turn.key) ? "checked" : ""} /></td>
      <td data-label="编号"><span class="number-badge">${escapeHtml(turn.project_number || "—")}</span></td>
      <td data-label="项目 / 仓库"><button class="record-name export-run-link" type="button" data-export-run-id="${escapeHtml(turn.run_id)}">${escapeHtml(turn.repo_name)}</button><small>${escapeHtml(turn.run_id)}</small></td>
      <td data-label="轮次"><span class="turn-badge">第 ${escapeHtml(turn.turn_number)} 轮</span></td>
      <td data-label="任务类型"><span class="task-type-badge">${escapeHtml(turn.task_type || "未记录")}</span></td>
      <td data-label="难度">${escapeHtml(turn.task_difficulty || "未记录")}</td>
      <td data-label="资料 / 提交前检查" title="${escapeHtml(preflightIssues.length ? preflightIssues.join("；") : exportIssues.join("；"))}">${readinessContent}</td>
      <td data-label="SOLO-QA" class="solo-qa-cell" title="${escapeHtml(soloQaDetail)}"><span class="solo-qa-state ${escapeHtml(soloQaTone)}">${escapeHtml(soloQaLabel)}</span>${soloQaLink ? `<small>${soloQaLink}</small>` : ""}${soloQaDetail ? `<small>${escapeHtml(soloQaDetail)}</small>` : ""}</td>
      <td data-label="完成时间" class="time-column">${renderTableTimestamp(turn.completed_at, "completed")}</td>
      <td data-label="操作"><button class="record-delete export-turn-delete" type="button" data-export-delete-key="${escapeHtml(turn.key)}" ${state.exportDeleteBusy ? "disabled" : ""}>删除</button></td>
    </tr>
    ${promptExpanded ? `<tr class="export-prompt-row" id="${escapeHtml(promptRowId)}"><td colspan="11"><div class="export-prompt-content"><strong>题面</strong><p>${escapeHtml(turn.prompt || "未记录题面")}</p></div>${evaluationEditor}</td></tr>` : ""}`;
  }).join("");
  updateExportSelectionControls();
}

const exportEvaluationDimensions = [
  ["delivery", "交付完整性"],
  ["instruction_following", "指令遵循"],
  ["planning", "任务规划"],
  ["reasoning", "推理能力"],
  ["execution", "执行能力"],
];

function exportEvaluationDraft(turn) {
  const saved = state.exportEvaluationDrafts.get(turn.key);
  if (saved) return saved;
  const draft = {};
  exportEvaluationDimensions.forEach(([key]) => {
    draft[key] = {
      score: Number(turn.evaluation?.[key]?.score || 0),
      description: String(turn.evaluation?.[key]?.description || ""),
    };
  });
  return draft;
}

function exportEvaluationEditorHtml(turn) {
  if (!turn.evaluation) {
    return '<div class="export-evaluation-empty">该轮尚无可编辑评分。</div>';
  }
  const draft = exportEvaluationDraft(turn);
  const busy = state.exportEvaluationBusy.has(turn.key);
  const status = turn.evaluation_overridden
    ? `<span class="manual">已人工修改${turn.evaluation_override_updated_at ? ` · ${escapeHtml(turn.evaluation_override_updated_at)}` : ""}</span>`
    : "<span>当前为自动评分</span>";
  return `<section class="export-evaluation-editor" data-evaluation-editor="${escapeHtml(turn.key)}">
    <div class="export-evaluation-heading"><div><strong>五维评分与描述</strong>${status}</div><small>保存后，Excel 导出和 SOLO-QA 提交均使用这里的内容。</small></div>
    <div class="export-evaluation-grid">${exportEvaluationDimensions.map(([key, label]) => {
      const item = draft[key] || {};
      return `<label class="export-evaluation-item"><span>${escapeHtml(label)}</span><select data-evaluation-key="${escapeHtml(turn.key)}" data-evaluation-dimension="${key}" data-evaluation-field="score" aria-label="${escapeHtml(label)}分数">${[1, 2, 3, 4, 5].map((score) => `<option value="${score}" ${Number(item.score) === score ? "selected" : ""}>${score} 分</option>`).join("")}</select><textarea rows="6" maxlength="2000" data-evaluation-key="${escapeHtml(turn.key)}" data-evaluation-dimension="${key}" data-evaluation-field="description" aria-label="${escapeHtml(label)}描述">${escapeHtml(item.description || "")}</textarea></label>`;
    }).join("")}</div>
    <div class="export-evaluation-actions"><button class="primary-button" type="button" data-save-evaluation="${escapeHtml(turn.key)}" ${busy ? "disabled" : ""}>${busy ? "保存中…" : "保存评分修改"}</button>${turn.evaluation_overridden ? `<button class="secondary-button" type="button" data-reset-evaluation="${escapeHtml(turn.key)}" ${busy ? "disabled" : ""}>恢复自动评分</button>` : ""}<span>原始自动评分不会被覆盖。</span></div>
  </section>`;
}

function updateEvaluationDraft(input) {
  const key = input.dataset.evaluationKey;
  const dimension = input.dataset.evaluationDimension;
  const field = input.dataset.evaluationField;
  const turn = state.completedTurns.find((item) => item.key === key);
  if (!key || !dimension || !field || !turn) return;
  const draft = exportEvaluationDraft(turn);
  draft[dimension] = {
    ...(draft[dimension] || {}),
    [field]: field === "score" ? Number(input.value) : input.value,
  };
  state.exportEvaluationDrafts.set(key, draft);
}

async function saveExportEvaluation(turnKey, reset = false) {
  if (state.exportEvaluationBusy.has(turnKey)) return;
  const turn = state.completedTurns.find((item) => item.key === turnKey);
  if (!turn) return;
  state.exportEvaluationBusy.add(turnKey);
  renderExportPage();
  try {
    await api("/api/exports/turns/evaluation", {
      method: "POST",
      body: JSON.stringify({
        turn_key: turnKey,
        reset,
        evaluation: reset ? undefined : exportEvaluationDraft(turn),
      }),
    });
    state.exportEvaluationDrafts.delete(turnKey);
    state.exportPreflight = null;
    await loadCompletedTurns();
    showNotice(reset ? "已恢复自动评分；后续导出和提交将使用自动版本" : "评分修改已保存；后续导出和提交将使用人工版本");
  } catch (error) {
    showNotice(error.message);
  } finally {
    state.exportEvaluationBusy.delete(turnKey);
    renderExportPage();
  }
}

function localDateValue(date = new Date()) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function setActiveModuleTab(activeModule) {
  const tabs = {
    runs: $("#module-tab-runs"),
    exports: $("#open-export-page"),
    analytics: $("#open-analytics-page"),
  };
  Object.entries(tabs).forEach(([module, tab]) => {
    const active = module === activeModule;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
}

const analyticsTaskTypes = [
  ["0-1 代码生成", "baseline"],
  ["Feature 迭代", "feature"],
  ["Bug 修复", "bugfix"],
  ["其他", "other"],
];

function renderHourlyChart(hours, peakCount) {
  const chart = $("#hourly-output-chart");
  hideHourlyChartTooltip();
  const chartType = state.analyticsChartType;
  const maxCount = Math.max(1, ...hours.map((hour) => Number(hour.total || 0)));
  [
    [$("#analytics-chart-bar"), "bar"],
    [$("#analytics-chart-line"), "line"],
  ].forEach(([button, type]) => {
    const active = chartType === type;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });

  if (chartType === "line") {
    const width = 930;
    const plotTop = 20;
    const plotBottom = 188;
    const xFor = (index) => 20 + (index * 890 / 23);
    const yFor = (total) => plotBottom - (Number(total || 0) / maxCount) * (plotBottom - plotTop);
    const points = hours.map((hour, index) => `${xFor(index)},${yFor(hour.total)}`).join(" ");
    const areaPoints = `20,${plotBottom} ${points} 910,${plotBottom}`;
    const gridLines = [0, 0.25, 0.5, 0.75, 1].map((ratio) => {
      const y = plotBottom - ratio * (plotBottom - plotTop);
      return `<line x1="20" y1="${y}" x2="910" y2="${y}" class="analytics-line-grid"></line>`;
    }).join("");
    const labels = hours.map((hour, index) =>
      `<text x="${xFor(index)}" y="218" text-anchor="middle">${String(hour.hour).padStart(2, "0")}</text>`
    ).join("");
    const nodes = hours.map((hour, index) => {
      const total = Number(hour.total || 0);
      const isPeak = peakCount > 0 && total === peakCount;
      return `<circle class="analytics-line-node${isPeak ? " peak" : ""}" cx="${xFor(index)}" cy="${yFor(total)}" r="5" tabindex="0" data-analytics-hour="${index}" aria-label="${escapeHtml(hour.label)}，完成 ${total} 轮"><title>${escapeHtml(hour.label)}：${total} 轮</title></circle>`;
    }).join("");
    chart.className = "hourly-chart analytics-line-chart";
    chart.innerHTML = `<svg viewBox="0 0 ${width} 228" role="img" aria-label="每小时完成轮次折线图">
      ${gridLines}
      <polygon class="analytics-line-area" points="${areaPoints}"></polygon>
      <polyline class="analytics-line-path" points="${points}"></polyline>
      ${nodes}
      <g class="analytics-line-labels">${labels}</g>
    </svg>`;
    return;
  }

  chart.className = "hourly-chart analytics-bar-chart";
  chart.innerHTML = hours.map((hour, index) => {
    const total = Number(hour.total || 0);
    const segments = analyticsTaskTypes.map(([taskType, className]) => {
      const count = Number(hour.by_task_type?.[taskType] || 0);
      if (!count) return "";
      const height = (count / maxCount) * 100;
      return `<i class="hourly-bar-segment ${className}" style="height:${height}%"></i>`;
    }).join("");
    const isPeak = peakCount > 0 && total === peakCount;
    return `<div class="hourly-bar${isPeak ? " peak" : ""}" tabindex="0" data-analytics-hour="${index}" aria-label="${escapeHtml(hour.label)}，完成 ${total} 轮">
      <b>${total || ""}</b>
      <div class="hourly-bar-track"><div class="hourly-bar-stack">${segments}</div></div>
      <span>${String(hour.hour).padStart(2, "0")}</span>
    </div>`;
  }).join("");
}

function showHourlyChartTooltip(hourIndex, target, pointerEvent = null) {
  const hour = state.hourlyAnalytics?.hours?.[Number(hourIndex)];
  const tooltip = $("#hourly-chart-tooltip");
  if (!hour || !tooltip || !target) return;
  const total = Number(hour.total || 0);
  tooltip.innerHTML = `<div class="analytics-tooltip-heading"><strong>${escapeHtml(hour.label)}</strong><b>${total} 轮</b></div>
    <div class="analytics-tooltip-breakdown">${analyticsTaskTypes.map(([taskType, className]) =>
      `<span><i class="${className}"></i>${escapeHtml(taskType)}<b>${Number(hour.by_task_type?.[taskType] || 0)}</b></span>`
    ).join("")}</div>`;
  tooltip.classList.remove("hidden");
  const cardRect = $(".analytics-chart-card").getBoundingClientRect();
  const targetRect = target.getBoundingClientRect();
  const pointerX = pointerEvent?.clientX || targetRect.left + targetRect.width / 2;
  const pointerY = pointerEvent?.clientY || targetRect.top;
  const left = Math.min(Math.max(pointerX - cardRect.left + 12, 10), cardRect.width - tooltip.offsetWidth - 10);
  const top = Math.max(pointerY - cardRect.top - tooltip.offsetHeight - 10, 70);
  tooltip.style.left = `${left}px`;
  tooltip.style.top = `${top}px`;
}

function hideHourlyChartTooltip() {
  $("#hourly-chart-tooltip")?.classList.add("hidden");
}

function setAnalyticsChartType(chartType) {
  if (!['bar', 'line'].includes(chartType) || state.analyticsChartType === chartType) return;
  state.analyticsChartType = chartType;
  hideHourlyChartTooltip();
  renderHourlyAnalytics();
}

function renderHourlyAnalytics() {
  const analytics = state.hourlyAnalytics;
  if (!analytics) return;
  const summary = analytics.summary || {};
  const hours = Array.isArray(analytics.hours) ? analytics.hours : [];
  const peakCount = Number(summary.peak_count || 0);

  $("#analytics-total").textContent = String(summary.completed_turns || 0);
  $("#analytics-peak").textContent = `${peakCount} 轮`;
  $("#analytics-active-hours").textContent = `${summary.active_hours || 0} 小时`;
  $("#analytics-average").textContent = Number(summary.average_per_hour || 0).toFixed(2);
  $("#analytics-peak-hours").textContent = (summary.peak_hours || []).join("、") || "当天暂无产出";
  $("#analytics-definition").textContent = `${analytics.definition || "按完成轮次统计。"} 时区：${analytics.timezone || "本机"}`;
  $("#analytics-chart-caption").textContent = `${analytics.date} · 共完成 ${summary.completed_turns || 0} 轮`;
  analyticsTaskTypes.forEach(([taskType, className]) => {
    const total = hours.reduce(
      (sum, hour) => sum + Number(hour.by_task_type?.[taskType] || 0),
      0
    );
    $(`#analytics-type-${className}-count`).textContent = `${total} 条`;
  });

  renderHourlyChart(hours, peakCount);

  $("#hourly-output-list").innerHTML = hours.map((hour) => {
    const total = Number(hour.total || 0);
    const isPeak = peakCount > 0 && total === peakCount;
    return `<tr${isPeak ? ' class="peak-hour"' : ""}>
      <td data-label="时段"><b>${escapeHtml(hour.label)}</b></td>
      <td data-label="完成轮次"><strong>${total}</strong></td>
      <td data-label="0-1 代码生成">${Number(hour.by_task_type?.["0-1 代码生成"] || 0)}</td>
      <td data-label="Feature 迭代">${Number(hour.by_task_type?.["Feature 迭代"] || 0)}</td>
      <td data-label="Bug 修复">${Number(hour.by_task_type?.["Bug 修复"] || 0)}</td>
      <td data-label="其他">${Number(hour.by_task_type?.["其他"] || 0)}</td>
    </tr>`;
  }).join("");
}

async function loadHourlyAnalytics(dateValue = state.analyticsDate || localDateValue(), showLoading = true) {
  if (state.analyticsBusy) return;
  state.analyticsBusy = true;
  state.analyticsDate = dateValue;
  $("#analytics-date").value = dateValue;
  if (showLoading && !state.hourlyAnalytics) {
    $("#hourly-output-chart").innerHTML = '<div class="analytics-loading">正在读取每小时产出…</div>';
    $("#hourly-output-list").innerHTML = '<tr><td colspan="6" class="table-empty">正在读取统计数据…</td></tr>';
  }
  try {
    state.hourlyAnalytics = await api(`/api/analytics/hourly-output?date=${encodeURIComponent(dateValue)}`);
    state.analyticsLastLoadedAt = Date.now();
    renderHourlyAnalytics();
  } catch (error) {
    if (showLoading) showNotice(error.message);
  } finally {
    state.analyticsBusy = false;
  }
}

function setPageHeader(title, description, showNewButton = true) {
  pageTitle.textContent = title;
  pageDescription.textContent = description;
  newRunButton.classList.toggle("hidden", !showNewButton);
}

function showListPage() {
  state.selectedId = null;
  state.detail = null;
  detailView.classList.add("hidden");
  exportView.classList.add("hidden");
  analyticsView.classList.add("hidden");
  recordsView.classList.remove("hidden");
  setActiveModuleTab("runs");
  setPageHeader("运行记录", "查看任务状态、当前轮次和使用的技术信息。", true);
  renderRunList();
  $(".records-table-wrap").scrollLeft = 0;
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function showDetailPage(id) {
  if (!state.runs.some((run) => run.id === id)) {
    showNotice("没有找到这条运行记录");
    navigateTo("#runs");
    return;
  }
  state.selectedId = id;
  recordsView.classList.add("hidden");
  exportView.classList.add("hidden");
  analyticsView.classList.add("hidden");
  detailView.classList.remove("hidden");
  setActiveModuleTab("runs");
  setPageHeader("任务详情", "查看完整题面、会话标识、逐轮结果和运行轨迹。", true);
  detailView.innerHTML = '<div class="detail-loading">正在加载任务详情…</div>';
  await loadDetail();
  await loadAutomaticIterationStatus(id);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function showExportPage() {
  state.selectedId = null;
  state.detail = null;
  recordsView.classList.add("hidden");
  detailView.classList.add("hidden");
  analyticsView.classList.add("hidden");
  exportView.classList.remove("hidden");
  setActiveModuleTab("exports");
  setPageHeader("导出与提交", "选择一个或多个已完成轮次，导出 Excel 或提交到 SOLO-QA。", true);
  $("#export-turn-list").innerHTML = '<tr><td colspan="11" class="table-empty">正在读取已完成轮次…</td></tr>';
  await loadCompletedTurns();
  pingSoloQaBridge();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function showAnalyticsPage() {
  state.selectedId = null;
  state.detail = null;
  recordsView.classList.add("hidden");
  detailView.classList.add("hidden");
  exportView.classList.add("hidden");
  analyticsView.classList.remove("hidden");
  setActiveModuleTab("analytics");
  setPageHeader("数据分析", "按自然小时查看已完成轮次的产出节奏和任务类型分布。", true);
  await loadHourlyAnalytics(state.analyticsDate || localDateValue());
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function navigateTo(hash) {
  if (window.location.hash === hash) {
    applyRoute();
    return;
  }
  window.location.hash = hash;
}

async function updateImportBaselinePreview() {
  const preview = $("#import-baseline-preview");
  const projectDirectory = $("#import-project-directory").value.trim() || "zzzz";
  const projectNumbers = $("#import-project-numbers").value.trim();
  preview.textContent = "正在计算导入编号…";
  try {
    const result = await api(`/api/runs/import-baseline/preview?project_directory=${encodeURIComponent(projectDirectory)}`);
    preview.textContent = projectNumbers
      ? `将从 ${result.planned_path.replace(/\/[^/]+$/, "")} 查找：${projectNumbers}。每个编号必须唯一对应一个项目目录。`
      : `请输入 ${result.number_range} 内的编号；当前下一个空闲导入编号是 ${result.project_number}。`;
  } catch (error) {
    preview.textContent = error.message;
  }
}

function closeImportBaselineDialog() {
  const dialog = $("#import-baseline-dialog");
  if (dialog.open) dialog.close();
}

async function openImportBaselineDialog() {
  const dialog = $("#import-baseline-dialog");
  const form = $("#import-baseline-form");
  form.reset();
  const projectDirectory = $("#project-directory").value.trim()
    || state.health?.default_project_directory
    || "zzzz";
  $("#import-project-directory").value = projectDirectory;
  dialog.showModal();
  await updateImportBaselinePreview();
}

async function submitImportedBaseline(event) {
  event.preventDefault();
  const button = $("#submit-import-baseline");
  button.disabled = true;
  button.textContent = "正在检查 Git 与 Compose…";
  try {
    const result = await api("/api/runs/import-baselines-by-number", {
      method: "POST",
      body: JSON.stringify({
        project_numbers: $("#import-project-numbers").value,
        project_directory: $("#import-project-directory").value,
      }),
    });
    await loadRuns();
    const succeeded = [...(result.imported || []), ...(result.existing || [])];
    const failed = result.failed || [];
    if (!failed.length) {
      closeImportBaselineDialog();
      if (succeeded.length === 1) navigateTo(`#run/${succeeded[0].id}`);
      showNotice(`已处理 ${succeeded.length} 个基线，其中新导入 ${result.imported?.length || 0} 个`);
    } else {
      const failureText = failed.map((item) => `${item.project_number}：${item.error}`).join("；");
      $("#import-baseline-preview").textContent = `已导入 ${result.imported?.length || 0} 个；${failureText}`;
      showNotice(failureText);
    }
  } catch (error) {
    showNotice(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "校验并导入";
  }
}

async function applyRoute() {
  const hash = window.location.hash || "#runs";
  if (!window.location.hash) window.history.replaceState(null, "", "#runs");
  if (hash === "#new") {
    window.history.replaceState(null, "", "#runs");
    showListPage();
    return;
  }
  if (hash === "#exports") {
    await showExportPage();
    return;
  }
  if (hash === "#analytics") {
    await showAnalyticsPage();
    return;
  }
  const detailMatch = hash.match(/^#run\/([a-f0-9]{12})$/);
  if (detailMatch) {
    await showDetailPage(detailMatch[1]);
    return;
  }
  showListPage();
}

async function startAutomaticRun() {
  const button = newRunButton;
  button.disabled = true;
  button.textContent = "正在创建任务记录…";
  try {
    const created = await api("/api/runs/auto", {
      method: "POST",
      body: JSON.stringify({
        project_directory: $("#project-directory").value,
      }),
    });
    await loadRuns();
    navigateTo("#runs");
    showNotice(`${created.project_number || "新任务"} 已创建，题面正在后台生成`);
  } catch (error) {
    showNotice(error.message);
  } finally {
    renderNewRunButtonState();
  }
}

async function submitModel(event) {
  event.preventDefault();
  const input = $("#global-model");
  const button = $("#model-submit");
  const model = input.value.trim();
  button.disabled = true;
  button.textContent = "切换中";
  try {
    const result = await api("/api/settings/model", {
      method: "POST",
      body: JSON.stringify({ model }),
    });
    state.modelInputDirty = false;
    if (state.health) state.health.model = result.model;
    renderHealth();
    await loadRuns();
    if (state.selectedId) await loadDetail();
    showNotice(`全局模型已切换为 ${result.model}，新任务和尚未启动容器的排队任务将使用它`);
  } catch (error) {
    showNotice(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "应用";
  }
}

async function toggleAutoRefill() {
  const button = $("#auto-refill-toggle");
  const wasScheduled = Boolean(state.health?.auto_refill?.enable_at);
  const enabled = !Boolean(state.health?.auto_refill?.enabled);
  button.disabled = true;
  button.textContent = enabled ? "正在开启…" : "正在关闭…";
  try {
    const result = await api("/api/settings/auto-refill", {
      method: "POST",
      body: JSON.stringify({
        enabled,
        project_directory: $("#project-directory").value,
      }),
    });
    if (state.health) state.health.auto_refill = result;
    renderHealth();
    showNotice(enabled
      ? (wasScheduled ? "自动补题已立即开启，原预约开始时间已取消" : "自动补题已开启，将自动补满空闲并行槽")
      : "自动补题已关闭，已启动任务继续运行");
  } catch (error) {
    showNotice(error.message);
    renderHealth();
  }
}

async function setAutoRefillStartSchedule() {
  const input = $("#auto-refill-start-hours");
  const button = $("#auto-refill-start-schedule");
  const hours = Number(input?.value);
  if (!Number.isFinite(hours) || hours < 0.5 || hours > 168) {
    showNotice("自动开始时长必须是 0.5 至 168 小时");
    input?.focus();
    return;
  }
  button.disabled = true;
  button.textContent = "正在预约…";
  try {
    const result = await api("/api/settings/auto-refill", {
      method: "POST",
      body: JSON.stringify({
        enabled: false,
        project_directory: $("#project-directory").value,
        enable_after_hours: hours,
      }),
    });
    if (state.health) state.health.auto_refill = result;
    renderHealth();
    showNotice(`已预约 ${hours} 小时后开始自动补题；到点前不会创建新任务`);
  } catch (error) {
    showNotice(error.message);
    renderHealth();
  }
}

async function clearAutoRefillStartSchedule() {
  const button = $("#auto-refill-start-clear");
  button.disabled = true;
  button.textContent = "正在取消…";
  try {
    const result = await api("/api/settings/auto-refill", {
      method: "POST",
      body: JSON.stringify({
        enabled: false,
        project_directory: $("#project-directory").value,
        enable_after_hours: null,
      }),
    });
    if (state.health) state.health.auto_refill = result;
    renderHealth();
    showNotice("自动补题的预约开始时间已取消");
  } catch (error) {
    showNotice(error.message);
    renderHealth();
  }
}

async function setAutoRefillSchedule() {
  const input = $("#auto-refill-hours");
  const button = $("#auto-refill-schedule");
  const hours = Number(input?.value);
  if (!Number.isFinite(hours) || hours < 0.5 || hours > 168) {
    showNotice("自动关闭时长必须是 0.5 至 168 小时");
    input?.focus();
    return;
  }
  button.disabled = true;
  button.textContent = "正在设置…";
  try {
    const result = await api("/api/settings/auto-refill", {
      method: "POST",
      body: JSON.stringify({
        enabled: true,
        project_directory: $("#project-directory").value,
        disable_after_hours: hours,
      }),
    });
    if (state.health) state.health.auto_refill = result;
    renderHealth();
    showNotice(`自动补题已开启，将在 ${hours} 小时后自动关闭；已启动任务不会被终止`);
  } catch (error) {
    showNotice(error.message);
    renderHealth();
  }
}

async function clearAutoRefillSchedule() {
  const button = $("#auto-refill-clear");
  button.disabled = true;
  button.textContent = "正在取消…";
  try {
    const result = await api("/api/settings/auto-refill", {
      method: "POST",
      body: JSON.stringify({
        enabled: true,
        project_directory: $("#project-directory").value,
        disable_after_hours: null,
      }),
    });
    if (state.health) state.health.auto_refill = result;
    renderHealth();
    showNotice("自动关闭定时已取消，自动补题将持续开启");
  } catch (error) {
    showNotice(error.message);
    renderHealth();
  }
}

function setAutomaticIterationJob(sourceRunId, job) {
  const previous = state.iterationJobs[sourceRunId];
  state.iterationJobs[sourceRunId] = job;
  if (job?.task_type) state.iterationTaskTypes[sourceRunId] = job.task_type;
  const changed = !previous
    || previous.status !== job?.status
    || previous.error !== job?.error
    || previous.last_poll_error !== job?.last_poll_error
    || previous.created_run_id !== job?.created_run_id;
  if (changed && state.selectedId === sourceRunId && state.detail?.id === sourceRunId) {
    renderDetail();
  }
}

async function completeAutomaticIteration(sourceRunId, job) {
  await loadRuns();
  if (state.selectedId === sourceRunId) navigateTo(`#run/${job.created_run_id}`);
  showNotice(`${job.task_type || "迭代"}需求已生成，独立会话已创建并开始执行`);
}

function watchAutomaticIteration(sourceRunId, taskType) {
  if (state.iterationPollers[sourceRunId]) return state.iterationPollers[sourceRunId];
  const poller = (async () => {
    let job = state.iterationJobs[sourceRunId];
    while (["starting", "generating"].includes(job?.status)) {
      await new Promise((resolve) => window.setTimeout(resolve, 1500));
      try {
        job = await api(`/api/runs/${sourceRunId}/auto-iteration-status?task_type=${encodeURIComponent(taskType)}`);
        setAutomaticIterationJob(sourceRunId, job);
      } catch (error) {
        job = { ...job, status: "generating", last_poll_error: error.message };
        setAutomaticIterationJob(sourceRunId, job);
      }
    }
    if (job?.status === "complete" && job.created_run_id) {
      await completeAutomaticIteration(sourceRunId, job);
    } else if (job?.status === "failed") {
      showNotice(job.error || "后台出题失败，可以再次点击重试");
    }
  })().finally(() => {
    delete state.iterationPollers[sourceRunId];
  });
  state.iterationPollers[sourceRunId] = poller;
  return poller;
}

async function loadAutomaticIterationStatus(sourceRunId, taskType = null) {
  try {
    let job;
    if (taskType) {
      job = await api(`/api/runs/${sourceRunId}/auto-iteration-status?task_type=${encodeURIComponent(taskType)}`);
    } else {
      const jobs = await Promise.all(
        ["Feature 迭代", "0-1 代码生成", "Bug 修复"].map((type) =>
          api(`/api/runs/${sourceRunId}/auto-iteration-status?task_type=${encodeURIComponent(type)}`)
        )
      );
      job = jobs.find((item) => item.status === "generating")
        || jobs.find((item) => item.status === "failed")
        || jobs.find((item) => item.status === "complete" && item.task_type === state.iterationTaskTypes[sourceRunId])
        || jobs.find((item) => item.status === "complete")
        || jobs[0];
    }
    if (job.status === "idle") {
      delete state.iterationJobs[sourceRunId];
      if (state.selectedId === sourceRunId && state.detail?.id === sourceRunId) renderDetail();
      return;
    }
    setAutomaticIterationJob(sourceRunId, job);
    if (job.status === "generating") watchAutomaticIteration(sourceRunId, job.task_type);
  } catch (error) {
    showNotice(`迭代任务状态读取失败：${error.message}`);
  }
}

async function startAutomaticIteration() {
  const sourceRunId = state.selectedId;
  const taskType = $("#iteration-task-type")?.value || "Feature 迭代";
  setAutomaticIterationJob(sourceRunId, {
    status: "starting",
    source_run_id: sourceRunId,
    task_type: taskType,
  });
  try {
    const job = await api(`/api/runs/${sourceRunId}/auto-iteration`, {
      method: "POST",
      body: JSON.stringify({ task_type: taskType }),
    });
    setAutomaticIterationJob(sourceRunId, job);
    if (job.status === "complete" && job.created_run_id) {
      await completeAutomaticIteration(sourceRunId, job);
    } else if (job.status === "generating") {
      watchAutomaticIteration(sourceRunId, taskType);
    } else {
      throw new Error(job.error || "后台出题状态已丢失，请再次点击重试");
    }
  } catch (error) {
    showNotice(error.message);
    setAutomaticIterationJob(sourceRunId, {
      status: "failed",
      source_run_id: sourceRunId,
      task_type: taskType,
      error: error.message,
    });
  }
}

async function cancelAutomaticIteration() {
  const sourceRunId = state.selectedId;
  if (!sourceRunId || !window.confirm("确认取消正在生成的迭代需求？不会修改当前项目代码。")) return;
  try {
    const job = await api(`/api/runs/${sourceRunId}/auto-iteration-cancel`, {
      method: "POST",
      body: "{}",
    });
    setAutomaticIterationJob(sourceRunId, job);
    showNotice("迭代题面生成已取消，当前项目代码未改动");
  } catch (error) {
    showNotice(error.message);
  }
}

async function stopSelectedRun() {
  if (!window.confirm("确认终止当前 Claude 会话？代码和会话记录会保留。")) return;
  try {
    await api(`/api/runs/${state.selectedId}/stop`, { method: "POST", body: "{}" });
    await loadDetail();
    await loadRuns();
  } catch (error) {
    showNotice(error.message);
  }
}

async function retryAutomaticGeneration() {
  const runId = state.selectedId;
  const button = $("#retry-generation");
  if (button) {
    button.disabled = true;
    button.textContent = "正在重新进入出题队列…";
  }
  try {
    await api(`/api/runs/${runId}/retry-generation`, {method: "POST", body: "{}"});
    await Promise.all([loadDetail(), loadRuns()]);
    showNotice("已沿用原项目编号重新生成题面");
  } catch (error) {
    showNotice(error.message);
    if (button) {
      button.disabled = false;
      button.textContent = "沿用原编号重新生成题面";
    }
  }
}

async function retryControlStage() {
  const runId = state.selectedId;
  try {
    await api(`/api/runs/${runId}/retry-stage`, { method: "POST", body: "{}" });
    await loadDetail();
    await loadRuns();
    showNotice("当前控制阶段已重新进入队列");
  } catch (error) {
    showNotice(error.message);
  }
}

async function retryFirstTurn() {
  const button = $("#retry-first");
  if (button) {
    button.disabled = true;
    button.textContent = "正在创建新会话…";
  }
  try {
    const created = await api(`/api/runs/${state.selectedId}/retry-first`, { method: "POST", body: "{}" });
    await loadRuns();
    navigateTo(`#run/${created.id}`);
    showNotice("已从初始快照创建全新的容器和会话");
  } catch (error) {
    showNotice(error.message);
    if (button) {
      button.disabled = false;
      button.textContent = "用新会话重跑";
    }
  }
}

async function deleteRun(runId) {
  const run = state.runs.find((item) => item.id === runId);
  if (!run) return;
  const message = `确认从列表隐藏 ${run.project_number || "—"} ${run.repo_name} 的控制台运行记录？\n\n数据采用软删除，可通过恢复接口找回；本地项目文件和 GitHub 仓库不会改动。`;
  if (!window.confirm(message)) return;
  try {
    await api(`/api/runs/${runId}`, { method: "DELETE" });
    state.selectedExportTurns = new Set(
      [...state.selectedExportTurns].filter((key) => !key.startsWith(`${runId}:`))
    );
    await loadRuns();
    showNotice(`已隐藏 ${run.repo_name} 的控制台记录；数据、本地项目和 GitHub 仓库仍保留`);
  } catch (error) {
    showNotice(error.message);
  }
}

async function deleteExportTurns(turnKeys) {
  const uniqueKeys = [...new Set(turnKeys)].filter((key) =>
    state.completedTurns.some((turn) => turn.key === key)
  );
  if (!uniqueKeys.length || state.exportDeleteBusy) return;
  const turns = uniqueKeys
    .map((key) => state.completedTurns.find((turn) => turn.key === key))
    .filter(Boolean);
  const preview = turns.slice(0, 6).map((turn) =>
    `${turn.project_number || "—"} ${turn.repo_name} · 第 ${turn.turn_number} 轮`
  ).join("\n");
  const extra = turns.length > 6 ? `\n另有 ${turns.length - 6} 条` : "";
  const confirmed = window.confirm(
    `确认从导出列表隐藏以下 ${turns.length} 个完成轮次？\n\n${preview}${extra}\n\n` +
    "只影响导出列表；运行记录、项目代码、GitHub 仓库、轨迹文件和 SOLO-QA 远端提交都会保留。"
  );
  if (!confirmed) return;
  state.exportDeleteBusy = true;
  updateExportSelectionControls();
  try {
    const result = await api("/api/exports/turns/delete", {
      method: "POST",
      body: JSON.stringify({ turn_keys: uniqueKeys }),
    });
    uniqueKeys.forEach((key) => state.selectedExportTurns.delete(key));
    state.exportPreflight = null;
    await loadCompletedTurns();
    showNotice(`已从导出列表隐藏 ${result.changed || 0} 个轮次；代码、轨迹和远端提交均未删除`);
  } catch (error) {
    showNotice(error.message);
  } finally {
    state.exportDeleteBusy = false;
    updateExportSelectionControls();
  }
}

async function downloadSelectedTurns() {
  const button = $("#download-export");
  const turnKeys = [...state.selectedExportTurns];
  if (!turnKeys.length) return;
  if (!await selectedTurnsPassPreflight(turnKeys)) return;
  button.disabled = true;
  button.textContent = "正在生成 Excel…";
  try {
    const response = await fetch("/api/exports/turns.xlsx", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ turn_keys: turnKeys }),
    });
    if (!response.ok) {
      let error = {};
      try { error = await response.json(); } catch { error = {}; }
      throw new Error(error.error || `导出失败 (${response.status})`);
    }
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename="([^"]+)"/i);
    const filename = match?.[1] || "completed-turns.xlsx";
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    showNotice(`已导出 ${turnKeys.length} 个完成轮次`);
  } catch (error) {
    showNotice(error.message);
  } finally {
    button.textContent = "导出 Excel";
    updateExportSelectionControls();
  }
}

$("#new-run-button").addEventListener("click", startAutomaticRun);
$("#open-import-baseline").addEventListener("click", openImportBaselineDialog);
$("#close-import-baseline").addEventListener("click", closeImportBaselineDialog);
$("#cancel-import-baseline").addEventListener("click", closeImportBaselineDialog);
$("#import-baseline-form").addEventListener("submit", submitImportedBaseline);
$("#import-project-directory").addEventListener("change", updateImportBaselinePreview);
$("#import-project-numbers").addEventListener("input", updateImportBaselinePreview);
$("#auto-refill-toggle").addEventListener("click", toggleAutoRefill);
$("#auto-refill-start-schedule").addEventListener("click", setAutoRefillStartSchedule);
$("#auto-refill-start-clear").addEventListener("click", clearAutoRefillStartSchedule);
$("#auto-refill-schedule").addEventListener("click", setAutoRefillSchedule);
$("#auto-refill-clear").addEventListener("click", clearAutoRefillSchedule);
$("#module-tab-runs").addEventListener("click", () => navigateTo("#runs"));
$("#open-export-page").addEventListener("click", () => navigateTo("#exports"));
$("#open-analytics-page").addEventListener("click", () => navigateTo("#analytics"));
$("#export-back-to-list").addEventListener("click", () => navigateTo("#runs"));
$("#analytics-date").addEventListener("change", (event) => {
  if (event.target.value) loadHourlyAnalytics(event.target.value);
});
$("#analytics-today").addEventListener("click", () => {
  loadHourlyAnalytics(localDateValue());
});
$("#analytics-chart-bar").addEventListener("click", () => setAnalyticsChartType("bar"));
$("#analytics-chart-line").addEventListener("click", () => setAnalyticsChartType("line"));
$("#hourly-output-chart").addEventListener("pointermove", (event) => {
  const point = event.target.closest("[data-analytics-hour]");
  if (point) showHourlyChartTooltip(point.dataset.analyticsHour, point, event);
});
$("#hourly-output-chart").addEventListener("pointerleave", hideHourlyChartTooltip);
$("#hourly-output-chart").addEventListener("focusin", (event) => {
  const point = event.target.closest("[data-analytics-hour]");
  if (point) showHourlyChartTooltip(point.dataset.analyticsHour, point);
});
$("#hourly-output-chart").addEventListener("focusout", hideHourlyChartTooltip);
$("#download-export").addEventListener("click", downloadSelectedTurns);
$("#delete-selected-export-turns").addEventListener("click", () => {
  deleteExportTurns([...state.selectedExportTurns]);
});
$("#solo-qa-sync").addEventListener("click", () => syncSoloQa());
$("#solo-qa-submit").addEventListener("click", submitSelectedToSoloQa);
$("#copy-solo-qa-helper-path").addEventListener("click", copySoloQaHelperPath);
$("#model-form").addEventListener("submit", submitModel);
$("#global-model").addEventListener("input", () => { state.modelInputDirty = true; });
$("#project-directory").addEventListener("input", renderDirectoryPreview);
$("#run-list").addEventListener("click", (event) => {
  const deleteButton = event.target.closest('[data-run-action="delete"]');
  if (deleteButton) {
    event.stopPropagation();
    deleteRun(deleteButton.dataset.runId);
    return;
  }
  const row = event.target.closest("[data-run-id]");
  if (row) navigateTo(`#run/${row.dataset.runId}`);
});
$("#run-list").addEventListener("keydown", (event) => {
  if (!["Enter", " "].includes(event.key)) return;
  const row = event.target.closest("tr[data-run-id]");
  if (row && event.target === row) {
    event.preventDefault();
    navigateTo(`#run/${row.dataset.runId}`);
  }
});
$("#run-filter-query").addEventListener("input", (event) => {
  state.filters.query = event.target.value;
  state.runPage = 1;
  renderRunList();
});
$("#run-filter-task-type").addEventListener("change", (event) => {
  state.filters.taskType = event.target.value;
  state.runPage = 1;
  renderRunList();
});
$("#run-filter-category").addEventListener("change", (event) => {
  state.filters.category = event.target.value;
  state.runPage = 1;
  renderRunList();
});
$("#run-filter-status").addEventListener("change", (event) => {
  state.filters.status = event.target.value;
  state.runPage = 1;
  renderRunList();
});
$("#reset-run-filters").addEventListener("click", () => {
  $("#run-filters").reset();
  state.filters = { query: "", taskType: "", category: "", status: "" };
  state.runPage = 1;
  renderRunList();
});
$("#export-filter-query").addEventListener("input", (event) => {
  state.exportFilters.query = event.target.value;
  state.exportPage = 1;
  renderExportPage();
});
$("#export-filter-task-type").addEventListener("change", (event) => {
  state.exportFilters.taskType = event.target.value;
  state.exportPage = 1;
  renderExportPage();
});
$("#export-filter-difficulty").addEventListener("change", (event) => {
  state.exportFilters.difficulty = event.target.value;
  state.exportPage = 1;
  renderExportPage();
});
$("#export-filter-readiness").addEventListener("change", (event) => {
  state.exportFilters.readiness = event.target.value;
  state.exportPage = 1;
  renderExportPage();
});
$("#export-filter-solo-qa").addEventListener("change", (event) => {
  state.exportFilters.soloQaState = event.target.value;
  state.exportPage = 1;
  renderExportPage();
});
$("#export-filter-date-from").addEventListener("change", (event) => {
  state.exportFilters.dateFrom = event.target.value;
  state.exportPage = 1;
  renderExportPage();
});
$("#export-filter-date-to").addEventListener("change", (event) => {
  state.exportFilters.dateTo = event.target.value;
  state.exportPage = 1;
  renderExportPage();
});
$("#reset-export-filters").addEventListener("click", () => {
  $("#export-filters").reset();
  state.exportFilters = {
    query: "",
    taskType: "",
    difficulty: "",
    readiness: "",
    soloQaState: "",
    dateFrom: "",
    dateTo: "",
  };
  state.exportPage = 1;
  renderExportPage();
});
document.querySelectorAll("[data-table-pagination]").forEach((pagination) => {
  pagination.addEventListener("click", (event) => {
    const button = event.target.closest("[data-page-scope][data-page-target]");
    if (!button || button.disabled) return;
    changeTablePage(button.dataset.pageScope, Number(button.dataset.pageTarget));
  });
});
$("#select-all-export-turns").addEventListener("change", (event) => {
  const visibleKeys = filteredCompletedTurns().map((turn) => turn.key);
  if (event.target.checked) {
    visibleKeys.forEach((key) => state.selectedExportTurns.add(key));
  } else {
    visibleKeys.forEach((key) => state.selectedExportTurns.delete(key));
  }
  renderExportPage();
});
$("#preflight-export").addEventListener("click", () => {
  const selected = [...state.selectedExportTurns];
  const visible = filteredCompletedTurns().map((turn) => turn.key);
  runExportPreflight(selected.length ? selected : visible);
});
$("#select-preflight-passed").addEventListener("click", () => {
  const eligible = new Set(state.exportPreflight?.eligible_keys || []);
  const visible = new Set(filteredCompletedTurns().map((turn) => turn.key));
  state.selectedExportTurns = new Set(
    state.completedTurns
      .filter((turn) => turn.export_ready && eligible.has(turn.key) && visible.has(turn.key))
      .map((turn) => turn.key)
  );
  renderExportPage();
  showNotice(`已选择 ${state.selectedExportTurns.size} 个检查通过轮次`);
});
$("#export-turn-list").addEventListener("change", (event) => {
  const evaluationInput = event.target.closest("[data-evaluation-field]");
  if (evaluationInput) {
    updateEvaluationDraft(evaluationInput);
    return;
  }
  const checkbox = event.target.closest("[data-export-key]");
  if (!checkbox) return;
  if (checkbox.checked) state.selectedExportTurns.add(checkbox.dataset.exportKey);
  else state.selectedExportTurns.delete(checkbox.dataset.exportKey);
  updateExportSelectionControls();
});
$("#export-turn-list").addEventListener("input", (event) => {
  const evaluationInput = event.target.closest("[data-evaluation-field]");
  if (evaluationInput) updateEvaluationDraft(evaluationInput);
});
$("#export-turn-list").addEventListener("click", (event) => {
  const saveEvaluation = event.target.closest("[data-save-evaluation]");
  if (saveEvaluation) {
    saveExportEvaluation(saveEvaluation.dataset.saveEvaluation);
    return;
  }
  const resetEvaluation = event.target.closest("[data-reset-evaluation]");
  if (resetEvaluation) {
    if (!window.confirm("恢复自动评分？已保存的人工修改将被移除。")) return;
    saveExportEvaluation(resetEvaluation.dataset.resetEvaluation, true);
    return;
  }
  const promptButton = event.target.closest("[data-export-prompt-key]");
  if (promptButton) {
    const key = promptButton.dataset.exportPromptKey;
    if (state.expandedExportPrompts.has(key)) state.expandedExportPrompts.delete(key);
    else state.expandedExportPrompts.add(key);
    renderExportPage();
    return;
  }
  const deleteButton = event.target.closest("[data-export-delete-key]");
  if (deleteButton) {
    deleteExportTurns([deleteButton.dataset.exportDeleteKey]);
    return;
  }
  const link = event.target.closest("[data-export-run-id]");
  if (link) navigateTo(`#run/${link.dataset.exportRunId}`);
});
document.querySelectorAll("[data-sort-key]").forEach((button) => {
  button.addEventListener("click", () => {
    const key = button.dataset.sortKey;
    if (state.sortKey === key) state.sortDirection = state.sortDirection === "asc" ? "desc" : "asc";
    else {
      state.sortKey = key;
      state.sortDirection = "desc";
    }
    state.runPage = 1;
    renderRunList();
  });
});
detailView.addEventListener("pointerdown", () => {
  state.detailInteractionUntil = Date.now() + 5000;
});
detailView.addEventListener("pointerup", () => {
  state.detailInteractionUntil = Date.now() + 800;
});
detailView.addEventListener("copy", () => {
  state.detailInteractionUntil = Date.now() + 1200;
});

async function refresh() {
  await Promise.all([loadRuns(), loadHealth()]);
  if (state.selectedId) await loadDetail();
  if (
    window.location.hash === "#exports"
    && Date.now() - state.exportLastLoadedAt >= EXPORT_REFRESH_INTERVAL_MS
  ) {
    await loadCompletedTurns();
  }
  if (
    window.location.hash === "#analytics"
    && Date.now() - state.analyticsLastLoadedAt >= 30000
  ) {
    await loadHourlyAnalytics(state.analyticsDate || localDateValue(), false);
  }
}

async function boot() {
  state.analyticsDate = localDateValue();
  $("#analytics-date").value = state.analyticsDate;
  await Promise.all([loadHealth(), loadRuns()]);
  await applyRoute();
  pingSoloQaBridge();
  state.timer = window.setInterval(refresh, 3000);
  state.durationTimer = window.setInterval(updateStageDurations, 60 * 1000);
}

window.addEventListener("hashchange", applyRoute);
boot();
