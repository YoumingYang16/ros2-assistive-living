"use strict";

(() => {
  const $ = id => document.getElementById(id);
  const ui = {
    input: $("command-input"), send: $("send-button"), preview: $("preview-button"),
    feedback: $("feedback"), mic: $("mic-button"), pause: $("pause-button"), stop: $("stop-button"),
  };
  const labels = {idle: "等待任务", running: "正在执行", paused: "已暂停", pausing: "正在暂停", cancelling: "正在停止", succeeded: "任务完成", failed: "任务失败", cancelled: "已停止", interrupted: "上次运行中断", skipped: "已跳过"};
  let snapshot = null, online = false, busy = false, listening = false, polling = false;
  let previousMissionState = null, pollTimer = null, voice = null;
  let currentView = "mission", configLoaded = false, configDirty = false, selectedHistory = null, pendingRequest = null, stateEpoch = 0;
  const sessionId = (() => { try { let id = sessionStorage.getItem("voice-patrol-session"); if (!id) { id = crypto.randomUUID(); sessionStorage.setItem("voice-patrol-session", id); } return id; } catch (_) { return crypto.randomUUID(); } })();
  let utterance = null;
  let boundLivingPreview = null;
  const number = value => typeof value === "number" && Number.isFinite(value);
  const activeStates = new Set(["running", "paused", "pausing", "cancelling"]);
  const setText = (id, value) => { $(id).textContent = String(value ?? "—"); };

  function feedback(message, kind = "") {
    ui.feedback.textContent = message;
    ui.feedback.className = `feedback ${kind}`;
  }
  function updateButtons() {
    const hasText = ui.input.value.trim().length > 0;
    ui.send.disabled = !online || busy || listening || !hasText;
    ui.preview.disabled = !online || busy || listening || !hasText;
    const state = snapshot?.state || "idle";
    ui.pause.disabled = !online || busy || !["running", "paused"].includes(state);
    ui.pause.textContent = state === "paused" ? "继续任务" : "暂停任务";
    ui.stop.disabled = !online || !activeStates.has(state) || state === "cancelling";
    $("config-save").disabled = !online || activeStates.has(state) || !configLoaded;
    $("character-count").textContent = `${ui.input.value.length} / 500`;
  }
  function setConnection(connected) {
    online = connected;
    $("connection-dot").className = `dot ${connected ? "connected" : "disconnected"}`;
    setText("connection-label", connected ? "服务已连接" : "服务连接中断");
    if (!connected) {
      setText("status-label", "连接中断 · 数据可能过期");
      setText("pose-label", "连接中断，暂停更新位置");
    }
    updateButtons();
  }
  async function api(path, payload, {method = "POST", raw = false, timeoutMs = 6500} = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const options = {signal: controller.signal, headers: {"Accept": "application/json"}, cache: "no-store"};
      if (payload !== undefined) {
        options.method = method;
        options.headers["Content-Type"] = raw ? "audio/wav" : "application/json";
        options.body = raw ? payload : JSON.stringify(payload);
      }
      const response = await fetch(path, options);
      let data;
      try { data = await response.json(); } catch (_) { throw new Error("服务未返回有效的 JSON 数据，请检查服务地址。"); }
      if (!response.ok || data.ok === false) { const error = new Error(data.message || `请求失败（${response.status}）`); error.definite = response.status < 500 && response.status !== 408; error.status = response.status; throw error; }
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("请求超时。请先刷新状态，确认是否已执行，再决定是否重试。");
      throw error;
    } finally { clearTimeout(timeout); }
  }
  function locationLabel(id) {
    if (!id) return "未提供";
    return snapshot?.locations?.[id]?.label || String(id);
  }
  function stepLabel(step) {
    const kind = step?.kind || "unknown";
    if (["navigate", "go", "navigation", "goto"].includes(kind)) return `前往${locationLabel(step.target)}`;
    if (["return", "return_home"].includes(kind)) return `返回${locationLabel(step.target || "home")}`;
    if (["wait", "sleep"].includes(kind)) return `等待 ${step.seconds ?? 0} 秒`;
    if (["inspect", "check", "detect", "search"].includes(kind)) return `检查${step.object_name ? `：${step.object_name}` : "周围环境"}${step.target ? `（${locationLabel(step.target)}）` : ""}`;
    return step?.description || step?.summary || `${kind}${step.target ? ` · ${locationLabel(step.target)}` : ""}`;
  }
  function conditionLabel(step) {
    if (!step?.condition) return "";
    const outcomes = {found: "找到目标", not_found: "未找到目标", inconclusive: "无法确认", succeeded: "成功"};
    function describe(value) { if (value.all) return `全部满足（${value.all.map(describe).join("；")}）`; if (value.any) return `任一满足（${value.any.map(describe).join("；")}）`; if (value.not) return `不满足（${describe(value.not)}）`; return `步骤 ${value.step_id} ${outcomes[value.outcome] || value.outcome}`; }
    return `条件：${describe(step.condition)}时执行`;
  }
  function speak(message) {
    if (!$("speech-toggle").checked || !window.speechSynthesis || !message) return;
    // Invalidate the old utterance before cancel(), which can synchronously
    // deliver its completion event and otherwise resume listening too early.
    utterance = null;
    voice?.speechStarted();
    window.dispatchEvent(new CustomEvent("patrol:speaking"));
    window.speechSynthesis.cancel();
    utterance = new SpeechSynthesisUtterance(String(message));
    utterance.lang = "zh-CN";
    utterance.rate = window.AssistiveUI?.getSnapshot()?.profile?.speech_rate || 1;
    const current = utterance;
    const finish = () => { if (utterance === current) { utterance = null; voice?.speechEnded(); } };
    utterance.onerror = finish;
    utterance.onend = finish;
    window.speechSynthesis.speak(utterance);
  }
  function renderTimeline(mission, state) {
    const list = $("timeline");
    list.replaceChildren();
    if (!mission || !Array.isArray(mission.steps) || !mission.steps.length) {
      const empty = document.createElement("li");
      empty.className = "timeline-empty";
      empty.textContent = "任务开始后，这里会逐步展示导航、等待和检查结果。";
      list.append(empty);
      return;
    }
    const current = Number.isInteger(mission.step_index) ? mission.step_index : 0;
    mission.steps.forEach((step, index) => {
      const li = document.createElement("li");
      const results = Array.isArray(mission.results) ? mission.results : [];
      const result = results.find(item => item && (item.step_index === index || item.index === index)) || results[index];
      const failed = result?.success === false || result?.status === "failed" || (index === current && state === "failed");
      if (result?.status === "skipped") li.className = "skipped";
      else if (failed) li.className = "failed";
      else if (state === "succeeded" || index < current || result?.success === true || result?.status === "succeeded") li.className = "done";
      else if (index === current && activeStates.has(state)) li.className = "active";
      li.append(document.createTextNode(stepLabel(step)));
      if (step.condition) { const condition = document.createElement("span"); condition.className = "branch-note"; condition.textContent = conditionLabel(step); li.append(condition); }
      const detail = document.createElement("span");
      detail.className = "step-detail";
      let message = result?.message || result?.detail || result?.error;
      if (result?.status === "skipped") message = `已跳过 · ${message || "条件未满足，未执行该步骤"}`;
      if (message && typeof message === "object") message = JSON.stringify(message);
      if (!message && index === current && state === "failed") message = mission.error;
      if (!message && index === current && activeStates.has(state)) message = state === "paused" ? "任务已暂停，等待继续" : state === "pausing" ? "等待当前动作停止" : "正在执行此步骤";
      if (message) { detail.textContent = message; li.append(detail); }
      list.append(li);
    });
  }
  function renderEvents(events) {
    const list = $("event-list");
    list.replaceChildren();
    if (!Array.isArray(events) || !events.length) {
      const p = document.createElement("p"); p.className = "empty-log"; p.textContent = "等待第一条任务事件。"; list.append(p); return;
    }
    [...events].slice(-50).reverse().forEach(event => {
      const row = document.createElement("div");
      row.className = "event-item";
      const level = String(event.level || "info").toLowerCase();
      if (["error", "warning", "warn"].includes(level)) row.classList.add(level === "warn" ? "warning" : level);
      const time = document.createElement("time");
      const rawTime = event.time ?? event.timestamp;
      const date = new Date(number(rawTime) && rawTime < 1e12 ? rawTime * 1000 : rawTime);
      time.textContent = Number.isNaN(date.getTime()) ? "—" : date.toLocaleTimeString("zh-CN", {hour12: false});
      if (!Number.isNaN(date.getTime())) time.dateTime = date.toISOString();
      const message = document.createElement("p");
      message.textContent = event.message || "状态更新";
      row.append(time, message); list.append(row);
    });
  }
  function renderReport(mission) {
    const box = $("mission-report");
    box.replaceChildren();
    const report = mission?.report;
    box.hidden = !report;
    if (!report) return;
    const title = document.createElement("strong");
    title.textContent = `${report.simulated ? "模拟任务报告" : "任务报告"} · ${report.completed_steps ?? 0} 步完成${report.skipped_steps ? ` / ${report.skipped_steps} 步跳过` : ` / 共 ${report.total_steps ?? 0} 步`}`;
    const message = document.createElement("p");
    message.textContent = report.message || labels[report.status] || "任务已结束";
    box.append(title, message);
    if (report.goal_outcome) box.append(element("p", "goal-result", `执行结果：${labels[report.status] || report.status} · 目标结果：${{achieved:"已达成",not_achieved:"未达成",unknown:"无法确认",not_applicable:"不适用"}[report.goal_outcome] || report.goal_outcome}`));
    for (const observation of report.observations || []) {
      const entry = document.createElement("p");
      const date = new Date(observation.observed_at);
      const observedAt = Number.isNaN(date.getTime()) ? "观测时间未提供" : date.toLocaleString("zh-CN", {hour12: false});
      const evidence = observation.evidence && typeof observation.evidence === "object" ? JSON.stringify(observation.evidence) : observation.evidence;
      entry.textContent = `${locationLabel(observation.target)}：${observation.message || "已记录观测"}。观测时间：${observedAt}。来源：${evidence || (observation.simulated ? "模拟数据" : "外部检测接口")}。`;
      box.append(entry);
    }
  }
  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }
  function displayTime(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", {hour12: false});
  }
  function showClarification(data) {
    const required = data.needs_clarification || data.needs_confirmation || data.kind === "clarify";
    $("clarification").hidden = !required;
    if (!required) return false;
    setText("clarification-message", data.message || "请补充任务信息。");
    const options = $("clarification-options"); options.replaceChildren();
    for (const [index,choice] of (data.options || []).entries()) {
      const button = element("button", "", data.option_labels?.[index] || choice);
      button.type = "button";
      button.addEventListener("click", () => { ui.input.value = String(choice); resetPreview(); void submit("/api/command", {text: String(choice)}); });
      options.append(button);
    }
    $("plan-preview").hidden = true;
    return true;
  }
  let previousRecoveries = "";
  function renderRecoveries(recoveries) {
    const items = Array.isArray(recoveries) ? recoveries : [];
    const serial = JSON.stringify(items);
    if (previousRecoveries === serial) return;
    previousRecoveries = serial;
    const box = $("recovery-banner"); box.replaceChildren(); box.hidden = !items.length;
    if (!items.length) return;
    box.append(element("strong", "", `发现 ${items.length} 条上次运行中断的任务`), element("p", "", "系统没有自动重新执行。请查看档案确认执行结果，再决定是否创建新的任务。"));
    for (const item of items) {
      const id = typeof item === "string" ? item : item.id || item.mission_id;
      const button = element("button", "", `已了解 · ${String(id).slice(0, 8)}`); button.type = "button";
      button.addEventListener("click", async () => {
        button.disabled = true;
        try { await api("/api/recovery", {mission_id: id, action: "dismiss"}); await refresh(); }
        catch (error) { feedback(error.message, "error"); button.disabled = false; }
      });
      box.append(button);
    }
  }
  async function loadHistory() {
    const list = $("history-list");
    try {
      const query=new URLSearchParams({limit:"50"});
      for(const key of ["query","state","since","until","archived"]){let value=$("history-filter-"+key)?.value;if(value&&(key==="since"||key==="until"))value=new Date(value).toISOString();if(value)query.set(key,value);}
      const data = await api(`/api/history?${query}`);
      const missions = data.missions || data.history || [];
      setText("history-count", `${data.total ?? missions.length} 条记录`);
      list.replaceChildren();
      if (!missions.length) list.append(element("p", "empty-log", "还没有任务记录。完成第一条任务后，它会保存在这里。"));
      for (const mission of missions) {
        const id = mission.id || mission.mission_id;
        const button = element("button", `history-item ${selectedHistory === id ? "selected" : ""}`); button.type = "button";
        button.append(element("strong", "", mission.summary || mission.command || `任务 ${String(id).slice(0, 8)}`));
        const meta = element("span", "history-meta");
        meta.append(element("span", "", displayTime(mission.started_at || mission.created_at)), element("span", "", labels[mission.state || mission.status] || mission.state || mission.status || "—"));
        button.append(meta); button.addEventListener("click", () => void loadHistoryDetail(id)); list.append(button);
      }
    } catch (error) { list.replaceChildren(element("p", "panel-error", error.message)); }
  }
  async function loadHistoryDetail(id) {
    const box = $("history-detail"); selectedHistory = id;
    try {
      const data = await api(`/api/missions/${encodeURIComponent(id)}`);
      const mission = data.mission || data;
      box.replaceChildren();
      box.append(element("h3", "detail-title", mission.summary || mission.command || "任务记录"));
      const state = mission.state || mission.status || mission.report?.status;
      box.append(element("p", "detail-meta", `${labels[state] || state || "—"} · ${displayTime(mission.started_at)}\n任务编号：${mission.id || id}`));
      if (mission.error) box.append(element("p", "detail-error", mission.error));
      if (mission.archived_at || mission.archived) { const undo=element("button","button secondary compact","取消软归档");undo.onclick=async()=>{try{await api("/api/data/unarchive",{mission_ids:[id]});await loadHistory();await loadHistoryDetail(id);}catch(error){feedback(error.message,"error");}};box.append(undo); }
      const results = mission.results || [];
      for (const [index, step] of (mission.steps || []).entries()) {
        const result = results.find(item => item.step_id && item.step_id === step.step_id || item.step_index === index) || {};
        const row = element("div", `detail-step ${result.status === "skipped" ? "skipped" : ""}`);
        row.append(document.createTextNode(`${step.step_id || index + 1} · ${stepLabel(step)}`), element("span", "quiet-tag", labels[result.status] || result.status || "未执行"));
        if (step.condition) row.append(element("span", "branch-note", conditionLabel(step)));
        if (result.message) row.append(element("p", "", result.message));
        if (result.observed_at) row.append(element("p", "", `观测时间：${displayTime(result.observed_at)} · 来源：${result.evidence || (result.simulated ? "预设模拟数据" : "外部接口")}`));
        box.append(row);
      }
      const raw = element("details", "raw-details"); raw.append(element("summary", "", "查看完整记录与事件"), element("pre", "", JSON.stringify(data, null, 2))); box.append(raw);
      const exportLink = $("history-export"); exportLink.hidden = false; exportLink.href = `/api/missions/${encodeURIComponent(id)}/export`; exportLink.download = `mission-${id}.json`;
      document.querySelectorAll(".history-item").forEach(item => item.classList.remove("selected"));
    } catch (error) { box.replaceChildren(element("p", "panel-error", error.message)); $("history-export").hidden = true; }
  }
  function renderConfigOverview(config) {
    const places = $("config-locations"); places.replaceChildren();
    const locations = config.locations || {};
    setText("config-location-count", `${Object.keys(locations).length} 个地点`);
    for (const [id, location] of Object.entries(locations)) {
      const row = element("div", "location-card"), name = element("div", "");
      name.append(element("strong", "", location.label || id), element("p", "", (location.aliases || []).join(" / ") || id));
      row.append(name, element("code", "", `x ${location.x} / y ${location.y}\nyaw ${location.yaw ?? 0}`)); places.append(row);
    }
    const routes = $("config-routes"); routes.replaceChildren();
    for (const [id, route] of Object.entries(config.patrol_routes || config.routes || {})) {
      const row = element("div", "route-card"); const points = Array.isArray(route) ? route : route.waypoints || route.locations || [];
      row.append(element("strong", "", route.label || id), document.createTextNode(points.map(point => locations[point]?.label || point).join(" → "))); routes.append(row);
    }
    if (!routes.children.length) routes.append(element("p", "empty-log", "未配置巡逻路线。"));
  }
  async function loadConfig() {
    try {
      const data = await api("/api/config"); const config = data.config || data;
      $("config-editor").value = JSON.stringify(config, null, 2); $("config-editor").setSelectionRange(0, 0); $("config-editor").scrollTop = 0; $("config-editor").scrollLeft = 0; configLoaded = true; configDirty = false;
      renderConfigOverview(config); setText("config-status", "已加载服务配置"); setText("config-feedback", ""); updateButtons();
    } catch (error) { setText("config-feedback", error.message); $("config-feedback").className = "feedback error"; }
  }
  async function saveConfig() {
    try {
      setText("config-feedback", "正在校验并保存配置…"); $("config-feedback").className = "feedback";
      const config = JSON.parse($("config-editor").value);
      if (!config || Array.isArray(config) || typeof config !== "object") throw new Error("配置必须为 JSON 对象。");
      $("config-save").disabled = true;
      await api("/api/config", config, {method: "PUT"});
      await loadConfig(); await refresh();
      setText("config-feedback", "配置校验通过，已保存。新任务将使用更新后的配置。"); $("config-feedback").className = "feedback success";
    } catch (error) { setText("config-feedback", error.message); $("config-feedback").className = "feedback error"; }
    finally { updateButtons(); }
  }
  async function loadHealth() {
    try {
      const [health, metrics] = await Promise.all([api("/api/health"), api("/api/metrics")]);
      const grid = $("health-summary"); grid.replaceChildren();
      const tiles = [
        ["运行接口", health.mode === "mock" ? "软件模拟" : "ROS 2", health.mode === "mock" ? "验证逻辑，不代表真实机器人" : "由真实 ROS 2 服务提供能力", "muted"],
        ["任务存储", health.database?.available ? "已连接" : "不可用", health.database?.persistent ? "SQLite 持久化记录" : "当前为临时记录", health.database?.available ? "good" : "warn"],
        ["规划方式", health.planner?.model_configured ? "模型已配置" : "本地规则", health.planner?.provider || "澄清与受约束的任务组合", "muted"],
        ["待确认恢复", Number(health.recovery_required || 0), "中断任务不会自动重放", health.recovery_required ? "warn" : "good"],
      ];
      for (const [label, value, hint, style] of tiles) { const tile = element("div", `health-tile ${style}`); tile.append(element("span", "tile-label", label), element("strong", "", value), element("p", "", hint)); grid.append(tile); }
      const details = $("health-details"); details.replaceChildren();
      const rows = [["任务状态", labels[health.state] || health.state], ["服务运行时间", `${Math.floor(health.uptime_seconds || 0)} 秒`], ["数据库版本", health.database?.schema_version ?? metrics.schema_version ?? "—"], ["位置有效", health.robot?.pose_valid ? "是" : "未确认"], ["当前位置", health.robot?.location ? locationLabel(health.robot.location) : "未确认"], ["本地规则", health.planner?.rules_available ? "可用" : "未确认"]];
      const rosHealth = health.robot?.health;
      if (health.hardware_interlock) rows.push(["机械执行互锁", ({pending:"动作终态待确认",unknown:"未知状态，已锁止",resolved:"已核实"})[health.hardware_interlock.status] || "状态异常"], ["互锁说明", health.hardware_interlock.reason || "需设备接入人员核实"]);
      if (rosHealth) {
        const ready = value => value === true ? "就绪" : value === false ? "未就绪" : "未确认";
        rows.push(["ROS 执行器", rosHealth.executor_alive ? "运行中" : "未运行"], ["导航接口", ready(rosHealth.navigation_ready)], ["检查接口", ready(rosHealth.inspection_ready)], ["感知后端", rosHealth.perception_backend || "未配置"], ["运动是否受阻", rosHealth.blocked ? "受阻 / 停止未确认" : "未报告阻塞"], ["位置数据年龄", number(rosHealth.pose_age_seconds) ? `${rosHealth.pose_age_seconds.toFixed(2)} 秒` : "未提供"]);
        if (rosHealth.last_error) rows.push(["最近接口错误", `${rosHealth.last_error.code || ""} ${rosHealth.last_error.message || ""}`]);
        if (rosHealth.executor_error) rows.push(["执行器异常", rosHealth.executor_error]);
      }
      if (health.robot?.action) rows.push(["当前 ROS 动作", `${health.robot.action.kind || "—"} / ${health.robot.action.phase || "—"}`], ["取消确认", health.robot.action.cancel_response || "尚无确认记录"]);
      for (const [key, value] of rows) { const row = element("div", "diagnostic-row"); row.append(element("dt", "", key), element("dd", "", value ?? "—")); details.append(row); }
      const raw = element("details", "raw-details"); raw.append(element("summary", "", "完整健康状态"), element("pre", "", JSON.stringify(health, null, 2))); details.append(raw);
      const metricBox = $("metrics-grid"); metricBox.replaceChildren();
      const stat = metrics.missions_by_state || {};
      const cards = [["记录任务", metrics.missions_total ?? 0], ["成功任务", stat.succeeded ?? 0], ["失败 / 中断", (stat.failed || 0) + (stat.interrupted || 0)], ["观测记录", metrics.observations_total ?? 0], ["请求记录", metrics.requests_total ?? 0], ["已停止", stat.cancelled ?? 0]];
      for (const [label, value] of cards) { const card = element("div", "metric-card"); card.append(element("strong", "", value), element("span", "", label)); metricBox.append(card); }
    } catch (error) { $("health-summary").replaceChildren(element("p", "panel-error", error.message)); }
  }
  function switchView(view) {
    if (!["mission", "history", "settings", "health", "queue", "workflows", "memory", "recovery", "data", "lifecycle", "scenarios", "assistive"].includes(view)) view = "mission";
    if (view !== "mission" && voice?.active) voice.stop();
    currentView = view;
    document.querySelectorAll(".workspace-view").forEach(node => { node.hidden = node.id !== `view-${view}`; });
    document.querySelectorAll("[data-view]").forEach(node => { node.classList.toggle("active", node.dataset.view === view); if (node.dataset.view === view) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current"); });
    setText("view-label", {mission: "任务控制台", history: "任务档案", settings: "地点与配置", health: "系统状态",queue:"队列与调度",workflows:"任务编排",memory:"观测记忆",recovery:"恢复评估",data:"数据管理",lifecycle:"能力与生命周期",scenarios:"场景验证",assistive:"生活辅助"}[view]);
    if (view === "history") void loadHistory();
    if (view === "settings" && !configDirty) void loadConfig();
    if (view === "health") void loadHealth();
    window.dispatchEvent(new CustomEvent("patrol:view", {detail:view}));
  }
  function svgElement(tag, attrs = {}, text) {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [name, value] of Object.entries(attrs)) element.setAttribute(name, String(value));
    if (text !== undefined) element.textContent = text;
    return element;
  }
  function renderDiagram(data) {
    const svg = $("location-diagram");
    svg.replaceChildren();
    const points = Object.entries(data.locations || {}).filter(([, point]) => number(point.x) && number(point.y));
    $("diagram-empty").hidden = points.length > 0;
    if (!points.length) return;
    const robot = data.robot || {};
    const validPose = number(robot.x) && number(robot.y) && (data.mode === "mock" || robot.pose_valid === true || robot.pose_available === true);
    const coordinates = points.map(([, point]) => point);
    if (validPose) coordinates.push(robot);
    const xs = coordinates.map(p => p.x), ys = coordinates.map(p => p.y);
    const xMin = Math.min(...xs), xMax = Math.max(...xs), yMin = Math.min(...ys), yMax = Math.max(...ys);
    const xRange = Math.max(2, xMax - xMin), yRange = Math.max(2, yMax - yMin);
    const scale = Math.min(430 / xRange, 215 / yRange);
    const centerX = (xMin + xMax) / 2, centerY = (yMin + yMax) / 2;
    const position = p => [300 + (p.x - centerX) * scale, 168 - (p.y - centerY) * scale];
    const planned = (data.mission?.steps || []).filter(step => ["navigate", "go", "goto", "navigation", "return", "return_home"].includes(step.kind)).map(step => data.locations?.[step.target]).filter(p => p && number(p.x) && number(p.y));
    if (planned.length > 1) svg.append(svgElement("polyline", {points: planned.map(p => position(p).join(",")).join(" "), stroke: "#b2a5de", "stroke-width": 1.5, "stroke-dasharray": "6 6", fill: "none", opacity: .8}));
    points.forEach(([id, point]) => {
      const [x, y] = position(point);
      const target = data.mission?.steps?.[data.mission?.step_index]?.target === id && activeStates.has(data.state);
      if (target) svg.append(svgElement("circle", {cx: x, cy: y, r: 23, stroke: "#d5ccef", "stroke-dasharray": "3 5", fill: "#f4f0fc"}));
      svg.append(svgElement("circle", {cx: x, cy: y, r: 7, stroke: target ? "#9a86d5" : "#afb8c6", "stroke-width": 1.7, fill: "#fff"}));
      svg.append(svgElement("circle", {cx: x, cy: y, r: 2, stroke: "none", fill: target ? "#9a86d5" : "#bfc5d1"}));
      svg.append(svgElement("text", {x, y: y + 29, fill: "#778396", stroke: "none", "text-anchor": "middle", "font-size": 12}, point.label || id));
    });
    if (validPose) {
      const [x, y] = position(robot);
      svg.append(svgElement("circle", {cx: x, cy: y, r: 16, fill: "#e2daf8", stroke: "none", opacity: .72}));
      svg.append(svgElement("circle", {cx: x, cy: y, r: 8.5, fill: "#8c77d2", stroke: "#fff", "stroke-width": 2}));
      const angle = number(robot.yaw) ? robot.yaw : 0;
      svg.append(svgElement("line", {x1: x, y1: y, x2: x + Math.cos(angle) * 17, y2: y - Math.sin(angle) * 17, stroke: "#8c77d2", "stroke-width": 2.5}));
      setText("pose-label", `${data.mode === "mock" ? "模拟位置" : "实时位置"} · x ${robot.x.toFixed(2)} / y ${robot.y.toFixed(2)} m`);
    } else setText("pose-label", "实时位置未确认 · 仅显示预设地点");
    svg.append(svgElement("text", {x: 23, y: 323, fill: "#aeb5c2", stroke: "none", "font-size": 9}, `坐标单位：m   ·   参考比例 ${Math.min(430, scale).toFixed(0)} px/m`));
  }
  function render(data) {
    if (!data || typeof data !== "object") return;
    snapshot = data;
    setConnection(true);
    const state = data.state || "idle", mission = data.mission;
    const isMock = data.mode === "mock";
    setText("mode-pill", isMock ? "软件模拟 · MOCK" : "ROS 2 · 接口模式");
    $("mode-pill").className = `mode-pill ${isMock ? "" : "ros2"}`;
    setText("mode-detail", isMock ? "本地软件模拟" : "ROS 2 服务连接");
    setText("status-label", labels[state] || state);
    setText("location-label", data.robot?.location ? locationLabel(data.robot.location) : "位置未确认");
    const total = mission?.steps?.length || 0;
    const current = Math.max(0, Math.min(total, Number(mission?.step_index) || 0));
    const done = state === "succeeded" ? total : Array.isArray(mission?.results) ? Math.min(total, mission.results.length) : current;
    const skipped = (mission?.results || []).filter(result => result.status === "skipped").length;
    setText("progress-label", total ? `${done} / ${total} 步已处理${skipped ? ` · ${skipped} 步跳过` : ""}` : "尚未开始");
    setText("mission-summary", mission?.summary || mission?.command || "下一段旅程，从一句指令开始。");
    setText("mission-id", mission?.id ? `任务 ${String(mission.id).slice(0, 10)}` : "无活动任务");
    setText("mission-state", labels[state] || state);
    $("mission-state").className = `mission-state ${state}`;
    const percent = total ? Math.round(done / total * 100) : 0;
    $("progress-fill").style.width = `${percent}%`;
    $("progress-track").setAttribute("aria-valuenow", String(percent));
    setText("diagram-note", isMock ? "软件模拟位置；虚线为候选任务顺序，条件分支可能跳过，并非避障路线。" : "配置坐标示意；虚线含可能跳过的分支，并非真实障碍地图或导航路径。");
    setText("capability-note", isMock ? "当前是软件模拟：导航位置与检查结果均由模拟后端提供，用于验证指令和任务流程。" : "当前使用 ROS 2 接口：导航需要 Nav2；物体检查需要检测节点提供新鲜的观测结果。未接入的能力不会伪造成功。");
    renderTimeline(mission, state); renderReport(mission); renderDiagram(data); renderEvents(data.events); renderRecoveries(data.recoveries); updateButtons();
    if (data.planner) setText("planner-note", data.planner.model_configured ? "模型辅助理解已配置，计划仍通过技能与参数校验。地点不明确时先澄清，条件执行依据观测结果。" : "当前使用本地中文规则与对话澄清；条件分支依据观测结果执行。不支持任意开放式任务。");
    const missionState = `${mission?.id || "none"}:${state}`;
    if (previousMissionState !== null && previousMissionState !== missionState && ["succeeded", "failed", "cancelled"].includes(state)) speak(`${labels[state]}${mission?.error ? `，${mission.error}` : ""}`);
    previousMissionState = missionState;
    window.dispatchEvent(new CustomEvent("patrol:state", {detail:data}));
  }
  async function refresh() {
    if (polling) return;
    polling = true;
    const epoch = stateEpoch;
    try { const data = await api("/api/state"); if (epoch === stateEpoch) render(data); }
    catch (_) { setConnection(false); }
    finally { polling = false; }
  }
  async function submit(path, payload, {priority = false} = {}) {
    if (busy && !priority) { feedback("正在处理上一条请求，请稍候。", "error"); return; }
    if (!priority) busy = true;
    const epoch = ++stateEpoch;
    if (path === "/api/command") {
      if (boundLivingPreview?.text === payload.text) payload = {...payload, text: boundLivingPreview.command};
      if (!pendingRequest || pendingRequest.text !== payload.text) pendingRequest = {text: payload.text, request_id: crypto.randomUUID()};
      payload = {...payload, request_id: pendingRequest.request_id, session_id: sessionId};
    }
    updateButtons();
    try {
      const response = await api(path, payload);
      if (path === "/api/command") pendingRequest = null;
      feedback(response.message || "指令已接收。", "success");
      showClarification(response);
      if (response.state && epoch === stateEpoch) render(response.state); else await refresh();
      speak(response.message || "指令已接收");
      $("plan-preview").hidden = true;
    } catch (error) {
      if (path === "/api/command" && error.definite) pendingRequest = null;
      feedback(error.message || "发送失败，请检查服务连接。", "error");
      await refresh();
    } finally { if (!priority) busy = false; updateButtons(); }
  }
  function resetPreview() { boundLivingPreview = null; $("plan-preview").hidden = true; updateButtons(); }
  ui.input.addEventListener("input", resetPreview);
  ui.input.addEventListener("keydown", event => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && !ui.send.disabled) { event.preventDefault(); ui.send.click(); }
  });
  document.querySelectorAll("[data-command]").forEach(button => button.addEventListener("click", () => {
    ui.input.value = button.dataset.command; resetPreview(); ui.input.focus(); feedback("指令已填入。可修改文字，再预览或发送。");
  }));
  ui.send.addEventListener("click", () => { if (!ui.send.disabled) void submit("/api/command", {text: ui.input.value.trim()}); });
  ui.pause.addEventListener("click", () => { if (!ui.pause.disabled) void submit("/api/control", {action: snapshot?.state === "paused" ? "resume" : "pause"}); });
  ui.stop.addEventListener("click", () => { if (!ui.stop.disabled) void submit("/api/control", {action: "stop"}, {priority: true}); });
  ui.preview.addEventListener("click", async () => {
    if (ui.preview.disabled) return;
    const requestedText = ui.input.value.trim();
    boundLivingPreview = null;
    busy = true; updateButtons();
    try {
      const data = await api("/api/plan", {text: requestedText, session_id: sessionId});
      if (ui.input.value.trim() !== requestedText) {
        $("plan-preview").hidden = true;
        feedback("指令文字已修改，请重新预览计划。");
        return;
      }
      if (showClarification(data)) { feedback(data.message || "请先补充信息。"); return; }
      if (data.execution_command) boundLivingPreview = {text: requestedText, command: data.execution_command};
      const plan = data.plan || {};
      setText("preview-summary", plan.summary || data.message || (data.kind !== "task" ? "即时控制指令" : "准备执行以下步骤"));
      const list = $("preview-steps"); list.replaceChildren();
      (plan.steps || []).forEach(step => { const li = document.createElement("li"); li.textContent = stepLabel(step); if (step.condition) li.append(element("span", "branch-note", conditionLabel(step))); list.append(li); });
      if (!list.children.length) { const li = document.createElement("li"); li.textContent = `处理内容：${data.kind === "assistive" ? data.message : plan.action || data.action || ui.input.value.trim()}`; list.append(li); }
      $("plan-preview").hidden = false; feedback("计划已解析，尚未执行。确认后点击“发送任务”。", "success");
    } catch (error) { $("plan-preview").hidden = true; feedback(error.message, "error"); }
    finally { busy = false; updateButtons(); }
  });
  $("refresh").addEventListener("click", () => { void refresh(); if (currentView === "health") void loadHealth(); if (currentView === "history") void loadHistory(); });
  $("speech-toggle").addEventListener("change", event => {
    if (!window.speechSynthesis) { event.target.checked = false; feedback("当前浏览器不支持语音播报。", "error"); return; }
    if (event.target.checked) speak("语音反馈已开启"); else { window.speechSynthesis.cancel(); utterance = null; voice?.speechEnded(); }
  });
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  voice = new window.VoiceController({
    Recognition: SpeechRecognition,
    onState: ({state, active, handsfree, message}) => {
      listening = ["starting", "listening"].includes(state);
      ui.mic.classList.toggle("listening", listening); ui.mic.setAttribute("aria-pressed", String(active));
      setText("mic-label", active ? "结束语音输入" : "点击说话");
      const live = $("voice-live"); live.className = `voice-live ${active ? "active" : ""} ${state === "speaking" ? "speaking" : ""}`;
      live.textContent = active ? message || (handsfree ? "连续语音开启" : "单次语音开启") : "麦克风未开启";
      $("handsfree-toggle").checked = active && handsfree;
      updateButtons();
    },
    onTranscript: ({text, final}) => {
      ui.input.value = text.slice(0, 500); resetPreview();
      $("voice-transcript").hidden = !text;
      setText("voice-transcript", `${final ? "识别文字" : "正在识别"}：${text}`);
    },
    onError: message => feedback(message, "error"),
    onFinal: async (text, {autoSend, priority}) => {
      if (!autoSend) { feedback("识别完成。请检查文字，确认后再发送。"); return; }
      if (priority) { await submit("/api/control", {action: "stop"}, {priority: true}); return; }
      await submit("/api/command", {text: text.slice(0, 500)});
    },
  });
  if (!SpeechRecognition) {
    ui.mic.disabled = true;
    $("handsfree-toggle").disabled = true;
    setText("voice-hint", "当前浏览器不支持语音识别。可直接输入文字、上传 WAV，或使用离线 Vosk 客户端。");
  }
  ui.mic.addEventListener("click", () => { if (voice.active) voice.stop(); else { window.speechSynthesis?.cancel(); utterance = null; voice.start(); } });
  $("handsfree-toggle").addEventListener("change", event => {
    if (event.target.checked) { window.speechSynthesis?.cancel(); utterance = null; voice.start({handsfree: true}); feedback("连续语音已开启，完整识别的指令会自动发送；停止指令优先处理。"); }
    else voice.stop();
  });
  $("wav-input").addEventListener("change", async event => {
    const file = event.target.files?.[0]; if (!file) return;
    voice.stop(); window.speechSynthesis?.cancel(); utterance = null;
    if (file.size > 10 * 1024 * 1024) { setText("wav-status", "文件超过 10 MiB，请选择较短的 PCM WAV。"); event.target.value = ""; return; }
    event.target.disabled = true; setText("wav-status", "正在由本地服务识别…");
    try {
      const result = await api("/api/voice/transcribe", await file.arrayBuffer(), {raw: true, timeoutMs: 90000});
      ui.input.value = String(result.text || "").slice(0, 500); resetPreview();
      setText("wav-status", result.text ? `${result.engine || "离线引擎"} · ${result.duration_seconds ?? "—"} 秒 · 已识别` : "没有识别到文字，请检查录音内容。");
      feedback(result.text ? "WAV 已转为文字，尚未执行。请检查后点击发送。" : "音频中未识别到有效文字。", result.text ? "success" : "error");
    } catch (error) { setText("wav-status", error.message); feedback(error.message, "error"); }
    finally { event.target.disabled = false; event.target.value = ""; }
  });
  document.querySelectorAll("[data-view]").forEach(node => node.addEventListener("click", event => { event.preventDefault(); if (location.hash === `#${node.dataset.view}`) switchView(node.dataset.view); else location.hash = node.dataset.view; }));
  window.addEventListener("hashchange", () => switchView(location.hash.slice(1)));
  $("history-refresh").addEventListener("click", () => void loadHistory());
  $("health-refresh").addEventListener("click", () => void loadHealth());
  $("config-reload").addEventListener("click", () => void loadConfig());
  $("config-save").addEventListener("click", () => void saveConfig());
  $("config-editor").addEventListener("input", () => { configDirty = true; setText("config-status", "有未保存的修改"); });
  $("config-format").addEventListener("click", () => {
    try { const config = JSON.parse($("config-editor").value); $("config-editor").value = JSON.stringify(config, null, 2); renderConfigOverview(config); setText("config-feedback", "JSON 格式正确；完整配置校验将在保存时进行。"); $("config-feedback").className = "feedback success"; }
    catch (error) { setText("config-feedback", `JSON 语法错误：${error.message}`); $("config-feedback").className = "feedback error"; }
  });
  window.PatrolUI = {sessionId,api,feedback,refresh,locationLabel,switchView,loadHistory,getSnapshot:()=>snapshot,stopVoice:()=>voice.stop(),speak};
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { voice.stop(); window.speechSynthesis?.cancel(); utterance = null; }
    if (!document.hidden) void refresh();
  });
  window.addEventListener("beforeunload", () => { clearInterval(pollTimer); voice.stop(); window.speechSynthesis?.cancel(); });
  updateButtons();
  void refresh();
  switchView(location.hash.slice(1));
  let ticks = 0;
  pollTimer = setInterval(() => { if (!document.hidden) { void refresh(); if (++ticks % 5 === 0 && currentView === "health") void loadHealth(); } }, 1000);
})();
