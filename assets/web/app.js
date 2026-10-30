"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const ICONS = {
    library: '<path d="M4 3h16v18H4zM8 3v18m4-13h5m-5 4h5"/>',
    grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    calendar: '<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M7 3v4m10-4v4M3 11h18m-13 5h2m4 0h2"/>',
    check: '<rect x="3" y="3" width="18" height="18" rx="5"/><path d="m7 12 3 3 7-7"/>',
    spark: '<path d="m12 3 2.2 6.8L21 12l-6.8 2.2L12 21l-2.2-6.8L3 12l6.8-2.2L12 3Z"/><path d="M19 2v4m-2-2h4"/>',
    settings: '<path d="m10 3-.7 2.3-2.3 1-2.2-.7-2 3.4 1.6 1.7v2.6L2.8 15l2 3.4 2.2-.7 2.3 1L10 21h4l.7-2.3 2.3-1 2.2.7 2-3.4-1.6-1.7v-2.6L21.2 9l-2-3.4-2.2.7-2.3-1L14 3h-4Z"/><circle cx="12" cy="12" r="3"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    close: '<path d="m6 6 12 12M6 18 18 6"/>',
    refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 6.1A8 8 0 0 1 20 12M4 12a8 8 0 0 0 13.9 5.9"/>',
    lock: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 5v2"/>',
    shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    'chevron-left': '<path d="m14 6-6 6 6 6"/>',
    'chevron-right': '<path d="m10 6 6 6-6 6"/>',
    'arrow-right': '<path d="M4 12h15m-5-5 5 5-5 5"/>',
    'arrow-up': '<path d="M12 20V5m-6 6 6-6 6 6"/>',
    download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
    pin: '<path d="M19 10c0 5-7 11-7 11S5 15 5 10a7 7 0 0 1 14 0Z"/><circle cx="12" cy="10" r="2"/>',
    edit: '<path d="m15 4 5 5M4 20l5-1L20 8a3.5 3.5 0 0 0-5-5L4 14l-1 7Z"/>',
    trash: '<path d="M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7"/>',
    compose: '<path d="M12 4H5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h13a2 2 0 0 0 2-2v-7M9 15l1-4L19 2l3 3-9 9-4 1Z"/>',
    history: '<path d="M3 4v5h5"/><path d="M3.5 9a9 9 0 1 1 1.4 9M12 7v5l3 2"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10h.01"/>',
  };
  const state = { token: "", status: null, prefs: {}, date: "", view: "overview", chatCollapsed: false, calendarMode: "month", calendarLoading: false, calendarError: null, overview: {}, calendarEvents: [], tasks: [], deletedTasks: [], taskFilter: "open", taskSort: "due", editingTask: null, taskForSlots: null, taskSlotRequest: 0, planningTask: null, memories: [], sessions: [], sessionId: null, messages: [], feedbackByMessage: {}, actions: [], models: [], model: "", modelAvailable: false, chatProjectId: null, chatBusy: false, sessionAttachments: [], editingEvent: null, eventOriginal: null, eventRequest: null, confirm: null, dataRequest: 0, calendarRequest: 0, sessionRequest: 0 };
  const VIEW_NAMES = { overview: "总览", calendar: "日程", tasks: "待办", memories: "记忆", knowledge: "资料库", automation: "自动化", quality: "评测与反馈", settings: "设置" };
  let toastTimer;
  let knowledgeUI;
  let qualityUI;
  let integrationsUI;
  let apiConfig = null;
  let apiFormDirty = false;
  const actionTargetCache = new Map();
  const actionTargetLoads = new Map();
  const chatDraft = new ChatFiles.Draft();
  let previewRequest = 0;

  function icon(name) {
    const span = document.createElement("span");
    span.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ICONS.info}</svg>`;
    return span;
  }
  function decorateIcons(root = document) { root.querySelectorAll("[data-icon]").forEach((el) => el.replaceChildren(...icon(el.dataset.icon).childNodes)); }
  function el(tag, cls, text) { const node = document.createElement(tag); if (cls) node.className = cls; if (text !== undefined) node.textContent = String(text); return node; }
  function uuid() { return globalThis.crypto && crypto.randomUUID ? crypto.randomUUID() : `web-${Date.now()}-${Math.random().toString(16).slice(2)}`; }
  function showError(id, error) { const node = $(id); node.textContent = error ? explainError(error) : ""; node.hidden = !error; }
  function explainError(error) {
    if (!error) return "操作没有完成，请稍后重试。";
    const friendly={version_conflict:"内容已更新，请刷新后重新操作。",stale_version:"内容已更新，请刷新后重新操作。",request_id_conflict:"这次操作已变化，请重新打开后再试。",csrf_failed:"页面已过期，请刷新后再试。",forbidden_origin:"请从工作台页面操作。",invalid_response:"暂时无法读取结果，请刷新后再试。",internal_error:"操作未完成，请稍后再试。"};
    let message = friendly[error.code] || error.message || "操作未完成，请稍后再试。";
    if (["unknown_outcome", "unknown_result", "write_outcome_unknown"].includes(error.code)) message += "\n请先核对实际结果，不要重复提交。";
    if (error.code === "connection_failed") message += "\n若刚刚进行了保存，请先刷新核查结果。";
    if (error.details && Array.isArray(error.details.conflicts)) message += "\n冲突日程：" + error.details.conflicts.map((e) => `${e.title || "日程"} ${formatTime(e.start)}`).join("、");
    return message;
  }
  function toast(text, failure = false) { clearTimeout(toastTimer); const box = $("toast"); box.textContent = text; box.classList.toggle("error", failure); box.hidden = false; toastTimer = setTimeout(() => { box.hidden = true; }, failure ? 6500 : 3400); }
  async function api(path, options = {}) {
    const method = options.method || "GET";
    const headers = { Accept: "application/json", ...(options.headers || {}) };
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    if (method !== "GET") headers["X-Agent-Token"] = state.token;
    let response;
    try { response = await fetch(path, { ...options, method, headers, body: options.body === undefined ? undefined : JSON.stringify(options.body), credentials: "same-origin" }); }
    catch (_) { throw { code: "connection_failed", message: "无法连接工作台，请检查服务是否正在运行。" }; }
    let result;
    try { result = await response.json(); } catch (_) { throw { code: "invalid_response", message: "暂时无法读取结果，请刷新后再试。" }; }
    if (response.status === 401 && result.error?.code === "authentication_required") window.location.assign("/login");
    if (!response.ok || result.error) throw result.error || { message: "操作未完成，请稍后再试。" };
    return result;
  }
  function timezone() { return state.prefs.timezone || (state.status && state.status.timezone) || "Asia/Shanghai"; }
  function timezoneLabel(value = timezone()) {
    if (value === "Asia/Shanghai") return "北京时间";
    try { return new Intl.DateTimeFormat("zh-CN", {timeZone:value,timeZoneName:"longGeneric"}).formatToParts(new Date()).find(part=>part.type==="timeZoneName").value; }
    catch (_) { return value; }
  }
  function dateKey(value = new Date(), zone = timezone()) {
    const parts = new Intl.DateTimeFormat("en-CA", { timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date(value));
    const get = (name) => parts.find((p) => p.type === name).value;
    return `${get("year")}-${get("month")}-${get("day")}`;
  }
  const { addDays, weekStart, shiftMonth, viewRange, queryChunks, eventsForDay } = CalendarDates;
  function prettyDate(value, full = false) { const date = new Date(`${value}T12:00:00Z`); return new Intl.DateTimeFormat("zh-CN", { timeZone: "UTC", ...(full ? { year: "numeric" } : {}), month: "long", day: "numeric", weekday: "long" }).format(date); }
  function formatTime(value, zone = timezone()) { if (!value) return ""; const date = new Date(value); return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat("zh-CN", { timeZone: zone, hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(date); }
  function localInput(value, zone = timezone()) { return `${dateKey(value, zone)}T${formatTime(value, zone)}`; }
  function eventDay(event) { return dateKey(event.start, event.all_day && event.timezone ? event.timezone : timezone()); }
  function empty(container, heading, detail) { container.replaceChildren(); const box = el("div", "empty-state"); box.append(el("p", "empty-title", heading)); if (detail) box.append(el("p", "empty-detail", detail)); container.append(box); }
  async function busy(button, callback) { const before = button.disabled; button.disabled = true; try { return await callback(); } finally { button.disabled = before || button.dataset.locked === "true"; } }

  function renderStatus() {
    if (!state.status) return;
    const local = state.status.backend === "local_development";
    $("backend-label").textContent = local ? state.cloudMode ? "工作台日历" : "本地日历" : "外部日历";
    $("backend-note").textContent = local ? state.caldavSettings && state.caldavSettings.enabled ? "外部日历已配置" : "未连接外部日历" : "日程由已连接的日历提供";
    $("sidebar-timezone").textContent = timezoneLabel();
    $("agenda-timezone").textContent = timezoneLabel();
    $("current-date").textContent = prettyDate(dateKey(state.status.current_time || new Date()), true);
  }
  function setDate(value) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return;
    state.date = value;
    $("overview-date").value = value;
    $("calendar-date").value = value;
    $("free-start").value = value;
    $("free-end").value = addDays(value, 1);
    $("agenda-subtitle").textContent = prettyDate(value);
    $("overview-month").textContent = `${Number(value.slice(5, 7))}月`;
    $("overview-day").textContent = value.slice(-2);
    $("overview-date-label").textContent = prettyDate(value, true);
  }
  function setView(view) {
    if (!VIEW_NAMES[view]) return;
    const changed = state.view !== view;
    state.view = view;
    history.replaceState(null, "", "#" + view);
    document.querySelectorAll(".view-panel").forEach((panel) => { panel.hidden = panel.id !== `view-${view}`; });
    document.querySelectorAll(".nav-item").forEach((button) => { button.classList.toggle("is-active", button.dataset.view === view); if (button.dataset.view === view) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current"); });
    $("breadcrumb-current").textContent = VIEW_NAMES[view];
    if (changed) window.scrollTo({ top: 0, behavior: "instant" });
    if (view === "automation" && integrationsUI) integrationsUI.load().catch((error) => showError("integration-error", error));
    if (view === "quality" && qualityUI) qualityUI.load().catch((error) => showError("quality-error", error));
    if (view === "knowledge" && knowledgeUI) knowledgeUI.load().catch((error) => showError("knowledge-error", error));
    if (view === "calendar") loadCalendar().catch((error) => showError("global-error", error));
  }
  function eventNode(event) {
    const row = el("div", "event-row");
    const time = el("div", "event-time", event.all_day ? "全天" : formatTime(event.start));
    if (!event.all_day) time.append(el("small", "", formatTime(event.end)));
    const main = el("div", "event-main"); main.append(el("h3", "", event.title));
    const meta = el("div", "event-meta");
    if (event.location) { meta.append(icon("pin"), el("span", "", event.location)); }
    else if (event.all_day) meta.append(el("span", "", "全天日程"));
    if (!event.all_day && dateKey(event.start) !== dateKey(event.end)) meta.append(el("span", "", `${dateKey(event.start)} → ${dateKey(event.end)}`));
    main.append(meta);
    const actions = el("div", "event-actions");
    const edit = el("button", "button is-ghost is-small icon-button"); edit.type = "button"; edit.title = "编辑日程"; edit.setAttribute("aria-label", `编辑 ${event.title}`); edit.append(icon("edit")); edit.addEventListener("click", () => openEvent(event));
    const remove = el("button", "button is-ghost is-small icon-button"); remove.type = "button"; remove.title = "取消日程"; remove.setAttribute("aria-label", `取消 ${event.title}`); remove.append(icon("trash")); remove.addEventListener("click", () => confirmDeleteEvent(event));
    actions.append(edit, remove); row.append(time, main, actions); return row;
  }
  function renderOverview() {
    const events = Array.isArray(state.overview.events) ? state.overview.events : [];
    $("event-count").textContent = events.length;
    $("open-task-count").textContent = state.tasks.filter((t) => !t.completed).length;
    $("memory-count").textContent = state.memories.length;
    const container = $("overview-events");
    if (!events.length) {
      empty(container, "暂无日程", "为这一天添加一项安排。");
      const add = el("button", "button is-outlined is-small empty-add", "添加日程"); add.type = "button";
      add.addEventListener("click", () => openEvent()); container.querySelector(".empty-state").append(add);
    }
    else container.replaceChildren(...events.map(eventNode));
    renderNextEvent(events); renderTasks(); renderMemories();
  }
  function renderNextEvent(events) {
    const target = $("next-event"); target.replaceChildren();
    const current = new Date(), isToday = state.date === dateKey(current);
    const timed = events.filter((event) => !event.all_day).slice().sort((a, b) => Date.parse(a.start) - Date.parse(b.start));
    const upcoming = isToday ? timed.find((event) => Date.parse(event.end) > current.getTime()) : timed[0];
    const ongoing = upcoming && isToday && Date.parse(upcoming.start) <= current.getTime();
    target.append(el("span", "next-event-label", ongoing ? "进行中" : isToday ? "下一场安排" : "当日首项安排"));
    if (!upcoming) { target.append(el("span", "next-event-empty", isToday ? "暂无后续安排" : "暂无安排")); return; }
    target.append(el("span", "next-event-time", `${formatTime(upcoming.start)}—${formatTime(upcoming.end)}`), el("strong", "next-event-title", upcoming.title));
    const view = el("button", "button is-text is-small text-button", "查看"); view.type = "button"; view.addEventListener("click", () => openEvent(upcoming)); target.append(view);
  }
  function openTaskEntry() { setView("tasks"); $("task-title").focus(); }
  function renderTaskEmpty(target, withEntry) {
    const row = el("div", "compact-empty"); row.append(el("span", "", "暂无待办任务"));
    if (withEntry) { const add = el("button", "button is-text is-small text-button", "添加任务"); add.type = "button"; add.addEventListener("click", openTaskEntry); row.append(add); }
    target.replaceChildren(row);
  }
  function setChatCollapsed(collapsed) {
    const panel = $("assistant-panel"), focusWasInside = panel.contains(document.activeElement);
    state.chatCollapsed = collapsed;
    if (collapsed) { if (panel.open) panel.close(); panel.hidden = true; }
    else { panel.hidden = false; if (!panel.open) panel.showModal(); }
    document.documentElement.classList.toggle("assistant-open", !collapsed);
    $("assistant-toggle").setAttribute("aria-expanded", String(!collapsed));
    $("assistant-toggle-label").textContent = collapsed ? "助手" : "关闭助手";
    if (collapsed && focusWasInside) $("assistant-toggle").focus();
    if (!collapsed) requestAnimationFrame(() => { if (state.chatCollapsed) return; const stream = $("chat-stream"); stream.scrollTop = stream.scrollHeight; $("chat-input").focus(); });
  }
  function initializeChatLayout() { setChatCollapsed(true); }
  function calendarRange() { return viewRange(state.date, state.calendarMode); }
  function selectCalendarDay(day, reveal = false) {
    changeDate(day).then(() => {
      if (reveal) $("calendar-selected-day").scrollIntoView({ behavior: "instant", block: "nearest" });
    });
  }
  function calendarEventChip(event, day) {
    const chip = el("button", "calendar-event-chip" + (event.all_day ? " all-day" : ""));
    chip.type = "button";
    const first = eventDay(event);
    const time = event.all_day ? "全天" : first < day ? "续" : formatTime(event.start);
    chip.append(el("span", "calendar-chip-time", time), el("span", "calendar-chip-title", event.title));
    chip.title = `${event.title} · ${event.all_day ? "全天" : `${formatTime(event.start)}—${formatTime(event.end)}`}`;
    chip.setAttribute("aria-label", `${day} ${time} ${event.title}，查看或编辑`);
    chip.addEventListener("click", () => openEvent(event));
    return chip;
  }
  function renderSelectedCalendarDay() {
    const target = $("calendar-selected-day"); target.replaceChildren();
    const events = eventsForDay(state.calendarEvents, state.date, timezone());
    const header = el("div", "calendar-selection-heading");
    const labels = el("div", ""); labels.append(el("h3", "", prettyDate(state.date)));
    labels.append(el("p", "", state.calendarLoading ? "正在读取日程…" : state.calendarError ? "日程读取失败，请刷新重试" : `${events.length} 项日程 · ${timezoneLabel()}`));
    const add = el("button", "button is-outlined is-small", "添加日程"); add.type = "button";
    add.addEventListener("click", () => openEvent()); header.append(labels, add); target.append(header);
    if (state.calendarLoading || state.calendarError) return;
    if (events.length) { const list = el("div", "event-list"); list.append(...events.map(eventNode)); target.append(list); }
    else target.append(el("p", "calendar-selection-empty", "暂无日程。"));
  }
  function renderCalendar() {
    $("view-calendar").dataset.mode = state.calendarMode;
    const container = $("calendar-events"); container.replaceChildren();
    container.className = "calendar-list " + (state.calendarMode === "day" ? "calendar-single-day" : "calendar-board");
    container.setAttribute("aria-busy", String(state.calendarLoading));
    const range = calendarRange();
    const month = state.date.slice(0, 7);
    $("calendar-period").textContent = state.calendarMode === "month" ? `${Number(month.slice(0, 4))}年${Number(month.slice(5))}月` : state.calendarMode === "week" ? `${range.start} — ${addDays(range.end, -1)}` : prettyDate(state.date, true);
    $("calendar-zone").textContent = timezoneLabel();
    $("export-button").replaceChildren(icon("download"), document.createTextNode(state.calendarMode === "month" ? "导出当天日程" : "导出日程"));
    $("export-button").title = state.calendarMode === "month" ? "下载所选日期的日历文件" : "下载当前日期范围的日历文件";
    if (state.calendarMode === "day") {
      // The selected-day panel below contains the complete event list and editing controls.
      container.hidden = true; renderSelectedCalendarDay(); return;
    }
    container.hidden = false;
    const weekdays = el("div", "calendar-weekdays");
    ["一", "二", "三", "四", "五", "六", "日"].forEach((name, index) => weekdays.append(el("span", index > 4 ? "weekend" : "", "周" + name)));
    const grid = el("div", "calendar-date-grid" + (state.calendarMode === "week" ? " calendar-week-grid" : ""));
    grid.setAttribute("role", "group"); grid.setAttribute("aria-label", state.calendarMode === "month" ? "月历" : "周历");
    const today = dateKey();
    for (let day = range.start; day < range.end; day = addDays(day, 1)) {
      const cellDay = day;
      const events = eventsForDay(state.calendarEvents, day, timezone());
      const outside = state.calendarMode === "month" && !day.startsWith(month);
      const cell = el("div", "calendar-date-cell" + (outside ? " outside-month" : "") + (day === state.date ? " selected" : "") + (day === today ? " is-today" : ""));
      cell.dataset.date = day;
      const top = el("div", "calendar-cell-top");
      const dateButton = el("button", "calendar-date-button", String(Number(day.slice(-2)))); dateButton.type = "button";
      dateButton.dataset.date = day;
      dateButton.setAttribute("aria-label", `${day}，${state.calendarLoading ? "正在读取日程" : state.calendarError ? "日程未能读取" : `${events.length}项日程`}`);
      dateButton.setAttribute("aria-pressed", String(day === state.date));
      if (day === today) dateButton.setAttribute("aria-current", "date");
      dateButton.addEventListener("click", () => selectCalendarDay(cellDay));
      const add = el("button", "calendar-cell-add", "+"); add.type = "button"; add.title = "添加日程";
      add.setAttribute("aria-label", `${day} 添加日程`);
      add.addEventListener("click", () => { changeDate(cellDay); openEvent(); });
      top.append(dateButton, add); cell.append(top);
      const list = el("div", "calendar-cell-events");
      const limit = state.calendarMode === "week" ? 6 : 3;
      events.slice(0, limit).forEach((event) => list.append(calendarEventChip(event, cellDay)));
      if (events.length > limit) {
        const more = el("button", "calendar-more-events", `+${events.length - limit} 项`); more.type = "button";
        more.setAttribute("aria-label", `查看 ${day} 的全部${events.length}项日程`);
        more.addEventListener("click", () => selectCalendarDay(cellDay, true)); list.append(more);
      }
      cell.append(list);
      cell.addEventListener("click", (event) => { if (event.target === cell || event.target === list) selectCalendarDay(cellDay); });
      grid.append(cell);
    }
    container.append(weekdays, grid);
    const hint = el("p", "calendar-board-hint", state.calendarLoading ? "正在读取日程…" : state.calendarError ? "日程读取失败，请刷新重试。" : "点击日期查看当天安排，点击日程可查看或编辑。");
    container.append(hint); renderSelectedCalendarDay();
  }
  async function loadCalendar() {
    const request = ++state.calendarRequest;
    const range = calendarRange();
    state.calendarLoading = true; state.calendarError = null; state.calendarEvents = []; renderCalendar();
    try {
      const results = await Promise.all(queryChunks(range).map((part) => api(`/api/events?${new URLSearchParams(part)}`)));
      if (request !== state.calendarRequest) return;
      const events = new Map();
      for (const result of results) {
        if (result.complete !== true || !Array.isArray(result.events)) throw { message: "未能读取完整日程，请刷新重试。" };
        for (const event of result.events) {
          if (events.has(event.id) && events.get(event.id).version !== event.version) throw { message: "日程在读取期间发生变化，请刷新重试。" };
          events.set(event.id, event);
        }
      }
      state.calendarEvents = [...events.values()].sort((a, b) => Date.parse(a.start) - Date.parse(b.start));
    } catch (error) {
      if (request !== state.calendarRequest) return;
      state.calendarError = error; throw error;
    } finally {
      if (request === state.calendarRequest) { state.calendarLoading = false; renderCalendar(); }
    }
  }
  function navigateCalendar(direction) {
    const next = state.calendarMode === "month" ? shiftMonth(state.date, direction) : addDays(state.date, direction * (state.calendarMode === "week" ? 7 : 1));
    changeDate(next);
  }
  async function loadData() {
    const request = ++state.dataRequest;
    const data = await api(`/api/overview?${new URLSearchParams({ date: state.date })}`);
    if (request !== state.dataRequest) return;
    state.overview = data; state.tasks = data.tasks || []; state.deletedTasks = data.deleted_tasks || []; state.memories = data.memories || [];
    renderOverview();
    if (state.view === "calendar") await loadCalendar();
    if (state.view === "knowledge" && knowledgeUI) await knowledgeUI.load();
  }
  async function refreshAfterWrite(message) {
    try { await loadData(); showError("global-error", null); toast(message); }
    catch (error) { showError("global-error", error); toast(`${message}；列表暂未刷新`); }
  }
  function taskAction(task, label, action, callback, iconName) {
    const button = el("button", iconName ? "button is-ghost is-small icon-button" : "button is-outlined is-small", iconName ? undefined : label);
    button.type = "button"; button.dataset.taskId = task.id; button.dataset.taskAction = action;
    button.setAttribute("aria-label", `${label} ${task.title}`); button.title = label;
    if (iconName) button.append(icon(iconName));
    button.addEventListener("click", () => callback()); return button;
  }
  function taskNode(task, compact = false) {
    const row = el("div", "task-row" + (compact ? " task-row-compact" : ""));
    const checkbox = el("input", "task-checkbox"); checkbox.type = "checkbox"; checkbox.checked = Boolean(task.completed); checkbox.disabled = Boolean(task.deleted_at);
    checkbox.setAttribute("aria-label", `${task.completed ? "重新打开" : "完成"}待办 ${task.title}`);
    checkbox.addEventListener("change", async () => { checkbox.disabled = true; try { await api(`/api/tasks/${encodeURIComponent(task.id)}`, { method: "PATCH", body: { completed: checkbox.checked, expected_version: task.version } }); await loadData(); } catch (error) { checkbox.checked = !checkbox.checked; toast(explainError(error), true); } finally { checkbox.disabled = false; } });
    const main = el("div", "task-main"); main.append(el("span", `task-title${task.completed ? " done" : ""}`, task.title));
    const due = TaskLists.dueLabel(task, dateKey());
    if (due) main.append(el("span", "task-due" + (!task.completed && task.due_date < dateKey() ? " overdue" : ""), due));
    const schedule = task.schedule;
    if (schedule && ["pending", "unknown"].includes(schedule.status)) main.append(el("span", "task-schedule-label", "安排结果待核查"));
    else if (schedule && schedule.status === "applied") main.append(el("span", "task-schedule-label", "已关联日程 · 完成状态单独管理"));
    row.append(checkbox, main);
    if (!compact) {
      const actions = el("div", "task-actions");
      if (task.deleted_at) actions.append(taskAction(task, "恢复任务", "restore", () => restoreTask(task)));
      else {
        if (schedule && ["pending", "unknown", "applied"].includes(schedule.status)) actions.append(taskAction(task, schedule.status === "applied" ? "查看日程" : "核查安排", "schedule", () => inspectTaskSchedule(task)));
        else if (!task.completed) actions.append(taskAction(task, "安排时间", "schedule", () => openTaskPlan(task)));
        actions.append(taskAction(task, "编辑任务", "edit", () => openTaskEdit(task), "edit"), taskAction(task, "删除任务", "delete", () => deleteTask(task), "trash"));
      }
      row.append(actions);
    }
    return row;
  }
  function renderTasks() {
    const today = dateKey(), open = TaskLists.sort(TaskLists.select(state.tasks, "open", today)), done = TaskLists.select(state.tasks, "completed", today);
    const labels = { open: "待完成", today: "今天到期", overdue: "已逾期", completed: "已完成", deleted: "回收站" };
    $("task-nav-count").textContent = open.length; $("task-nav-count").hidden = !open.length; $("overview-task-link").hidden = !open.length;
    $("completed-count").textContent = done.length;
    $("task-filter").querySelector('option[value="deleted"]').textContent = `回收站（${state.deletedTasks.length}）`;
    const overview = $("overview-tasks"), all = $("all-open-tasks"), completed = $("all-completed-tasks");
    if (open.length) overview.replaceChildren(...open.slice(0, 4).map((task) => taskNode(task, true))); else renderTaskEmpty(overview, true);
    const visible = TaskLists.sort(TaskLists.select([...state.tasks, ...state.deletedTasks], state.taskFilter, today), state.taskSort);
    $("task-list-title").textContent = labels[state.taskFilter]; $("task-section-count").textContent = `${visible.length} 件`;
    $("task-filter-note").hidden = state.taskFilter !== "deleted";
    $("task-filter-note").textContent = state.taskFilter === "deleted" ? "可恢复已删除的待办，关联日程会保留。" : "";
    if (visible.length) all.replaceChildren(...visible.map((task) => taskNode(task)));
    else if (state.taskFilter === "open") renderTaskEmpty(all, false);
    else empty(all, { today: "今天没有到期任务", overdue: "暂无逾期任务", completed: "暂无已完成任务", deleted: "回收站为空" }[state.taskFilter]);
    document.querySelector(".completed-tasks").hidden = state.taskFilter !== "open" || !done.length;
    completed.replaceChildren(...TaskLists.sort(done, state.taskSort).map((task) => taskNode(task)));
  }
  function openTaskEdit(task) {
    state.editingTask = task; $("task-edit-title").value = task.title; $("task-edit-due").value = task.due_date || "";
    showError("task-edit-error", null); $("task-edit-dialog").showModal(); $("task-edit-title").focus();
  }
  function deleteTask(task) {
    confirmOperation("将任务移入回收站", `${task.title}\n\n可在回收站恢复。已关联的日程会保留，不会一起取消。`, "移入回收站", async () => {
      await api(`/api/tasks/${encodeURIComponent(task.id)}`, { method: "DELETE", body: { expected_version: task.version } }); await refreshAfterWrite("任务已移入回收站");
    });
  }
  async function restoreTask(task) {
    try { await api(`/api/tasks/${encodeURIComponent(task.id)}/restore`, { method: "POST", body: { expected_version: task.version } }); await refreshAfterWrite("任务已恢复"); }
    catch (error) { toast(explainError(error), true); }
  }
  function openTaskPlan(task) {
    state.taskForSlots = task; state.taskSlotRequest++;
    const today = dateKey(); $("task-plan-title").textContent = task.title;
    $("task-plan-start").value = today; $("task-plan-start").min = today;
    $("task-plan-end").value = task.due_date && task.due_date >= today && task.due_date < addDays(today, 7) ? addDays(task.due_date, 1) : addDays(today, 7);
    $("task-plan-duration").value = "60"; $("task-plan-results").replaceChildren(); showError("task-plan-error", null);
    $("task-plan-note").textContent = `按${timezoneLabel()}和工作时间查找。确认后加入日历，待办仍需手动完成。`;
    $("task-plan-dialog").showModal(); $("task-plan-start").focus();
  }
  async function inspectTaskSchedule(task) {
    try {
      const result = await api(`/api/tasks/${encodeURIComponent(task.id)}/schedule`);
      await loadData();
      if (result.schedule && result.schedule.status === "applied") { openEvent(result.schedule.event); return; }
      if (result.schedule && ["pending", "unknown"].includes(result.schedule.status)) {
        confirmOperation("核查并继续原安排", "先查看上次安排的结果，再继续未完成的安排，不会重复创建。", "继续原安排", async () => {
          const checked = await api(`/api/tasks/${encodeURIComponent(task.id)}/schedule/retry`, { method: "POST", body: {} });
          if (!checked.schedule || checked.schedule.status !== "applied") throw { message: "原安排已取消或未完成，请重新选择时段。" };
          await refreshAfterWrite("已核查并保存原安排");
        }, false);
      } else { toast("原日程已取消或没有有效安排，可重新选择时段"); }
    } catch (error) { toast(explainError(error), true); }
  }
  async function findTaskSlots(event) {
    event.preventDefault(); showError("task-plan-error", null);
    const task = state.taskForSlots, request = ++state.taskSlotRequest;
    if (!task) return;
    const startDay = $("task-plan-start").value, end = $("task-plan-end").value;
    if (end <= startDay) { showError("task-plan-error", { message: "结束日期必须晚于开始日期。" }); return; }
    const start = startDay === dateKey() ? new Date().toISOString() : startDay;
    const query = new URLSearchParams({ start, end, duration_minutes: $("task-plan-duration").value, limit: "12" });
    $("task-plan-results").replaceChildren();
    await busy($("find-task-slots"), async () => {
      try {
        const result = await api(`/api/free-slots?${query}`);
        if (request !== state.taskSlotRequest || !$("task-plan-dialog").open) return;
        const target = $("task-plan-results");
        if (!(result.slots || []).length) { empty(target, "暂无符合条件的空闲时段", "可调整查询日期、预计用时或工作时段。"); return; }
        result.slots.forEach((slot) => {
          const row = el("div", "task-slot"); row.append(el("span", "", `${prettyDate(dateKey(slot.start))} ${formatTime(slot.start)}—${formatTime(slot.end)}`));
          const choose = el("button", "button is-outlined is-small", "选择时段"); choose.type = "button";
          choose.addEventListener("click", () => { $("task-plan-dialog").close(); openEvent(null, { ...slot, title: task.title }, task); }); row.append(choose); target.append(row);
        });
      } catch (error) { if (request === state.taskSlotRequest) showError("task-plan-error", error); }
    });
  }
  function renderMemories() {
    const list = $("memory-list");
    if (!state.memories.length) { empty(list, "暂无记忆", "仅保存你主动添加的信息。"); return; }
    list.replaceChildren(...state.memories.map((memory, index) => {
      const card = el("article", "memory-card"); const header = el("div", "memory-card-header"); const label = el("span", ""); label.append(icon("spark"), document.createTextNode(`记忆 ${String(index + 1).padStart(2, "0")}`));
      const remove = el("button", "button is-ghost is-small icon-button"); remove.type = "button"; remove.setAttribute("aria-label", "删除这条记忆"); remove.append(icon("trash"));
      remove.addEventListener("click", () => confirmOperation("删除这条记忆", "删除后，后续对话将不再使用这条记忆。\n\n" + memory.content, "删除记忆", async () => { await api(`/api/memories/${encodeURIComponent(memory.id)}`, { method: "DELETE", body: {} }); await refreshAfterWrite("记忆已删除"); }));
      header.append(label, remove); card.append(header, el("p", "", memory.content)); return card;
    }));
  }
  function renderPreferences() {
    const p = state.prefs; $("pref-timezone").value = p.timezone || "Asia/Shanghai";
    [["work-start", "work_start"], ["work-end", "work_end"], ["lunch-start", "lunch_start"], ["lunch-end", "lunch_end"]].forEach(([id, key]) => { $("pref-" + id).value = p[key] || ""; });
    $("pref-buffer").value = p.buffer_minutes === undefined ? 15 : p.buffer_minutes;
    const weekdays = $("weekday-options"); weekdays.replaceChildren();
    ["一", "二", "三", "四", "五", "六", "日"].forEach((name, index) => { const label = el("label", "checkbox"); const box = el("input", ""); box.type = "checkbox"; box.value = index; box.checked = (p.workdays || [0, 1, 2, 3, 4]).includes(index); box.setAttribute("aria-label", "星期" + name); label.append(box, el("span", "", name)); weekdays.append(label); });
  }
  function setChatMode() {
    const enabled = Boolean(apiConfig && apiConfig.enabled);
    $("chat-mode").textContent = enabled ? (state.runtimeInfo && !state.runtimeInfo.available ? "智能对话暂不可用，请检查服务" : `智能对话 · ${apiConfig.model}`) : "基础助手 · 日程、资料与提醒";
  }
  function markAPIFormDirty() { apiFormDirty = true; $("test-api-connection").disabled = true; $("api-test-result").hidden = true; }
  function applyAPISettings(settings) {
    apiConfig = settings; apiFormDirty = false; state.model = "";
    $("api-base-url").value = settings.base_url || ""; $("api-model").value = settings.model || "";
    $("api-enabled").checked = Boolean(settings.enabled); $("api-clear-key").checked = false;
    $("api-key").value = ""; $("api-key").type = "password"; $("toggle-api-key").textContent = "显示"; $("toggle-api-key").setAttribute("aria-pressed", "false");
    $("api-key").placeholder = settings.has_key ? "已保存，留空保留原 Key" : "稍后填写 Key";
    $("api-key-status").textContent = settings.has_key ? "Key 已保存，留空保留。" : "尚未填写 Key，无需密钥的服务可留空。";
    const test = settings.last_test;
    $("model-detail").textContent = (settings.enabled ? "API 已启用" : "API 未启用") + (test ? test.ok ? " · 连接测试通过" : " · 连接测试未通过" : " · 尚未测试");
    $("test-api-connection").disabled = !settings.base_url || !settings.model;
    setChatMode();
  }
  async function loadModels() {
    try { const result = await api("/api/model-api"); applyAPISettings(result.settings); showError("model-error", null); $("api-test-result").hidden = true; }
    catch (error) { $("model-detail").textContent = "暂时无法读取模型设置"; showError("model-error", error); }
  }
  async function loadSessions() { const result = await api("/api/sessions"); state.sessions = result.sessions || []; const select = $("session-select"); select.replaceChildren(); const first = el("option", "", "新的对话"); first.value = ""; select.append(first); state.sessions.forEach((s) => { const option = el("option", "", s.title || "未命名对话"); option.value = s.id; select.append(option); }); select.value = state.sessionId || ""; }
  function chatWelcome() {
    const welcome = el("div", "chat-welcome");
    welcome.append(el("h3", "", "从一个问题开始"), el("p", "", "查日程、导入资料、设置提醒，也可以整理待办。"));
    const prompts = el("div", "prompt-chips");
    [{ label: "查询今天的日程", prompt: "今天有什么安排？" }, { label: "导入链接", prompt: "导入链接：" }, { label: "设置提醒", prompt: "明天15:00提醒我整理资料" }].forEach(({ label, prompt }) => {
      const button = el("button", "prompt-chip", label); button.type = "button";
      button.addEventListener("click", () => { $("chat-input").value = prompt; $("chat-input").focus(); }); prompts.append(button);
    });
    welcome.append(prompts); return welcome;
  }
  function updateChatControls() {
    const locked = state.chatBusy || chatDraft.uploading;
    ["send-message", "session-select", "new-chat", "chat-project", "add-chat-file"].forEach(id => { $(id).disabled = locked; });
    $("chat-input").readOnly = state.chatBusy;
  }
  function resetChatDraft() {
    chatDraft.reset(); $("chat-files").value = ""; renderChatDraft(); updateChatControls();
  }
  function previewBody(preview) {
    const target = $("chat-preview-body"); target.replaceChildren();
    const info = [preview.filename || preview.url || "", Number.isFinite(preview.characters) ? `${preview.characters} 字` : ""].filter(Boolean).join(" · ");
    if (info) target.append(el("p", "source-meta", info));
    if (preview.warning) target.append(el("p", "chat-file-warning", preview.warning));
    if (Array.isArray(preview.duplicates) && preview.duplicates.length) target.append(el("p", "field-help", "资料库中可能已有相同内容：" + preview.duplicates.map(item => item.title).join("、")));
    target.append(el("div", "chat-preview-content", preview.content || "未提取到正文。"));
    $("chat-preview-title").textContent = preview.title || preview.filename || "资料预览";
  }
  function showLocalPreview(preview) {
    previewRequest++; previewBody(preview); if (!$("chat-preview-dialog").open) $("chat-preview-dialog").showModal();
  }
  async function openChatPreview(previewId) {
    if (!state.sessionId) { showError("chat-error", { message: "请先发送附件或导入请求。" }); return; }
    const request = ++previewRequest, sessionId = state.sessionId;
    const target = $("chat-preview-body"); target.replaceChildren(el("p", "field-help", "正在读取资料…"));
    $("chat-preview-title").textContent = "资料预览"; if (!$("chat-preview-dialog").open) $("chat-preview-dialog").showModal();
    try {
      const result = await api(`/api/chat/previews/${encodeURIComponent(previewId)}?${new URLSearchParams({session_id:sessionId})}`);
      if (request !== previewRequest || state.sessionId !== sessionId || !$("chat-preview-dialog").open) return;
      previewBody(result.preview);
    } catch (error) { if (request === previewRequest && $("chat-preview-dialog").open) target.replaceChildren(el("p", "inline-error", explainError(error))); }
  }
  function attachmentRow(attachment, removable = false) {
    const row = el("div", "chat-file-row"), main = el("div", "chat-file-main");
    main.append(el("span", "chat-file-name", attachment.filename || attachment.title || "文件"), el("span", "source-meta", `${attachment.characters || 0} 字${attachment.truncated ? " · 已截选" : ""}`));
    const actions = el("div", "chat-file-actions"), view = el("button", "button is-ghost is-small", "预览"); view.type = "button";
    view.addEventListener("click", () => { if (typeof attachment.content === "string") showLocalPreview(attachment); else openChatPreview(attachment.preview_id || attachment.id); }); actions.append(view);
    if (removable) { const remove = el("button", "button is-ghost is-small", "移除"); remove.type = "button"; remove.disabled = state.chatBusy || chatDraft.uploading; remove.setAttribute("aria-label", "移除文件 " + (attachment.filename || attachment.title || "")); remove.addEventListener("click", () => { chatDraft.remove(attachment.id); renderChatDraft(); }); actions.append(remove); }
    row.append(main, actions); return row;
  }
  function renderChatDraft() {
    const list = $("chat-attachments"); list.replaceChildren(); list.hidden = !chatDraft.items.length;
    chatDraft.items.forEach(attachment => list.append(attachmentRow(attachment, true)));
    $("chat-upload-status").hidden = !chatDraft.uploading;
    $("chat-upload-status").textContent = chatDraft.uploading ? "正在解析文件，完成后可发送…" : "";
  }
  function readFile(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => { const data = String(reader.result || "").split(",")[1]; if (!data) reject(new Error("文件读取失败，请重新选择。")); else resolve(data); };
      reader.onerror = () => reject(new Error("文件读取失败，请重新选择。")); reader.onabort = () => reject(new Error("文件读取已取消。")); reader.readAsDataURL(file);
    });
  }
  async function uploadChatFiles() {
    const files = Array.from($("chat-files").files || []); $("chat-files").value = "";
    if (!files.length || state.chatBusy) return;
    showError("chat-error", null); let ticket, sessionId;
    const known = new Set([...state.sessionAttachments, ...chatDraft.items].map(item=>item.id));
    try {
      ticket = chatDraft.begin(files); const projectId = state.chatProjectId;
      updateChatControls(); renderChatDraft();
      if (!state.sessionId) {
        const created = await api("/api/sessions", {method:"POST",body:{project_id:projectId}});
        if (!chatDraft.current(ticket)) return;
        state.sessionId = created.session.id;
      }
      sessionId = state.sessionId;
      for (const file of files) {
        const data = await readFile(file); if (!chatDraft.current(ticket)) return;
        const result = await api("/api/chat/attachments", { method:"POST", body:{filename:file.name, data, session_id:sessionId, project_id:projectId} });
        if (chatDraft.accept(ticket, result.attachment)) renderChatDraft();
      }
    } catch (error) {
      if (!ticket || chatDraft.current(ticket)) {
        if (sessionId && error.code === "connection_failed") {
          try {
            const saved = await api(`/api/sessions/${encodeURIComponent(sessionId)}`);
            if (chatDraft.current(ticket) && state.sessionId === sessionId) {
              let restored = 0;
              (saved.attachments || []).forEach(attachment => { if (!known.has(attachment.id) && chatDraft.accept(ticket, attachment)) restored++; });
              if (restored) error = {message:"已找回解析完成的文件，请核对列表后发送。尚未发送聊天请求。"};
            }
          } catch (_) { /* Keep uploaded IDs and require an explicit later action. */ }
        }
        showError("chat-error", error);
      }
    }
    finally {
      if (ticket) chatDraft.finish(ticket);
      if (sessionId && state.sessionId === sessionId) { try { await loadSessions(); } catch (_) { /* Keep the known session and uploaded IDs. */ } }
      renderChatDraft(); updateChatControls();
    }
  }
  function actionTargetKey(action) { return JSON.stringify([action.arguments.event_id,action.arguments.expected_version]); }
  function savedActionTarget(action) {
    const args=action.arguments||{};
    if(args.event_id) return actionTargetCache.get(actionTargetKey(action)) || (action.result&&action.result.event) || null;
    if(args.task_id) return [...state.tasks,...state.deletedTasks].find(task=>task.id===args.task_id) || (action.result&&action.result.task) || null;
    return null;
  }
  async function readActionTarget(action) {
    const key=actionTargetKey(action);
    if(actionTargetLoads.has(key))return actionTargetLoads.get(key);
    const loading=api(`/api/events/${encodeURIComponent(action.arguments.event_id)}`).then(result=>{
      if(result.event.version!==action.arguments.expected_version)throw {code:"version_conflict",message:"原日程已更新，请重新生成这次操作。"};
      actionTargetCache.set(key,result.event);return result.event;
    }).finally(()=>actionTargetLoads.delete(key));
    actionTargetLoads.set(key,loading);return loading;
  }
  function actionTime(event) {
    if(!event.start||!event.end)return "";
    const zone=event.timezone||timezone();
    if(event.all_day){const day=value=>value.includes("T")?dateKey(value,zone):value;return `${day(event.start)} 至 ${day(event.end)}（不含结束当天）`;}
    const display=value=>/(?:Z|[+-]\d{2}:\d{2})$/.test(value)?localInput(value,zone).replace("T"," "):value.replace("T"," ");
    return `${display(event.start)} → ${display(event.end)}`;
  }
  function actionDetails(action,target=savedActionTarget(action)) {
    const args=action.arguments||{};
    const review=ChatFiles.reviewLines(action); if(review!==null)return review;
    if(action.tool==="calendar_set_preferences"){
      const labels={timezone:"时区",workdays:"工作日",work_start:"开始工作",work_end:"结束工作",lunch_start:"午休开始",lunch_end:"午休结束",buffer_minutes:"日程间隔"};
      return Object.entries(args).filter(([key])=>labels[key]).map(([key,value])=>`${labels[key]}：${key==="timezone"?timezoneLabel(value):key==="workdays"?value.map(day=>"周"+["一","二","三","四","五","六","日"][day]).join("、"):key==="buffer_minutes"?value+" 分钟":value}`).join("\n");
    }
    if(action.tool&&action.tool.startsWith("calendar_")){
      const lines=[];
      if(args.event_id&&target){lines.push((action.status==="pending"?"原日程：":"日程：")+target.title,actionTime(target));if(action.tool==="calendar_update_event")lines.push("调整后：");}
      if(args.event_id&&!target)return (action.status||"pending")==="pending"?"正在读取原日程…":action.status==="applied"?"已完成这项日程操作。":"这项日程的信息暂不可用。";
      const event=args.changes?{...target,...args.changes}:args;
      if(action.tool!=="calendar_delete_event"){
        if(event.title)lines.push(event.title);
        const time=actionTime(event);if(time)lines.push(time);
        if(event.location)lines.push("地点："+event.location);
        if(event.description)lines.push("备注："+event.description);
      }
      if(args.recurrence){const r=args.recurrence;lines.push(`重复：${(r.interval||1)===1?({daily:"每天",weekly:"每周",monthly:"每月"})[r.frequency]:"每 "+r.interval+({daily:"天",weekly:"周",monthly:"月"})[r.frequency]}${r.weekdays?"（"+r.weekdays.map(day=>"周"+["一","二","三","四","五","六","日"][day]).join("、")+"）":""} · ${r.count?"共 "+r.count+" 次":"截至 "+r.until}`);}
      if(args.scope)lines.push(args.scope==="series"?"范围：整个系列，会清除单次修改和取消记录。":"范围：仅本次");
      lines.push("时间："+timezoneLabel(event.timezone||timezone()));return lines.filter(Boolean).join("\n");
    }
    if(args.content)return args.content;
    if(args.title)return args.title+(args.due_date?"\n到期："+args.due_date:"");
    if(args.task_id)return target?"待办："+target.title:action.status==="applied"?"已完成这项待办操作。":"这项待办已不可用，请重新确认。";
    return "请核对后确认。";
  }
  function renderAction(action) {
    const card = el("article", "action-card"); const status = action.status || "pending"; const args=action.arguments||{};
    const statusText = { pending: "等待确认", applied: "已完成", failed: "操作未完成", unknown: "结果待确认，请勿重复操作" }[status] || status;
    const detail=el("div", "action-detail", actionDetails(action));
    card.append(el("div", "action-status", statusText), el("h4", "", action.title || args.review && args.review.title || "待确认操作"), detail);
    if (args.preview_id) { const preview = el("button", "button is-ghost is-small action-preview", "查看完整预览"); preview.type="button"; preview.addEventListener("click",()=>openChatPreview(args.preview_id)); card.append(preview); }
    if (status === "pending") {
      const destructive = ["calendar_delete_event", "assistant_delete_reminder"].includes(action.tool);
      const button = el("button", "button "+(destructive?"is-danger":"is-primary"), "确认执行"); button.type = "button";
      let targetReady = !args.event_id && (!args.task_id || Boolean(savedActionTarget(action)));
      let acknowledgement = null, applying = false;
      const feedback=el("p", "inline-error"); feedback.hidden=true;
      if (ChatFiles.requiresTruncation(action)) {
        const label = el("label", "checkbox-label chat-truncation-check"); acknowledgement = el("input", ""); acknowledgement.type="checkbox"; acknowledgement.checked=false;
        label.append(acknowledgement, el("span", "", "我已核对截选内容")); card.append(label);
      }
      const enable = () => { button.disabled = applying || !targetReady || Boolean(acknowledgement && !acknowledgement.checked); if(acknowledgement)acknowledgement.disabled=applying; };
      if (acknowledgement) acknowledgement.addEventListener("change", enable);
      enable();
      if(args.event_id){
        button.textContent="正在读取日程…";
        readActionTarget(action).then(target=>{if(!card.isConnected)return;const stream=$("chat-stream"),follow=stream.scrollHeight-stream.scrollTop-stream.clientHeight<48;detail.textContent=actionDetails(action,target);button.textContent="确认执行";targetReady=true;enable();if(follow)stream.scrollTop=stream.scrollHeight;}).catch(error=>{if(!card.isConnected)return;detail.textContent=error.code==="version_conflict"?"原日程已更新，请重新生成这次操作。":"无法读取原日程，请刷新或重新生成这次操作。";button.textContent="请重新确认";});
      } else if(args.task_id&&!savedActionTarget(action)){button.textContent="请重新确认";}
      button.addEventListener("click", async () => {
        if(button.disabled)return; applying=true; enable(); feedback.hidden=true;
        try {
          const confirmation = ChatFiles.confirmation(action, acknowledgement && acknowledgement.checked);
          const result = await api(`/api/actions/${encodeURIComponent(action.id)}/apply`, { method: "POST", body: confirmation });
          Object.assign(action, result.action || { status: "applied", result: result.result });
          await refreshAfterWrite("操作已完成");
          if (action.tool === "assistant_save_import" && knowledgeUI) { try { await knowledgeUI.load(); } catch (error) { toast("资料已保存，列表暂未刷新", true); } }
          if (["assistant_create_reminder", "assistant_update_reminder", "assistant_delete_reminder", "assistant_sync_calendar"].includes(action.tool) && integrationsUI) { try { await integrationsUI.load(); } catch (error) { toast("操作已完成，连接状态暂未刷新", true); } }
          renderChat();
        } catch (error) {
          if (error.code === "confirmation_required") { feedback.textContent=explainError(error);feedback.hidden=false;return; }
          if (error.details && error.details.action) Object.assign(action, error.details.action);
          else if (error.code !== "connection_failed") Object.assign(action, { status: ["unknown_outcome", "unknown_result", "write_outcome_unknown"].includes(error.code) ? "unknown" : "failed", result: { error } });
          if (state.sessionId) { try { await selectSession(state.sessionId); } catch (_) { /* Keep the write error visible until its saved state can be read. */ } }
          if (action.status === "pending") { action.status = "unknown"; action.result = { error: { message: "暂时无法确认结果。恢复连接后，请重新打开对话查看，不要重复操作。" } }; }
          renderChat();
        } finally { applying=false;enable(); }
      }); card.append(feedback,button);
    } else if (action.result) { const text = action.result.error ? explainError(action.result.error) : "操作已完成。"; card.append(el("p", `action-result${action.result.error ? " failed" : ""}`, text)); }
    return card;
  }
  function renderChat() {
    const stream = $("chat-stream"); stream.replaceChildren();
    if (state.sessionAttachments.length) {
      const files = el("details", "chat-session-files"); files.append(el("summary", "", `本次对话的文件（${state.sessionAttachments.length}）`));
      state.sessionAttachments.forEach(attachment => files.append(attachmentRow(attachment))); stream.append(files);
    }
    if (!state.messages.length) stream.append(chatWelcome());
    else state.messages.forEach((message) => { const row = el("div", `chat-message ${message.role === "user" ? "user" : "assistant"}`), label = el("div", "message-label", message.role === "user" ? "我" : "助手"); row.append(label, el("div", "message-bubble", message.content || ""));
      if (message.sources && message.sources.length) {
        const refs = el("div", "answer-sources"); refs.append(el("span", "muted", "参考资料 · 引用时的内容"));
        message.sources.forEach((source) => { const link = el("button", "source-reference", source.title); link.type = "button"; link.addEventListener("click", () => qualityUI.citation(source)); refs.append(link); }); row.append(refs);
      }
      if (message.role === "assistant" && message.id && qualityUI) row.append(qualityUI.answerControls(message,state.sessionId,state.feedbackByMessage[message.id]));
      stream.append(row); });
    state.actions.forEach((action) => stream.append(renderAction(action)));
    if (state.chatBusy) { const pending = el("div", "chat-message pending"); pending.append(el("div", "message-label", "助手"), el("div", "message-bubble", "正在处理…")); stream.append(pending); }
    stream.scrollTop = stream.scrollHeight;
  }
  async function sendMessage(event) {
    event.preventDefault(); if (state.chatBusy || chatDraft.uploading) return;
    const input = $("chat-input"), attachments = chatDraft.snapshot(), original = input.value, message = ChatFiles.message(original, attachments); if (!message) return;
    showError("chat-error", null); const previous = state.messages.slice(); state.messages.push({ role: "user", content: message + (attachments.length ? "\n\n附件：" + attachments.map(item=>item.filename || item.title).join("、") : "") }); state.chatBusy = true; input.value = ""; updateChatControls(); renderChatDraft(); renderChat();
    if (integrationsUI) integrationsUI.watch(true);
    try {
      const body = { message, project_id: state.chatProjectId }; if (state.sessionId) body.session_id = state.sessionId; if (state.model) body.model = state.model; if (attachments.length) body.attachment_ids = attachments.map(item=>item.id);
      const result = await api("/api/chat", { method: "POST", body }); state.sessionId = result.session_id;
      state.sessionAttachments = Array.isArray(result.attachments) ? result.attachments : ChatFiles.merge(state.sessionAttachments, attachments);
      state.messages.push({ id:result.message_id, role: "assistant", content: result.reply || "已处理这条请求。", sources: result.sources || [] }); state.actions.push(...(result.actions || []));
      chatDraft.reset(); renderChatDraft();
      try { await loadSessions(); } catch (error) { toast("消息已处理，对话列表暂未刷新", true); }
    } catch (error) { state.messages = previous; input.value = original; showError("chat-error", error); }
    finally { if (integrationsUI) integrationsUI.watch(false); state.chatBusy = false; updateChatControls(); renderChatDraft(); renderChat(); }
  }
  async function selectSession(value) {
    if (state.chatBusy || chatDraft.uploading) return;
    const request = ++state.sessionRequest;
    if (value !== state.sessionId) { resetChatDraft(); state.sessionAttachments=[]; previewRequest++; if ($("chat-preview-dialog").open) $("chat-preview-dialog").close(); }
    if (!value) { state.sessionId = null; state.messages = []; state.actions = []; renderChat(); return; }
    const result = await api(`/api/sessions/${encodeURIComponent(value)}`); if (request !== state.sessionRequest) return; state.sessionId = result.id || value; state.chatProjectId = result.project_id || null; $("chat-project").value = state.chatProjectId || ""; state.messages = result.messages || []; state.feedbackByMessage = Object.fromEntries((result.feedback || []).map(f=>[f.message_id,f])); state.actions = result.actions || []; state.sessionAttachments = result.attachments || []; renderChat();
  }
  function openEvent(event = null, suggested = null, task = null) {
    state.planningTask = task; $("event-title").readOnly = Boolean(task); $("event-all-day").disabled = Boolean(task); $("event-task-note").hidden = !task;
    state.editingEvent = event; state.eventRequest = null; const form = $("event-form"); form.reset(); showError("event-error", null); $("save-event").disabled = false; delete $("save-event").dataset.locked;
    $("event-dialog-title").textContent = task ? "确认任务安排" : event ? "编辑日程" : "新建日程"; $("save-event").textContent = task ? "确认加入日历" : event ? "保存修改" : "保存日程";
    const data = event || suggested || {};
    $("event-title").value = data.title || ""; $("event-location").value = data.location || ""; $("event-description").value = data.description || ""; $("event-all-day").checked = Boolean(data.all_day);
    eventTimeInputs();
    const zone = event && event.timezone ? event.timezone : timezone();
    if (data.start) $("event-start").value = data.all_day ? dateKey(data.start, zone) : localInput(data.start, zone);
    else $("event-start").value = `${state.date}T09:00`;
    if (data.end) $("event-end").value = data.all_day ? dateKey(data.end, zone) : localInput(data.end, zone);
    else $("event-end").value = `${state.date}T10:00`;
    $("event-repeat-create").hidden = Boolean(event || task); $("event-repeat-edit").hidden = !event || !event.series_id;
    $("event-repeat").value = "none"; $("event-repeat-scope").value = "occurrence"; $("event-repeat-options").hidden = true;
    $("event-repeat-until-field").hidden = true; $("event-repeat-count-field").hidden = false;
    state.eventOriginal = eventFields();
    $("event-dialog").showModal(); $("event-title").focus();
  }
  function eventFields() { return { title: $("event-title").value.trim(), start: $("event-start").value, end: $("event-end").value, all_day: $("event-all-day").checked, location: $("event-location").value, description: $("event-description").value }; }
  function eventTimeInputs() {
    const allDay = $("event-all-day").checked;
    const start = $("event-start"), end = $("event-end"); const oldStart = start.value, oldEnd = end.value;
    start.type = allDay ? "date" : "datetime-local"; end.type = allDay ? "date" : "datetime-local";
    if (allDay) { start.value = oldStart ? oldStart.slice(0, 10) : state.date; end.value = oldEnd ? oldEnd.slice(0, 10) : addDays(state.date, 1); if (end.value <= start.value) end.value = addDays(start.value, 1); }
    else { start.value = oldStart && oldStart.includes("T") ? oldStart : `${oldStart || state.date}T09:00`; end.value = oldEnd && oldEnd.includes("T") ? oldEnd : `${oldStart || state.date}T10:00`; }
    const zone = state.editingEvent && state.editingEvent.timezone || timezone();
    $("event-time-help").textContent = allDay ? `按${timezoneLabel(zone)}填写。结束日期不含当天，单日安排请选次日结束。` : `时间按${timezoneLabel(zone)}填写。`;
  }
  async function saveEvent(event) {
    event.preventDefault(); showError("event-error", null);
    const fields = eventFields();
    const edit = state.editingEvent;
    if (!fields.title || !fields.start || !fields.end || (!edit && fields.end <= fields.start)) { showError("event-error", { message: "请填写标题，并确保结束晚于开始。" }); return; }
    const changes = edit ? Object.fromEntries(Object.entries(fields).filter(([key, value]) => value !== state.eventOriginal[key])) : fields;
    if (edit && !Object.keys(changes).length) { $("event-dialog").close(); toast("没有修改"); return; }
    if (!edit && !state.planningTask && $("event-repeat").value !== "none") {
      const recurrence = {frequency:$("event-repeat").value,interval:Number($("event-repeat-interval").value)};
      if ($("event-repeat-end-type").value === "until") recurrence.until = $("event-repeat-until").value;
      else recurrence.count = Number($("event-repeat-count").value);
      fields.recurrence = recurrence; changes.recurrence = recurrence;
    }
    const fingerprint = JSON.stringify([changes,$("event-repeat-scope").value]);
    if (!state.eventRequest || state.eventRequest.fingerprint !== fingerprint) state.eventRequest = { id: uuid(), fingerprint };
    await busy($("save-event"), async () => {
      try { if (edit) await api(`/api/events/${encodeURIComponent(edit.id)}`, { method: "PATCH", body: { request_id: state.eventRequest.id, expected_version: edit.version, changes, ...(edit.series_id ? {scope:$("event-repeat-scope").value} : {}) } }); else if (state.planningTask) await api(`/api/tasks/${encodeURIComponent(state.planningTask.id)}/schedule`, { method: "POST", body: { request_id: state.eventRequest.id, expected_version: state.planningTask.version, start: fields.start, end: fields.end, location: fields.location, description: fields.description } }); else await api("/api/events", { method: "POST", body: { request_id: state.eventRequest.id, ...fields } }); $("event-dialog").close(); await refreshAfterWrite(state.planningTask ? "已安排日程，待办仍需手动完成" : edit ? "日程已更新" : "日程已保存"); }
      catch (error) { showError("event-error", error); if (["unknown_outcome", "unknown_result", "write_outcome_unknown", ...(state.planningTask ? ["connection_failed", "storage_unavailable"] : [])].includes(error.code)) { $("save-event").dataset.locked = "true"; $("save-event").textContent = "结果需核查"; } }
    });
  }
  function confirmOperation(title, description, buttonText, callback, destructive = true) { $("confirm-series-control").hidden = true; $("confirm-series-scope").value = "occurrence"; $("confirm-action").classList.toggle("is-danger", destructive); $("confirm-action").classList.toggle("is-primary", !destructive); document.querySelector("#confirm-dialog .dialog-actions [data-close-dialog]").textContent = destructive ? "保留" : "取消"; state.confirm = callback; $("confirm-title").textContent = title; $("confirm-description").textContent = description; $("confirm-action").textContent = buttonText; $("confirm-action").disabled = false; delete $("confirm-action").dataset.locked; showError("confirm-error", null); $("confirm-dialog").showModal(); }
  function confirmDeleteEvent(event) {
    const requestId = uuid(); confirmOperation("取消这项日程", `${event.title}\n${event.all_day ? "全天" : `${formatTime(event.start)}—${formatTime(event.end)}`} · ${eventDay(event)}\n\n${event.series_id ? "请选择取消本次或整个系列，整个系列包括已做单次调整的日程。" : "只取消这项日程。"}`, "确认取消", async () => { await api(`/api/events/${encodeURIComponent(event.id)}`, { method: "DELETE", body: { request_id: requestId, expected_version: event.version, ...(event.series_id ? {scope:$("confirm-series-scope").value} : {}) } }); await refreshAfterWrite("日程已取消"); });
    $("confirm-series-control").hidden = !event.series_id;
  }

  async function exportCalendar() {
    const response = await fetch(`/api/export?${new URLSearchParams(state.calendarMode === "month" ? { start: state.date, end: addDays(state.date, 1) } : calendarRange())}`, { credentials: "same-origin" });
    if (!response.ok) { const result = await response.json(); throw result.error || { message: "导出失败" }; }
    const blob = await response.blob(), url = URL.createObjectURL(blob), anchor = el("a", ""); anchor.href = url; anchor.download = `我的日程-${state.date}.ics`; document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); toast("日历文件已导出，后续修改不会自动同步。");
  }
  function bindEvents() {
    $("event-repeat").addEventListener("change", () => { $("event-repeat-options").hidden = $("event-repeat").value === "none"; });
    $("event-repeat-end-type").addEventListener("change", () => { const until = $("event-repeat-end-type").value === "until"; $("event-repeat-until-field").hidden = !until; $("event-repeat-count-field").hidden = until; });
    $("task-filter").addEventListener("change", (event) => { state.taskFilter = event.target.value; renderTasks(); });
    $("task-sort").addEventListener("change", (event) => { state.taskSort = event.target.value; renderTasks(); });
    $("task-edit-form").addEventListener("submit", async (event) => {
      event.preventDefault(); showError("task-edit-error", null); const task = state.editingTask;
      await busy($("save-task"), async () => { try {
        await api(`/api/tasks/${encodeURIComponent(task.id)}`, { method: "PATCH", body: { title: $("task-edit-title").value.trim(), due_date: $("task-edit-due").value || null, expected_version: task.version } });
        $("task-edit-dialog").close(); await refreshAfterWrite("待办已更新");
      } catch (error) { showError("task-edit-error", error); } });
    });
    $("task-plan-form").addEventListener("submit", findTaskSlots);
    $("task-plan-form").addEventListener("change", () => { state.taskSlotRequest++; $("task-plan-results").replaceChildren(); showError("task-plan-error", null); });
    $("event-dialog").addEventListener("close", () => { if (state.planningTask) loadData().catch((error) => showError("global-error", error)); });
    window.addEventListener("hashchange", () => { const view = location.hash.slice(1); if (state.token && VIEW_NAMES[view] && state.view !== view) setView(view); });
    $("assistant-toggle").addEventListener("click", () => setChatCollapsed(!state.chatCollapsed));
    $("collapse-assistant").addEventListener("click", () => setChatCollapsed(true));
    $("assistant-panel").addEventListener("keydown", (event) => {
      if (event.key !== "Tab") return;
      const panel = event.currentTarget;
      const controls = Array.from(panel.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])')).filter((node) => node.getClientRects().length && getComputedStyle(node).visibility !== "hidden");
      const first = controls[0], last = controls[controls.length - 1];
      if (!first) { event.preventDefault(); return; }
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    });
    $("assistant-panel").addEventListener("cancel", (event) => { event.preventDefault(); setChatCollapsed(true); });
    $("assistant-panel").addEventListener("close", () => { if (!$("assistant-panel").open && !state.chatCollapsed) setChatCollapsed(true); });
    $("assistant-panel").addEventListener("click", (event) => {
      if (event.target !== $("assistant-panel")) return;
      const rect = event.currentTarget.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) setChatCollapsed(true);
    });
    document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
    document.querySelectorAll("[data-go]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.go)));
    document.querySelectorAll("[data-new-event]").forEach((button) => button.addEventListener("click", () => openEvent()));
    document.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", () => $(button.dataset.closeDialog).close()));
    document.querySelectorAll("[data-date-step]").forEach((button) => button.addEventListener("click", () => changeDate(addDays(state.date, Number(button.dataset.dateStep)))));
    ["overview-date", "calendar-date"].forEach((id) => $(id).addEventListener("change", (event) => changeDate(event.target.value)));
    $("today-button").addEventListener("click", () => changeDate(dateKey()));
    $("calendar-prev").addEventListener("click", () => navigateCalendar(-1));
    $("calendar-next").addEventListener("click", () => navigateCalendar(1));
    $("calendar-today").addEventListener("click", () => changeDate(dateKey()));
    document.querySelectorAll("[data-calendar-mode]").forEach((button) => button.addEventListener("click", () => { state.calendarMode = button.dataset.calendarMode; document.querySelectorAll("[data-calendar-mode]").forEach((node) => { const active = node === button; node.classList.toggle("is-selected", active); node.setAttribute("aria-pressed", active); }); loadCalendar().catch((error) => showError("global-error", error)); }));
    $("refresh-button").addEventListener("click", () => busy($("refresh-button"), async () => { try { await loadData(); if (state.sessionId && !state.chatBusy) await selectSession(state.sessionId); showError("global-error", null); toast("已刷新"); } catch (error) { showError("global-error", error); } }));
    $("overview-free-button").addEventListener("click", () => { setView("calendar"); $("free-slots-surface").open = true; $("free-slots-surface").scrollIntoView({ behavior: "instant", block: "center" }); });
    $("event-form").addEventListener("submit", saveEvent); $("event-all-day").addEventListener("change", eventTimeInputs);
    $("confirm-action").addEventListener("click", () => busy($("confirm-action"), async () => { try { if (state.confirm) await state.confirm(); $("confirm-dialog").close(); } catch (error) { showError("confirm-error", error); if (["unknown_outcome", "unknown_result", "write_outcome_unknown"].includes(error.code)) { $("confirm-action").dataset.locked = "true"; $("confirm-action").textContent = "结果需核查"; } } }));
    $("free-form").addEventListener("submit", async (event) => {
      event.preventDefault(); showError("free-error", null); const button = event.submitter || $("free-form").querySelector("button");
      await busy(button, async () => { try { const query = new URLSearchParams({ start: $("free-start").value, end: $("free-end").value, duration_minutes: $("free-duration").value }); const result = await api(`/api/free-slots?${query}`); const target = $("free-results"); target.replaceChildren(); if (!(result.slots || []).length) { empty(target, "暂无符合条件的空闲时间", "请调整日期、时长或工作时段。", "clock"); return; } result.slots.forEach((slot) => { const row = el("div", "free-slot"); row.append(el("span", "", `${prettyDate(dateKey(slot.start))} ${formatTime(slot.start)}—${formatTime(slot.end)}`)); const use = el("button", "button is-outlined", "使用这个时段"); use.type = "button"; use.addEventListener("click", () => openEvent(null, slot)); row.append(use); target.append(row); }); } catch (error) { showError("free-error", error); } });
    });
    $("export-button").addEventListener("click", () => busy($("export-button"), async () => { try { await exportCalendar(); } catch (error) { toast(explainError(error), true); } }));
    $("task-form").addEventListener("submit", async (event) => { event.preventDefault(); showError("task-error", null); const title = $("task-title").value.trim(); if (!title) return; await busy(event.submitter || $("task-form").querySelector("button"), async () => { try { const body = { title }; if ($("task-due").value) body.due_date = $("task-due").value; await api("/api/tasks", { method: "POST", body }); $("task-form").reset(); await refreshAfterWrite("待办已添加"); } catch (error) { showError("task-error", error); } }); });
    $("memory-form").addEventListener("submit", async (event) => { event.preventDefault(); showError("memory-error", null); const content = $("memory-content").value.trim(); if (!content) return; await busy(event.submitter || $("memory-form").querySelector("button"), async () => { try { await api("/api/memories", { method: "POST", body: { content } }); $("memory-form").reset(); await refreshAfterWrite("已保存这条记忆"); } catch (error) { showError("memory-error", error); } }); });
    $("preferences-form").addEventListener("submit", async (event) => { event.preventDefault(); showError("preferences-error", null); const body = { timezone: $("pref-timezone").value.trim(), workdays: Array.from($("weekday-options").querySelectorAll("input:checked"), (node) => Number(node.value)), work_start: $("pref-work-start").value, work_end: $("pref-work-end").value, lunch_start: $("pref-lunch-start").value, lunch_end: $("pref-lunch-end").value, buffer_minutes: Number($("pref-buffer").value) }; await busy(event.submitter || $("preferences-form").querySelector("button"), async () => { try { const result = await api("/api/preferences", { method: "PUT", body }); state.prefs = result.preferences; renderPreferences(); renderStatus(); await loadData(); toast("偏好已保存"); } catch (error) { showError("preferences-error", error); } }); });
    ["api-base-url", "api-key", "api-model", "api-clear-key", "api-enabled"].forEach((id) => $(id).addEventListener("input", markAPIFormDirty));
    $("toggle-api-key").addEventListener("click", () => { const visible = $("api-key").type === "password"; $("api-key").type = visible ? "text" : "password"; $("toggle-api-key").textContent = visible ? "隐藏" : "显示"; $("toggle-api-key").setAttribute("aria-pressed", String(visible)); });
    $("model-form").addEventListener("submit", async (event) => {
      event.preventDefault(); showError("model-error", null); if (!apiConfig) { showError("model-error", {message:"请先重新加载配置。"}); return; }
      const body = {base_url:$("api-base-url").value.trim(), api_key:$("api-key").value, model:$("api-model").value.trim(), clear_key:$("api-clear-key").checked, enabled:$("api-enabled").checked, expected_version:apiConfig.version};
      await busy($("save-api-settings"), async () => { try { const result = await api("/api/model-api", {method:"PUT",body}); applyAPISettings(result.settings); $("api-test-result").hidden = true; toast("设置已保存"); } catch(error) { showError("model-error",error); } });
    });
    $("test-api-connection").addEventListener("click", async () => {
      if (!apiConfig || apiFormDirty) return; showError("model-error",null); $("api-test-result").hidden = true;
      const version = apiConfig.version; $("save-api-settings").disabled = true; $("refresh-models").disabled = true;
      await busy($("test-api-connection"),async () => { try {
        const result = await api("/api/model-api/test",{method:"POST",body:{expected_version:version}});
        if (!apiFormDirty && apiConfig.version === version) { applyAPISettings(result.settings); $("api-test-result").textContent = result.message; $("api-test-result").hidden = false; }
      } catch(error) { showError("model-error",error); if (!apiFormDirty) $("model-detail").textContent = (apiConfig.enabled ? "API 已启用" : "API 未启用") + " · 连接测试未通过"; }
      finally { $("save-api-settings").disabled = false; $("refresh-models").disabled = false; } });
      if (apiFormDirty) $("test-api-connection").disabled = true;
    });
    $("refresh-models").addEventListener("click", () => busy($("refresh-models"), loadModels));
    $("chat-project").addEventListener("change", (event) => {
      if (state.chatBusy || chatDraft.uploading) { event.target.value = state.chatProjectId || ""; return; }
      state.chatProjectId = event.target.value || null; resetChatDraft(); state.sessionAttachments=[];
      if (state.messages.length) { state.sessionRequest++; state.sessionId = null; state.messages = []; state.actions = []; $("session-select").value = ""; showError("chat-error", null); toast("已切换项目，将开始新对话"); }
      renderChat();
    });
    $("chat-form").addEventListener("submit", sendMessage);
    $("add-chat-file").addEventListener("click",()=>{if(!state.chatBusy && !chatDraft.uploading)$("chat-files").click();});
    $("chat-files").addEventListener("change",uploadChatFiles);
    $("chat-preview-dialog").addEventListener("close",()=>{previewRequest++;});
    $("chat-input").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); if (!state.chatBusy && !chatDraft.uploading && ChatFiles.message($("chat-input").value, chatDraft.items)) $("chat-form").requestSubmit(); } });
    $("new-chat").addEventListener("click", () => { if (state.chatBusy || chatDraft.uploading) return; resetChatDraft(); state.sessionAttachments=[]; previewRequest++; if($("chat-preview-dialog").open)$("chat-preview-dialog").close(); state.sessionRequest++; state.sessionId = null; state.messages = []; state.actions = []; $("session-select").value = ""; showError("chat-error", null); renderChat(); $("chat-input").focus(); });
    $("session-select").addEventListener("change", (event) => { selectSession(event.target.value).catch((error) => showError("chat-error", error)); });
    document.querySelector(".brand").addEventListener("click", (event) => { event.preventDefault(); setView("overview"); });
  }
  async function changeDate(value) { setDate(value); try { await loadData(); showError("global-error", null); } catch (error) { showError("global-error", error); } }
  async function boot() {
    decorateIcons(); bindEvents(); initializeChatLayout(); renderChatDraft(); updateChatControls(); renderChat();
    try { const bootstrap = await api("/api/bootstrap"); state.token = bootstrap.token || bootstrap.csrf_token || ""; state.status = bootstrap.status; state.prefs = bootstrap.preferences || {}; state.cloudMode = bootstrap.deployment?.mode === "cloud"; $("cloud-session").hidden = !state.cloudMode;
      $("cloud-reminder-note").hidden = bootstrap.deployment?.reminders !== "on_demand";
      knowledgeUI = new KnowledgeUI({api, el, busy, showError, toast, confirmOperation,
        onProjects(projects) { const select = $("chat-project"); select.replaceChildren(); const first = el("option", "", "不限定项目"); first.value = ""; select.append(first); projects.forEach((project) => { const option = el("option", "", project.name); option.value = project.id; select.append(option); }); if (!projects.some(p => p.id === state.chatProjectId)) state.chatProjectId = null; select.value = state.chatProjectId || ""; },
        onAsk(projectId, query) { if (state.chatBusy || chatDraft.uploading) { toast("请等待当前对话完成"); return; } resetChatDraft(); state.sessionAttachments=[]; state.sessionId = null; state.sessionRequest++; state.messages = []; state.actions = []; state.chatProjectId = projectId; $("session-select").value = ""; $("chat-project").value = projectId || ""; renderChat(); setChatCollapsed(false); $("chat-input").value = query; $("chat-input").focus(); }
      }); knowledgeUI.bind(); await knowledgeUI.load();
      integrationsUI = new IntegrationsUI({api,el,busy,showError,toast,confirmOperation,timezone,today:()=>dateKey(),addDays,refreshData:loadData,onRuntime(runtime,caldav){state.runtimeInfo=runtime;state.caldavSettings=caldav;renderStatus();setChatMode();}});
      qualityUI = new QualityUI({api,el,busy,showError,toast,openSource:id=>knowledgeUI.openSource(id),
        onFeedback(record){if(state.sessionId===record.session_id){state.feedbackByMessage[record.message_id]=record;renderChat();}if(state.view==="quality")qualityUI.load().catch(error=>showError("quality-error",error));},
        async onOpenSession(id){try{await selectSession(id);setChatCollapsed(false);}catch(error){showError("quality-error",error);}}
      }); qualityUI.bind();
      integrationsUI.bind(); await integrationsUI.load(); setDate(dateKey(state.status && state.status.current_time || new Date())); renderStatus(); renderPreferences(); await loadData(); const requestedView = location.hash.slice(1); if (VIEW_NAMES[requestedView]) setView(requestedView); await Promise.allSettled([loadModels(), loadSessions()]); }
    catch (error) { showError("global-error", error); $("backend-label").textContent = "工作台暂不可用"; $("backend-note").textContent = "请重新启动工作台后刷新页面。"; }
  }
  boot();
})();
