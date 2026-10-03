"use strict";
/* Calendar editing and complete local history. No microphone or device access. */
(() => {
  const U = window.PatrolUI, A = window.AssistiveUI;
  const $ = id => document.getElementById(id);
  const el = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const attempt = async fn => { try { return await fn(); } catch (error) { A.message(error.message, true); return null; } };
  const localDateTime = value => {
    if (!value) return "";
    const date = new Date(value);
    date.setMinutes(date.getMinutes() - date.getTimezoneOffset());
    return date.toISOString().slice(0, 16);
  };
  const displayDate = value => value ? new Date(value).toLocaleString() : "未设置";
  const localZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "本机时区";
  const days = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
  const terminal = new Set(["acknowledged", "cancelled", "completed"]);
  function button(text, fn) {
    const b = el("button", text); b.type = "button"; b.onclick = () => attempt(fn); return b;
  }
  function scheduleFields(prefix) {
    return `<label>重复方式<select id="${prefix}-mode"><option value="once">只提醒一次</option><option value="daily">每天固定时间</option><option value="weekly">每周指定几天</option><option value="interval">固定间隔</option></select></label>
      <div id="${prefix}-calendar" class="daily-calendar" hidden>
        <fieldset id="${prefix}-weekdays"><legend>选择每周日期</legend>${days.map((name, i) => `<label class="daily-day"><input type="checkbox" value="${i + 1}">${name}</label>`).join("")}</fieldset>
        <label>日历中的提醒时间<input id="${prefix}-clock" type="time" value="08:00"></label>
        <label>日历时区<input id="${prefix}-zone" value="Asia/Hong_Kong" maxlength="80" placeholder="Asia/Hong_Kong"></label>
      </div><label id="${prefix}-interval-label" hidden>间隔（分钟）<input id="${prefix}-interval" type="number" min="1" max="527040" value="60" step="1"></label>
      <label>截止时间（选填，本机时区）<input id="${prefix}-end" type="datetime-local"></label>`;
  }
  function setupSchedule(prefix, timeInput) {
    const mode = $(prefix + "-mode").value;
    const calendar = ["daily", "weekly"].includes(mode);
    $(prefix + "-calendar").hidden = !calendar;
    $(prefix + "-weekdays").hidden = mode !== "weekly";
    $(prefix + "-interval-label").hidden = mode !== "interval";
    timeInput.closest("label").hidden = calendar;
    timeInput.required = !calendar;
    timeInput.disabled = calendar;
    $(prefix + "-clock").required = calendar;
    $(prefix + "-zone").required = calendar;
    $(prefix + "-clock").disabled = !calendar;
    $(prefix + "-zone").disabled = !calendar;
    $(prefix + "-interval").disabled = mode !== "interval";
  }
  function schedulePayload(prefix, timeValue, editing) {
    const mode = $(prefix + "-mode").value;
    const body = {};
    if (["daily", "weekly"].includes(mode)) {
      const weekdays = mode === "daily" ? [1, 2, 3, 4, 5, 6, 7] : [...$(prefix + "-weekdays").querySelectorAll("input:checked")].map(input => Number(input.value));
      if (!weekdays.length) throw new Error("请至少选择一个每周日期。");
      body.calendar = {weekdays, local_time: $(prefix + "-clock").value, timezone: $(prefix + "-zone").value.trim()};
    } else {
      if (!timeValue) throw new Error("请填写首次提醒时间。");
      body.due_at = new Date(timeValue).toISOString();
      if (editing) body.calendar = null;
      if (mode === "interval") body.repeat_seconds = Number($(prefix + "-interval").value) * 60;
      else if (editing) body.repeat_seconds = null;
    }
    const end = $(prefix + "-end").value;
    if (end) body.end_at = new Date(end).toISOString();
    else if (editing) body.end_at = null;
    return body;
  }
  const createForm = $("care-reminder-form");
  const createFields = el("div", undefined, "daily-schedule-fields");
  createFields.innerHTML = scheduleFields("daily-create");
  createForm.insertBefore(createFields, createForm.querySelector("button"));
  createForm.before(el("p", `首次时间和截止时间按本机时区 ${localZone} 填写。每周/每天的时间按单独选择的日历时区计算。`, "help-text"));
  $("daily-create-mode").onchange = () => setupSchedule("daily-create", $("care-reminder-time"));
  setupSchedule("daily-create", $("care-reminder-time"));
  createForm.onsubmit = event => {
    event.preventDefault();
    attempt(async () => {
      const result = await A.act({op: "reminder.create", title: $("care-reminder-title").value,
        ...schedulePayload("daily-create", $("care-reminder-time").value, false)});
      await refreshOpenHistory(); return result;
    });
  };

  const editor = el("dialog", undefined, "daily-editor"); editor.id = "daily-editor";
  editor.setAttribute("aria-labelledby", "daily-editor-title");
  editor.innerHTML = `<form id="daily-edit-form"><h2 id="daily-editor-title">修改提醒安排</h2>
    <p class="help-text">修改时间规则后重新计算下一次提醒；只改内容会保留原时间。过去的确认保留在历史中。</p>
    <label>提醒内容<input id="daily-edit-title" required maxlength="160"></label>
    <label>首次提醒时间（本机时区）<input id="daily-edit-time" type="datetime-local" required></label>
    ${scheduleFields("daily-edit")}
    <p id="daily-edit-status" role="status" aria-live="polite"></p>
    <div class="care-actions"><button id="daily-edit-save" type="submit" class="care-primary">保存修改</button><button id="daily-edit-reload" type="button">重新读取当前记录</button><button id="daily-edit-close" type="button">关闭编辑</button></div>
  </form>`;
  $("view-assistive").append(editor);
  let editing = null, editingSchedule = "", editingEnd = "";
  function editorScheduleKey() {
    return JSON.stringify([...["mode", "time", "interval", "clock", "zone"].map(key => $("daily-edit-" + key).value),
      [...$("daily-edit-weekdays").querySelectorAll("input:checked")].map(input => input.value)]);
  }
  function fillEditor(record) {
    editing = record;
    $("daily-edit-title").value = record.title;
    $("daily-edit-time").value = localDateTime(record.due_at);
    $("daily-edit-end").value = localDateTime(record.end_at);
    $("daily-edit-mode").value = record.calendar ? (record.calendar.weekdays.length === 7 ? "daily" : "weekly") : record.repeat_seconds ? "interval" : "once";
    $("daily-edit-interval").value = (record.repeat_seconds || 3600) / 60;
    $("daily-edit-clock").value = record.calendar?.local_time || "08:00";
    $("daily-edit-zone").value = record.calendar?.timezone || A.getSnapshot()?.profile?.timezone || "Asia/Hong_Kong";
    for (const input of $("daily-edit-weekdays").querySelectorAll("input")) input.checked = (record.calendar?.weekdays || []).includes(Number(input.value));
    setupSchedule("daily-edit", $("daily-edit-time"));
    editingSchedule = editorScheduleKey(); editingEnd = $("daily-edit-end").value;
    $("daily-edit-status").textContent = `正在编辑“${record.title}”；若记录在编辑期间变化，保存会提示重新读取。`;
  }
  function openEditor(record) {
    if (terminal.has(record.state)) throw new Error("该提醒已经结束，可另建新的提醒。");
    A.stopScan(); fillEditor(record); editor.showModal(); $("daily-edit-title").focus();
  }
  $("daily-edit-mode").onchange = () => setupSchedule("daily-edit", $("daily-edit-time"));
  $("daily-edit-close").onclick = () => editor.close();
  $("daily-edit-reload").onclick = () => attempt(async () => {
    const data = await U.api("/api/assistive/history/" + encodeURIComponent(editing.id));
    if (terminal.has(data.record.state)) { editor.close(); throw new Error("这条提醒已经结束。"); }
    fillEditor(data.record);
  });
  $("daily-edit-form").onsubmit = async event => {
    event.preventDefault(); if (!editing) return;
    $("daily-edit-save").disabled = true;
    try {
      const patch = {op: "reminder.update", id: editing.id, expected_revision: editing.revision || 1};
      if ($("daily-edit-title").value !== editing.title) patch.title = $("daily-edit-title").value;
      if (editorScheduleKey() !== editingSchedule) Object.assign(patch, schedulePayload("daily-edit", $("daily-edit-time").value, true));
      else if ($("daily-edit-end").value !== editingEnd) patch.end_at = $("daily-edit-end").value ? new Date($("daily-edit-end").value).toISOString() : null;
      if (Object.keys(patch).length === 3) { $("daily-edit-status").textContent = "没有需要保存的修改。"; return; }
      await A.act(patch);
      editor.close(); await refreshOpenHistory();
    } catch (error) { $("daily-edit-status").textContent = error.message; }
    finally { $("daily-edit-save").disabled = false; }
  };
  function recurrenceText(record) {
    const rule = record.calendar;
    const text = rule ? `${rule.weekdays.map(day => days[day - 1]).join("、")} ${rule.local_time} · ${rule.timezone}` : record.repeat_seconds ? `每 ${record.repeat_seconds / 60} 分钟` : "单次提醒";
    return text + (record.end_at ? "；截止 " + displayDate(record.end_at) : "");
  }
  window.addEventListener("assistive:state", event => {
    const rows = [...$("care-reminders").querySelectorAll("article")];
    for (const [index, record] of (event.detail.reminders || []).entries()) {
      const row = rows[index]; if (!row) continue;
      row.append(el("p", recurrenceText(record), "help-text"));
      if (!terminal.has(record.state)) row.append(button("修改提醒", () => openEditor(record)));
    }
  });

  const history = el("section", undefined, "card daily-history"); history.id = "daily-history";
  history.innerHTML = `<h2>全部生活记录</h2><p class="help-text">查询本机保存的完整历史，包含已结束和已移除的记录。按更新时间排列；记录更新后会排到前面。日期筛选使用本机日期。</p>
    <form id="daily-history-filter" class="care-form">
      <label>记录类别<select id="daily-history-kind"><option value="">全部类别</option></select></label>
      <label>记录状态<select id="daily-history-state"><option value="">全部状态</option></select></label>
      <label>内容关键词<input id="daily-history-query" maxlength="160" placeholder="如：饮水、纸巾、就医"></label>
      <label>更新开始日期<input id="daily-history-since" type="date"></label><label>更新结束日期<input id="daily-history-until" type="date"></label>
      <label>每页条数<select id="daily-history-limit"><option>10</option><option selected>25</option><option>50</option><option>100</option></select></label>
      <button type="submit">查询生活记录</button><button id="daily-history-reset" type="button">清空筛选</button>
    </form><p id="daily-history-status" role="status" aria-live="polite">点击查询，查看历史记录。</p>
    <div class="daily-history-layout"><div><div id="daily-history-list" class="care-records"></div><div class="care-actions"><button id="daily-history-prev" type="button" disabled>上一页</button><button id="daily-history-next" type="button" disabled>下一页</button></div></div><section id="daily-history-detail" aria-label="生活记录详情"><p class="help-text">选择记录可查看详情、修改记录和完整处理历史。</p></section></div>`;
  const catalogCard = $("care-catalog").closest("section"); catalogCard.before(history);
  $("care-refresh").after(button("查询全部生活记录", () => { history.scrollIntoView({behavior: "auto", block: "start"}); $("daily-history-query").focus(); return loadHistory(0); }));
  let metadata = null, metadataLoading = null, offset = 0, lastResult = null, appliedQuery = null, generation = 0, detailGeneration = 0, detailId = null;
  async function loadMetadata() {
    if (metadata) return;
    if (!metadataLoading) metadataLoading = (async () => {
      metadata = await U.api("/api/assistive/history/meta");
      for (const [value, label] of Object.entries(metadata.kinds || {})) { const option = el("option", label); option.value = value; $("daily-history-kind").append(option); }
      updateStates();
    })().finally(() => { metadataLoading = null; });
    await metadataLoading;
  }
  function updateStates() {
    const value = $("daily-history-state").value, kind = $("daily-history-kind").value;
    const all = el("option", "全部状态"); all.value = ""; $("daily-history-state").replaceChildren(all);
    const allowed = kind ? metadata?.kind_states?.[kind] || [] : Object.keys(metadata?.states || {});
    for (const key of allowed) { const option = el("option", metadata.states[key] || key); option.value = key; $("daily-history-state").append(option); }
    if (allowed.includes(value)) $("daily-history-state").value = value;
  }
  function historyQuery(pageOffset) {
    const query = new URLSearchParams({limit: $("daily-history-limit").value, offset: String(pageOffset)});
    for (const key of ["kind", "state", "query"]) { const value = $("daily-history-" + key).value.trim(); if (value) query.set(key, value); }
    for (const key of ["since", "until"]) {
      const value = $("daily-history-" + key).value;
      if (value) {
        const date = new Date(value + "T00:00:00");
        if (key === "until") { date.setDate(date.getDate() + 1); date.setMilliseconds(-1); }
        query.set(key, date.toISOString().replace(key === "until" ? ".999Z" : "never-match", ".999999Z"));
      }
    }
    return query;
  }
  async function loadHistory(pageOffset = 0, reuseFilters = false) {
    const request = ++generation;
    $("daily-history-prev").disabled = $("daily-history-next").disabled = true;
    $("daily-history-status").textContent = "正在查询本机记录…";
    try {
      await loadMetadata();
      const query = reuseFilters && appliedQuery ? new URLSearchParams(appliedQuery) : historyQuery(pageOffset);
      query.set("offset", String(pageOffset));
      const data = await U.api("/api/assistive/history?" + query);
      if (request !== generation) return;
      offset = data.offset; lastResult = data; appliedQuery = query.toString();
      const list = $("daily-history-list"); list.replaceChildren();
      for (const record of data.records) {
        const row = el("article", undefined, "care-record");
        row.append(el("strong", record.title || record.name || record.note || metadata.kinds[record.kind] || "生活记录"),
          el("p", `${metadata.kinds[record.kind] || record.kind} · ${metadata.states[record.state] || record.state}`),
          el("p", "更新于 " + displayDate(record.updated_at)), button("查看这条记录", () => loadDetail(record.id)));
        list.append(row);
      }
      if (!data.records.length) list.append(el("p", "没有符合条件的生活记录。"));
      $("daily-history-status").textContent = `共 ${data.total} 条 · 当前第 ${data.total ? Math.floor(offset / data.limit) + 1 : 0} 页 · 每页 ${data.limit} 条 · 已显示 ${data.records.length} 条`;
      $("daily-history-prev").disabled = offset === 0;
      $("daily-history-next").disabled = !data.has_more;
    } catch (error) { if (request === generation) $("daily-history-status").textContent = error.message; throw error; }
  }
  async function loadDetail(identifier, eventOffset = 0) {
    const request = ++detailGeneration;
    const data = await U.api(`/api/assistive/history/${encodeURIComponent(identifier)}?event_limit=20&event_offset=${eventOffset}`);
    if (request !== detailGeneration) return;
    detailId = identifier;
    const record = data.record, box = $("daily-history-detail"); box.replaceChildren();
    box.append(el("h3", record.title || record.name || metadata.kinds[record.kind] || "生活记录"));
    const fields = [["状态", metadata.states[record.state] || record.state], ["建立时间", displayDate(record.created_at)], ["更新时间", displayDate(record.updated_at)]];
    if (record.due_at) fields.push(["提醒/确认时间", displayDate(record.due_at)]);
    if (record.kind === "reminder") fields.push(["重复安排", recurrenceText(record)]);
    if (record.detail || record.note) fields.push(["本人填写", record.detail || record.note]);
    const definitions = el("dl", undefined, "daily-details");
    for (const [label, value] of fields) definitions.append(el("dt", label), el("dd", value));
    box.append(definitions, el("p", "来源：本机记录。本人确认、外部回执和实际照护完成是不同状态。", "help-text"));
    if (record.kind === "reminder" && !terminal.has(record.state)) box.append(button("修改这条提醒", () => openEditor(record)));
    box.append(el("h4", `处理历史 · 共 ${data.event_total} 条`));
    const operations = {create:"建立记录", update:"修改记录", ack:"本人知晓", snooze:"稍后提醒", cancel:"取消安排", save:"保存记录", remove:"移除记录", add:"加入清单", check:"更新勾选", reset:"重置清单", start:"开始安排", report:"保存本人反馈", confirm:"本人确认", resolve:"记录已解决", service:"记录维护", due:"提醒到时", missed:"等待本人知晓", overdue:"等待人工核实", acknowledged:"记录已知晓", resolved:"记录已解决", delivered:"收到送达回执", completed:"完成本地记录", help_pending:"等待建立协助记录", from_checkin:"从本人反馈建立协助"};
    for (const event of data.events) box.append(el("p", displayDate(event.time) + " · " + (operations[event.action.split(".").at(-1)] || "更新处理记录")));
    if (eventOffset) box.append(button("上一页处理历史", () => loadDetail(identifier, Math.max(0, eventOffset - 20))));
    if (data.events_has_more) box.append(button("下一页处理历史", () => loadDetail(identifier, eventOffset + 20)));
    const raw = el("details"); raw.append(el("summary", "查看完整本机记录"), el("pre", JSON.stringify(record, null, 2))); box.append(raw);
    box.append(button("下载这条记录", () => {
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: "application/json"}));
      const link = el("a"); link.href = url; link.download = "living-record-" + record.id + ".json"; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }));
  }
  async function refreshOpenHistory() { if (lastResult) await loadHistory(offset, true); if (detailId) await loadDetail(detailId); }
  $("daily-history-filter").onsubmit = event => { event.preventDefault(); attempt(() => loadHistory(0)); };
  $("daily-history-kind").onchange = updateStates;
  $("daily-history-prev").onclick = () => attempt(() => loadHistory(Math.max(0, offset - lastResult.limit), true));
  $("daily-history-next").onclick = () => attempt(() => loadHistory(offset + lastResult.limit, true));
  $("daily-history-reset").onclick = () => { $("daily-history-filter").reset(); updateStates(); attempt(() => loadHistory(0)); };
  attempt(loadMetadata);
})();
