"use strict";
(() => {
  const U = window.PatrolUI;
  const $ = id => document.getElementById(id);
  const node = (tag, cls, text) => {
    const value = document.createElement(tag);
    if (cls) value.className = cls;
    if (text !== undefined) value.textContent = text;
    return value;
  };
  const root = node("section", "workspace-view");
  root.id = "view-scenarios";
  root.hidden = true;
  root.innerHTML = `
    <div class="page-heading"><div><p class="eyebrow">TRY THE BRANCHES.</p>
    <h1>让每种结果，都有可见的处理<span>。</span></h1>
    <p class="subheading">在隔离的软件环境中比较流程，不改变当前任务、队列和历史。</p></div></div>
    <div class="scenario-layout"><section class="card"><h2>验证输入</h2>
    <div class="toolbar"><button id="scenario-current" class="button secondary">使用当前编排</button>
    <button id="scenario-example" class="button secondary">三地搜索示例</button></div>
    <label class="stacked-label">工作流 JSON<textarea id="scenario-workflow" class="json-editor" rows="12" spellcheck="false"></textarea></label>
    <button id="scenario-preflight" class="button secondary">检查当前执行条件</button>
    <div id="scenario-preflight-result" class="scenario-findings" aria-live="polite"></div>
    <h3>选择场景</h3><div id="scenario-catalog" class="scenario-catalog"></div>
    <p class="help-text">运行的是生产任务状态机与明确标记的测试接口。等待已加速，不验证真实路径、感知效果或运行时长。</p>
    <div class="toolbar"><button id="scenario-run" class="button primary">运行所选场景</button>
    <button id="scenario-export" class="button secondary" disabled>导出验证报告</button></div>
    <p id="scenario-status" role="status"></p></section>
    <section class="card"><h2>结果对比</h2><div id="scenario-results"><p class="help-text">选择场景后运行，查看执行状态、目标结果、重试和分支证据。</p></div></section></div>`;
  document.querySelector("main").insertBefore(root, document.querySelector(".page-footer"));
  for (const [parent, text, cls] of [[document.querySelector(".sidebar"), "场景验证", "nav-link"], [document.querySelector(".mobile-tabs"), "验证", ""]]) {
    if (!parent) continue;
    const link = node(cls ? "a" : "button", cls, text);
    link.href = "#scenarios";
    link.dataset.view = "scenarios";
    link.onclick = event => {event.preventDefault(); if (location.hash === "#scenarios") U.switchView("scenarios"); else location.hash = "scenarios";};
    if (cls) parent.insertBefore(link, parent.querySelector(".sidebar-bottom")); else parent.append(link);
  }
  const example = {version: 1, name: "三地搜索水杯", steps: [{type: "search", id: "find", locations: ["reception", "storage", "meeting_room"], object_name: "水杯", return_home: "found", continue_on: ["not_found"]}]};
  let lastReport = null, running = false, catalogLoading = null;
  function setRunning(value) {
    running = value;
    for (const id of ["scenario-run", "scenario-current", "scenario-example", "scenario-workflow", "scenario-preflight"]) $(id).disabled = value;
  }
  const labels = {succeeded:"流程完成", failed:"流程失败", cancelled:"已停止", achieved:"目标达成", not_achieved:"目标未达成", unknown:"目标未知", not_applicable:"无独立目标", skipped:"已跳过", pending:"未执行", found:"已发现", not_found:"未发现", inconclusive:"无法判断", timed_out:"超时"};
  const scenarioNames = new Map();
  function input() {return {workflow: JSON.parse($("scenario-workflow").value)};}
  function status(message, error = false) {
    $("scenario-status").textContent = message;
    $("scenario-status").className = error ? "panel-error" : "help-text";
  }
  async function attempt(action) {try {await action();} catch (error) {status(error.message, true);}}
  function invalidateInput() {
    lastReport = null; $("scenario-export").disabled = true;
    $("scenario-preflight-result").replaceChildren();
    $("scenario-results").replaceChildren(node("p", "help-text", "输入已更新，请重新运行场景。"));
  }
  function loadWorkflow(value) {
    $("scenario-workflow").value = JSON.stringify(value, null, 2);
    invalidateInput();
    status("草稿已载入；当前任务没有变化。");
  }
  async function loadCatalog() {
    if (scenarioNames.size) return;
    if (catalogLoading) return catalogLoading;
    catalogLoading = (async () => {
      const data = await U.api("/api/scenarios");
      $("scenario-catalog").replaceChildren();
      for (const item of data.scenarios) {
        scenarioNames.set(item.id, item.label);
        const label = node("label", "scenario-option"), check = node("input");
        check.type = "checkbox"; check.value = item.id; check.checked = true;
        const copy = node("span"); copy.append(node("strong", "", item.label), node("small", "", item.description));
        label.append(check, copy); $("scenario-catalog").append(label);
      }
    })();
    try {await catalogLoading;} finally {catalogLoading = null;}
  }
  function showPreflight(data) {
    const box = $("scenario-preflight-result"); box.replaceChildren();
    box.append(node("strong", "", data.ready ? "当前检查通过，可核对后提交" : "当前存在执行阻碍"));
    box.append(node("p", "help-text", `${data.steps} 个步骤 · ${data.conditional_steps} 个条件步骤 · 最多 ${data.maximum_attempts} 次尝试`));
    box.append(node("p", "help-text", `配置超时合计 ${data.configured_timeout_budget_seconds} 秒。${data.budget_note}`));
    for (const finding of data.findings) box.append(node("p", `finding-${finding.severity}`, `${finding.step_id ? finding.step_id + "：" : ""}${finding.message}`));
    box.append(node("p", "help-text", "这是当前状态快照，不会派发任务，也不保证稍后状态保持不变。"));
  }
  function showReport(report) {
    const box = $("scenario-results"); box.replaceChildren();
    const tableWrap = node("div", "scenario-table-wrap"), table = node("table", "scenario-table");
    const head = node("thead"), row = node("tr");
    for (const title of ["场景", "执行", "目标", "失败 / 跳过", "重试"]) row.append(node("th", "", title));
    head.append(row); table.append(head); const body = node("tbody");
    for (const item of report.comparison) {
      const tr = node("tr");
      for (const value of [scenarioNames.get(item.scenario_id), labels[item.state] || item.state, labels[item.goal_outcome], `${item.failed_steps} / ${item.skipped_steps}`, item.retries]) tr.append(node("td", "", String(value)));
      body.append(tr);
    }
    table.append(body); tableWrap.append(table); box.append(tableWrap);
    for (const run of report.runs) {
      const details = node("details", "scenario-evidence");
      details.append(node("summary", "", `${scenarioNames.get(run.scenario_id)} · 步骤证据`));
      if (run.bounded_stop) details.append(node("p", "panel-error", "已达到场景时间上限并停止；本次验证未完成。"));
      const list = node("ol");
      for (const step of run.step_states) {
        const result = run.results.find(item => item.step_id === step.step_id);
        const li = node("li");
        li.append(node("strong", "", `${step.step_id} · ${labels[step.status] || step.status}`));
        li.append(node("p", "help-text", result?.message || step.reason || "未执行"));
        if (result?.error_code) li.append(node("code", "", result.error_code));
        if (result?.evidence) li.append(node("pre", "", typeof result.evidence === "string" ? result.evidence : JSON.stringify(result.evidence, null, 2)));
        list.append(li);
      }
      details.append(list); box.append(details);
    }
    box.append(node("p", "help-text", "全部结果为隔离场景数据。配置等待已加速，失败为明确注入，不能推断真实机器人效果。"));
  }
  $("scenario-current").onclick = () => attempt(async () => loadWorkflow(window.PatrolWorkflow.snapshot()));
  $("scenario-example").onclick = () => loadWorkflow(example);
  $("scenario-workflow").addEventListener("input", invalidateInput);
  $("scenario-preflight").onclick = () => attempt(async () => showPreflight(await U.api("/api/preflight", input())));
  $("scenario-run").onclick = () => attempt(async () => {
    if (running) return;
    const payload = {...input(), scenario_ids: [...$("scenario-catalog").querySelectorAll("input:checked")].map(item => item.value)};
    if (!payload.scenario_ids.length) throw new Error("请至少选择一个场景。");
    setRunning(true); status("正在隔离环境中验证所选场景…");
    try {
      lastReport = await U.api("/api/scenarios/run", payload, {timeoutMs: 40000});
      showReport(lastReport); $("scenario-export").disabled = false;
      status(`已完成 ${lastReport.runs.length} 个场景，真实任务历史没有变化。`);
    } finally {setRunning(false);}
  });
  $("scenario-export").onclick = () => {
    if (!lastReport) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(lastReport, null, 2)], {type:"application/json"}));
    const a = node("a"); a.href = url; a.download = `scenario-${lastReport.id}.json`; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  loadWorkflow(example);
  window.addEventListener("patrol:view", event => {if (event.detail === "scenarios") attempt(loadCatalog);});
  // Queue edits update only waiting jobs; server remains the authority on race conditions.
  const edit = node("section", "card queue-editor");
  edit.innerHTML = `<h2>调整待执行任务</h2><p class="help-text">只调整排队中的任务；正在执行、完成或中断的任务不能修改。</p>
    <form id="queue-edit-form" class="workspace-form"><label>待执行任务<select id="queue-edit-job"></select></label>
    <div class="form-row"><label>优先级<input id="queue-edit-priority" type="number" min="0" max="100" value="50"></label>
    <label>新执行时间<input id="queue-edit-time" type="datetime-local"></label></div>
    <label>重复策略<select id="queue-edit-repeat"><option value="keep">保持原策略</option><option value="none">改为单次</option><option value="interval">固定间隔</option></select></label>
    <label>间隔秒数<input id="queue-edit-interval" type="number" min="1" max="31536000" value="3600"></label>
    <button id="queue-edit-save" class="button secondary">保存调整</button><p id="queue-edit-status" role="status"></p></form>`;
  $("view-queue").append(edit);
  let waitingJobs = [];
  function fillJob() {
    const job = waitingJobs.find(item => item.id === $("queue-edit-job").value);
    $("queue-edit-save").disabled = !job;
    if (!job) return;
    $("queue-edit-priority").value = job.priority;
    $("queue-edit-time").value = "";
    $("queue-edit-repeat").value = "keep";
  }
  async function loadJobs() {
    const data = await U.api("/api/queue"); waitingJobs = data.jobs.filter(job => job.status === "queued");
    const select = $("queue-edit-job"), previous = select.value; select.replaceChildren();
    for (const job of waitingJobs) {const option = node("option", "", job.summary); option.value = job.id; select.append(option);}
    if (waitingJobs.some(job => job.id === previous)) select.value = previous;
    fillJob();
  }
  $("queue-edit-job").onchange = fillJob;
  $("queue-edit-form").onsubmit = async event => {
    event.preventDefault(); const output = $("queue-edit-status");
    try {
      const payload = {priority: Number($("queue-edit-priority").value)};
      if ($("queue-edit-time").value) payload.run_at = new Date($("queue-edit-time").value).toISOString();
      if ($("queue-edit-repeat").value === "none") payload.repeat = null;
      if ($("queue-edit-repeat").value === "interval") payload.repeat = {interval_seconds: Number($("queue-edit-interval").value)};
      await U.api(`/api/queue/${$("queue-edit-job").value}`, payload, {method:"PUT"});
      output.textContent = "已保存调整并记录审计事件。"; await loadJobs();
      $("queue-refresh").click();
    } catch (error) {output.textContent = error.message;}
  };
  window.addEventListener("patrol:view", event => {if (event.detail === "queue") loadJobs().catch(error => $("queue-edit-status").textContent = error.message);});
  $("queue-refresh").addEventListener("click", () => loadJobs().catch(error => $("queue-edit-status").textContent = error.message));
  if (location.hash === "#scenarios") {U.switchView("scenarios"); attempt(loadCatalog);}
})();
