"use strict";
(() => {
  const U = window.PatrolUI, $ = id => document.getElementById(id);
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
  const section = el("section", undefined, "workspace-view assistive-view");
  section.id = "view-assistive"; section.hidden = true;
  section.innerHTML = `
    <div class="care-hero"><div><p class="eyebrow">EVERYDAY, WITH YOUR CHOICE.</p><h1>让日常，多一份自主。</h1><p>说出需要，查看进展。每一步，都由你确认。</p></div><div class="care-mode" id="care-mode">正在读取运行方式</div></div>
    <div class="care-toolbar"><button id="care-refresh" type="button">刷新生活记录</button><button id="care-large" type="button" aria-pressed="false">大字模式</button><button id="care-contrast" type="button" aria-pressed="false">高对比度</button><button id="care-scan" type="button" aria-pressed="false">开启单键扫描</button><button id="care-stop" class="care-stop" type="button">停止机器人任务</button></div>
    <p id="care-scan-help" class="help-text">支持键盘 Tab / Enter。开启单键扫描后，空格选择当前项目，Escape 退出；不会自动开启麦克风。</p>
    <div id="care-message" class="care-message" role="status" aria-live="polite">生活辅助记录保存在本机；人工协助默认只记录请求，没有发送电话或短信。</div>
    <div class="care-layout">
      <div>
        <section class="card care-command"><h2>今天需要什么帮助？</h2><form id="care-command-form"><label for="care-text">输入你的需要</label><textarea id="care-text" maxlength="500" rows="2" placeholder="例如：十分钟后提醒我喝水"></textarea><div class="care-actions"><button id="care-preview" type="button">先看处理方式</button><button type="submit" class="care-primary">发送请求</button><button id="care-voice" type="button">使用语音输入</button></div></form><div id="care-draft" hidden></div><div class="care-quick" id="care-quick"></div></section>
        <section class="card"><h2>日常提醒</h2><p class="help-text">到时显示，完成后由你确认。药物仅按你填写的计划提醒，不提供剂量判断。</p><form id="care-reminder-form" class="care-form"><label>提醒内容<input id="care-reminder-title" required maxlength="160" placeholder="喝水 / 预约出门 / 个人计划"></label><label>首次提醒时间<input id="care-reminder-time" type="datetime-local" required></label><button class="care-primary" type="submit">添加提醒</button></form><div id="care-reminders" class="care-records"></div></section>
        <section class="card"><h2>家居与取送</h2><p class="help-text">软件演示会标明预设结果。真实设备需要声明能力并提供操作证据。</p><form id="care-device-form" class="care-form"><label>房间<select id="care-device-room"></select></label><label>设备<select id="care-device-kind"><option>灯</option><option>窗帘</option><option>风扇</option><option>电视</option></select></label><label>操作<select id="care-device-action"><option>打开</option><option>关闭</option></select></label><button type="submit">执行并等待回读</button></form><form id="care-delivery-form" class="care-form"><label>物品<select id="care-item"></select></label><label>从<select id="care-from"></select></label><label>送到<select id="care-to"></select></label><button type="submit">生成取送计划</button></form><div id="care-robot" class="care-detail"></div></section>
        <section class="card"><h2>生活用品清单</h2><form id="care-need-form" class="care-form"><label>需要准备的用品<input id="care-need-text" required maxlength="160" placeholder="纸巾、食品或其他用品"></label><button type="submit">记入清单</button></form><div id="care-needs" class="care-records"></div></section>
      </div>
      <div>
        <section class="card"><h2>请求人工协助</h2><p class="help-text">移乘、如厕、洗浴等由人工协助；界面记录进展，不表示机器人已完成身体照护。</p><form id="care-help-form" class="care-form"><label>需要哪类帮助<select id="care-help-category"></select></label><label>补充说明<textarea id="care-help-note" maxlength="500" rows="2"></textarea></label><button class="care-primary" type="submit">记录协助请求</button></form><button id="care-urgent" class="care-urgent" type="button">紧急求助 · 本地记录</button><div id="care-assistance" class="care-records"></div></section>
        <section class="card"><h2>每日生活流程</h2><div id="care-routines" class="care-actions"></div><div id="care-checklists" class="care-records"></div></section>
        <section class="card"><h2>沟通快捷语</h2><p class="help-text">点击显示大字；播报需你另外点击，也不代表对方已听到。</p><div id="care-aac" class="care-quick"></div><p id="care-phrase" class="care-phrase" aria-live="polite"></p><button id="care-say" type="button">播报当前文字</button></section>
        <section class="card"><h2>本人状态记录</h2><form id="care-checkin-form" class="care-form"><label>现在的感受<select id="care-checkin-status"><option value="good">目前还好</option><option value="need_help">需要帮助</option><option value="uncomfortable">感觉不适</option></select></label><label>补充记录<input id="care-checkin-note" maxlength="500"></label><button type="submit">保存本人反馈</button></form><div id="care-checkins" class="care-records"></div></section>
      </div>
    </div>
    <section class="card"><h2>场景覆盖与边界</h2><p class="help-text">这是一份可检查、可扩充的生活场景目录；“人工协助”表示请求管理，“设备接口”表示需要接入驱动。</p><label for="care-filter">查找生活场景</label><input id="care-filter" placeholder="如厕、洗浴、出门、睡眠…"><div id="care-catalog" class="care-catalog"></div></section>`;
  document.querySelector("main").insertBefore(section, document.querySelector(".page-footer"));
  const contactsCard=el("section",undefined,"card");
  contactsCard.innerHTML=`<h2>联系人与协助安排</h2><p class="help-text">只保存联系信息供人工或后续通信接口使用，不自动拨号或发送消息。</p><form id="care-contact-form" class="care-form"><label>联系人称呼<input id="care-contact-name" maxlength="80" required></label><label>联系备注<input id="care-contact-hint" maxlength="160" placeholder="如电话或联系方法"></label><button type="submit">保存联系人</button></form><div id="care-contacts" class="care-records"></div>`;
  section.querySelector(".care-layout>div:last-child").append(contactsCard);
  const contactChoice=el("label","本次请求的指定联系人（可不选）");
  const contactSelect=el("select");contactSelect.id="care-help-contact";contactChoice.append(contactSelect);
  const consentLabel=el("label",undefined,"care-consent");const consent=el("input");consent.type="checkbox";consent.id="care-help-consent";
  consentLabel.append(consent,document.createTextNode("同意为本次请求使用所选联系信息；当前仍未发送"));
  $("care-help-form").insertBefore(contactChoice,$("care-help-form button"));
  $("care-help-form").insertBefore(consentLabel,$("care-help-form button"));
  const domainFilter=el("select");domainFilter.id="care-domain";domainFilter.setAttribute("aria-label","按生活领域筛选");
  const domainLabel=el("label","按生活领域筛选");domainLabel.append(domainFilter);$("care-filter").before(domainLabel);
  let snapshot = null, catalog = null, busy = false, pending = null, scanTimer = null, scanIndex = 0, scanTarget = null, lastDue = "", loading = false;
  const session = U.sessionId;
  const labels = {scheduled:"等待提醒",active:"进行中",recorded:"本人记录",due:"等待本人确认",missed:"逾期未确认",acknowledged:"已确认",snoozed:"稍后提醒",cancelled:"已取消",created:"请求已记录",open:"待处理",resolved:"本人报告已解决",escalated:"待升级处理（本地）",delivered:"送达回执",completed:"已完成",pending:"待处理",checked:"已勾选"};
  function message(text, error=false) { $("care-message").textContent = text; $("care-message").classList.toggle("error",error); }
  function button(text, fn) { const b=el("button",text); b.type="button"; b.onclick=()=>attempt(fn); return b; }
  async function attempt(fn) { try { return await fn(); } catch (e) { message(e.message,true); return null; } }
  const mutations=new Map();
  async function act(body) {
    const key=JSON.stringify(body);
    let pendingAction=mutations.get(key);
    if(pendingAction?.promise)return pendingAction.promise;
    if(!pendingAction){pendingAction={request_id:crypto.randomUUID(),promise:null};mutations.set(key,pendingAction);}
    pendingAction.promise=(async()=>{try{const r=await U.api("/api/assistive/action",{...body,request_id:pendingAction.request_id});mutations.delete(key);message(r.message||"已保存");await load();return r;}catch(error){if(error.definite)mutations.delete(key);throw error;}finally{pendingAction.promise=null;}})();
    return pendingAction.promise;
  }
  function list(id, records, draw) { const parent=$(id); parent.replaceChildren(); if(!records?.length)parent.append(el("p","暂无记录","help-text")); for(const record of records||[]){const row=el("article",undefined,"care-record"); draw(row,record); parent.append(row);} }
  function title(row, r) { row.append(el("strong",r.title||r.text||r.label||r.category||r.id),el("span",labels[r.state]||r.state||"已记录","care-status")); }
  const date = value => value ? new Date(value).toLocaleString("zh-CN",{hour12:false}) : "";
  function render() {
    if(!snapshot)return;
    const prefs=snapshot.profile||{};
    document.body.classList.toggle("care-large",prefs.text_scale>1);
    document.body.classList.toggle("care-contrast",prefs.high_contrast===true);
    $("care-large").setAttribute("aria-pressed",String(prefs.text_scale>1));
    $("care-contrast").setAttribute("aria-pressed",String(prefs.high_contrast===true));
    list("care-contacts",snapshot.contacts,(row,r)=>{row.append(el("strong",r.name),el("p",r.contact_hint||"仅保存称呼"),button("移除联系人",()=>act({op:"contact.remove",id:r.id})));});
    const priorContact=contactSelect.value;contactSelect.replaceChildren();const none=el("option","暂不指定");none.value="";contactSelect.append(none);for(const c of snapshot.contacts||[]){const option=el("option",c.name);option.value=c.id;contactSelect.append(option);}contactSelect.value=priorContact;
    list("care-reminders",snapshot.reminders,(row,r)=>{title(row,r);row.append(el("p",date(r.due_at||r.run_at)));if(!["cancelled","acknowledged","completed"].includes(r.state)){const expected_revision=r.revision||1;row.append(button("我已知晓",()=>act({op:"reminder.ack",id:r.id,expected_revision})),button("10 分钟后提醒",()=>act({op:"reminder.snooze",id:r.id,seconds:600,expected_revision})),button("取消提醒",()=>act({op:"reminder.cancel",id:r.id,expected_revision})));}});
    list("care-assistance",snapshot.assistance,(row,r)=>{title(row,r);row.append(el("p",r.detail||r.note||r.message||"本机记录，未发送外部消息"));if(!["resolved","cancelled"].includes(r.state))row.append(button("本人确认有人回应",()=>act({op:"assistance.report",id:r.id,status:"acknowledged"})),button("本人确认已解决",()=>act({op:"assistance.report",id:r.id,status:"resolved"})),button("取消请求",()=>act({op:"assistance.cancel",id:r.id})));});
    list("care-needs",snapshot.needs,(row,r)=>{title(row,r);row.append(button(r.state==="completed"?"恢复待准备":"已经备好",()=>act({op:"need.check",id:r.id,checked:r.state!=="completed"})),button("移除",()=>act({op:"need.remove",id:r.id})));});
    list("care-checkins",snapshot.checkins?.slice(0,5),(row,r)=>{row.append(el("strong",({good:"目前良好",okay:"目前一般",uncomfortable:"感觉不适",need_help:"需要帮助"})[r.feeling]||"本人记录"),el("p",r.note||""),el("small",date(r.created_at)));});
    list("care-checklists",snapshot.checklists,(row,r)=>{title(row,r);for(const item of r.items||[]){row.append(button(`${item.checked?"✓":"○"} ${item.title||item.label||item.text}`,()=>act({op:"checklist.check",id:r.id,item_id:item.id,checked:!item.checked})));}});
    const due=(snapshot.reminders||[]).filter(r=>["due","missed"].includes(r.state)).map(r=>r.id).join(",");
    window.dispatchEvent(new CustomEvent("assistive:state",{detail:snapshot}));
    if(due && due!==lastDue)message("有生活提醒等待你确认，请查看日常提醒。"); lastDue=due;
  }
  function renderCatalog(){const box=$("care-catalog");box.replaceChildren();const query=$("care-filter").value.trim();for(const c of catalog?.categories||[]){if((domainFilter.value&&c.domain!==domainFilter.value)||(query&&!JSON.stringify(c).includes(query)))continue;const card=el("article",undefined,"care-scenario");card.append(el("span",({software:"软件功能",hardware_interface:"设备接口",human_assistance:"人工协助"})[c.mode]||c.mode||"生活辅助","care-status"),el("h3",c.label||c.title||c.id),el("p",c.description||""),el("small",c.boundary||""));for(const example of (c.examples||[]).slice(0,1))card.append(button(example,()=>{ $("care-text").value=example;$("care-text").focus();message("示例已填入，尚未提交。");}));box.append(card);}}
  async function load(){if(loading)return;loading=true;try{snapshot=await U.api("/api/assistive");if(!catalog){catalog=await U.api("/api/assistive/catalog");renderCatalog();populate();}render();}finally{loading=false;}}
  function populate(){const routineLabels={morning:"晨间准备",night:"睡前准备",outdoor:"出门准备",return_home:"回家检查",meal:"用餐准备",home_safety:"居家检查"};$("care-routines").replaceChildren();for(const [key,routine]of Object.entries(catalog.routines||{}))$("care-routines").append(button(routineLabels[key]||routine.label,()=>act({op:"checklist.start",routine:key})));const all=el("option","全部生活领域");all.value="";domainFilter.replaceChildren(all);for(const domain of catalog.coverage_audit?.domains||[]){const opt=el("option",domain.label+" · "+domain.scenario_ids.length);opt.value=domain.id;domainFilter.append(opt);}const select=$("care-help-category");select.replaceChildren();for(const c of catalog.categories||[]){if(c.mode!=="human_assistance")continue;const option=el("option",c.label||c.id);option.value=c.id;select.append(option);} }
  async function command(text,preview=false){
    if(busy)return;busy=true;
    try{
      if(!pending||pending.text!==text)pending={text,request_id:crypto.randomUUID()};
      const r=await U.api(preview?"/api/plan":"/api/command",{...pending,session_id:session});
      if(!preview)pending=null;
      message(r.message||"已处理");
      const box=$("care-draft");box.replaceChildren();
      box.hidden=!(preview||r.needs_confirmation||r.needs_clarification);
      if(!box.hidden){
        box.append(el("p",r.plan?.summary||r.message));
        if(r.needs_clarification){for(const [i,option] of (r.options||[]).entries())box.append(button(r.option_labels?.[i]||option,()=>command(option)));}
        else if(preview&&r.execution_command)box.append(button("执行这条生活操作",()=>command(r.execution_command)));
        else if(r.needs_confirmation)box.append(button("确认执行取送计划",()=>command("确认执行")),button("放弃计划",()=>command("取消计划")));
      }
      if(!preview)U.speak(r.message||"已处理");
      await load();await U.refresh();return r;
    }catch(error){if(error.definite)pending=null;throw error;}finally{busy=false;}
  }
  $("care-command-form").onsubmit=e=>{e.preventDefault();attempt(()=>command($("care-text").value.trim()));};
  $("care-preview").onclick=()=>attempt(()=>command($("care-text").value.trim(),true));
  $("care-refresh").onclick=()=>attempt(load);
  $("care-voice").onclick=()=>{U.switchView("mission");location.hash="mission";$("command-input").focus();U.feedback("点击“点击说话”或选择本地 Vosk；识别后的生活辅助命令也会进入生活记录。");};
  $("care-stop").onclick=()=>attempt(async()=>{U.stopVoice();const r=await U.api("/api/control",{action:"stop"});message(r.message);await U.refresh();});
  const quick=["十分钟后提醒我喝水","我需要如厕帮助","我需要洗澡帮助","我需要穿衣帮助","我需要移乘帮助","我需要陪同出门"];
  for(const text of quick)$("care-quick").append(button(text,()=>{$("care-text").value=text;return command(text,true);}));
  for(const text of ["请等一下","我需要帮助","请说慢一点","我想喝水","请不要碰我","我想联系家人"])$("care-aac").append(button(text,()=>{$("care-phrase").textContent=text;}));
  $("care-say").onclick=()=>{if($("care-phrase").textContent){if(window.speechSynthesis){const utterance=new SpeechSynthesisUtterance($("care-phrase").textContent);utterance.lang="zh-CN";utterance.rate=snapshot?.profile?.speech_rate||1;window.speechSynthesis.cancel();window.speechSynthesis.speak(utterance);message("文字已提交浏览器播报，未确认对方听到。");}else message("当前环境没有语音播报接口",true);}};
  $("care-device-form").onsubmit=e=>{e.preventDefault();attempt(()=>command($("care-device-action").value+$("care-device-room").selectedOptions[0].text+$("care-device-kind").value));};
  $("care-delivery-form").onsubmit=e=>{e.preventDefault();attempt(()=>command(`把${$("care-item").value}从${$("care-from").selectedOptions[0].text}送到${$("care-to").selectedOptions[0].text}`));};
  $("care-reminder-form").onsubmit=e=>{e.preventDefault();attempt(()=>act({op:"reminder.create",title:$("care-reminder-title").value,due_at:new Date($("care-reminder-time").value).toISOString()}));};
  $("care-help-form").onsubmit=e=>{e.preventDefault();const contact=contactSelect.value;attempt(()=>act({op:"assistance.create",category:$("care-help-category").value,detail:$("care-help-note").value,...(contact?{contact_id:contact,consent:consent.checked}:{})}));};
  $("care-contact-form").onsubmit=e=>{e.preventDefault();attempt(()=>act({op:"contact.save",name:$("care-contact-name").value,contact_hint:$("care-contact-hint").value}));};
  $("care-urgent").onclick=()=>attempt(()=>command("紧急求助"));
  $("care-need-form").onsubmit=e=>{e.preventDefault();attempt(()=>act({op:"need.add",title:$("care-need-text").value}));};
  $("care-checkin-form").onsubmit=e=>{e.preventDefault();attempt(()=>act({op:"checkin.create",feeling:$("care-checkin-status").value,note:$("care-checkin-note").value}));};
  $("care-filter").oninput=renderCatalog;domainFilter.onchange=renderCatalog;
  
  for(const [id,cls]of [["care-large","care-large"],["care-contrast","care-contrast"]]){let saved=false;try{saved=localStorage.getItem(cls)==="true";}catch{}document.body.classList.toggle(cls,saved);$(id).setAttribute("aria-pressed",String(saved));$(id).onclick=()=>{const enabled=document.body.classList.toggle(cls);$(id).setAttribute("aria-pressed",String(enabled));try{localStorage.setItem(cls,String(enabled));}catch{}attempt(()=>act({op:"profile.update",changes:cls==="care-large"?{text_scale:enabled?1.5:1}:{high_contrast:enabled}}));};}
  function stopScan(){clearInterval(scanTimer);scanTimer=null;scanTarget?.classList.remove("care-scan-target");scanTarget=null;$("care-scan").textContent="开启单键扫描";$("care-scan").setAttribute("aria-pressed","false");}
  function advanceScan(){scanTarget?.classList.remove("care-scan-target");const targets=[...section.querySelectorAll("button,input,select,textarea")].filter(n=>!n.disabled&&n.getClientRects().length);scanTarget=targets[scanIndex++%targets.length];scanTarget?.classList.add("care-scan-target");scanTarget?.scrollIntoView({block:"nearest"});}
  $("care-scan").onclick=()=>{if(scanTimer){stopScan();return;}scanIndex=0;advanceScan();scanTimer=setInterval(advanceScan,1000*(snapshot?.profile?.switch_scan_seconds||3));$("care-scan").textContent="关闭单键扫描";$("care-scan").setAttribute("aria-pressed","true");};
  window.addEventListener("keydown",e=>{if(!scanTimer)return;if(e.key==="Escape"){e.preventDefault();stopScan();}else if(e.code==="Space"&&!e.repeat){e.preventDefault();const target=scanTarget;stopScan();if(target?.tagName==="BUTTON")target.click();else target?.focus();}});
  window.addEventListener("patrol:view",e=>{if(e.detail==="assistive")attempt(load);else stopScan();});
  let lastLocations="";
  window.addEventListener("patrol:state",e=>{const s=e.detail;$("care-mode").textContent=s.mode==="mock"?"软件演示 · 未连接真实硬件":"ROS 2 接口模式";const key=JSON.stringify(s.locations);if(key!==lastLocations){lastLocations=key;for(const id of ["care-device-room","care-from","care-to"]){const sel=$(id);sel.replaceChildren();for(const [k,p]of Object.entries(s.locations||{})){const opt=el("option",p.label);opt.value=k;sel.append(opt);}}$("care-from").value=s.locations.living_room?"living_room":"home";$("care-to").value=s.locations.bedroom?"bedroom":"home";$("care-device-room").value=s.locations.bedroom?"bedroom":"home";}$("care-robot").textContent=`机器人：${s.state} · ${s.robot?.held_payload?"仍持有 "+s.robot.held_payload:"无已确认载荷"}${s.mission?.error?" · "+s.mission.error:""}`;});
  for(const item of ["手机","遥控器","纸巾","眼镜","空水杯","密封饮用水","毛巾","书","钥匙"])$("care-item").append(el("option",item));
  setInterval(()=>{if(!section.hidden&&!scanTimer&&!section.querySelector(".care-record:focus-within"))attempt(load);},5000);
  window.AssistiveUI={act,message,command,stopScan,refresh:load,getSnapshot:()=>snapshot};
  window.addEventListener("pagehide",stopScan);
  if(!location.hash){location.hash="assistive";U.switchView("assistive");}
  if(location.hash==="#assistive") {U.switchView("assistive");attempt(load);}
})();
