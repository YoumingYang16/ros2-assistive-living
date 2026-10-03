/* Local mock server, fake speech input. No microphone, devices or external messages. */
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const assert=require('node:assert/strict'), fs=require('node:fs'), path=require('node:path');
const base=process.env.UI_URL||'http://127.0.0.1:8773';
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_EXECUTABLE});
  const page=await browser.newPage({viewport:{width:1440,height:1000},timezoneId:'Asia/Hong_Kong'});
  const errors=[],checks=[];page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{
    window.__dailyVoice=[];
    class FakeRecognition{
      constructor(){window.__dailyVoice.push(this);}
      start(){this.onstart?.();} stop(){this.onend?.();} abort(){this.onend?.();}
      emit(text,final=true){const result=[{transcript:text}];result.isFinal=final;this.onresult?.({results:[result]});}
    }
    window.SpeechRecognition=window.webkitSpeechRecognition=FakeRecognition;
  });
  async function get(route){return(await page.request.get(base+route)).json();}
  async function act(data){const r=await page.request.post(base+'/api/assistive/action',{data});const json=await r.json();assert.equal(r.status(),200,JSON.stringify(json));return json.assistive.record;}
  async function record(id){return(await get('/api/assistive/history/'+id)).record;}
  async function command(text){await page.locator('#care-text').fill(text);const response=page.waitForResponse(r=>r.url().endsWith('/api/command')&&r.request().method()==='POST');await page.locator('#care-command-form button[type=submit]').click();const data=await(await response).json();assert.equal(data.ok,true,JSON.stringify(data));await page.waitForFunction(()=>!document.querySelector('#care-text').disabled);return data;}
  async function image(name,selector){if(!process.env.UI_ARTIFACTS)return;fs.mkdirSync(process.env.UI_ARTIFACTS,{recursive:true});await page.locator(selector).screenshot({path:path.join(process.env.UI_ARTIFACTS,name)});}
  async function saveEditor(){const response=page.waitForResponse(r=>r.url().endsWith('/api/assistive/action'));await page.locator('#daily-edit-save').click();return(await response).json();}
  try{
    assert.equal((await get('/api/state')).mode,'mock');
    await page.goto(base+'/#assistive',{waitUntil:'networkidle'});
    await page.locator('#daily-history-kind option').nth(9).waitFor({state:'attached'});
    assert.equal(await page.evaluate(()=>window.__dailyVoice.length),0);
    await page.locator('#care-reminder-title').fill('每周生活安排V7');
    await page.locator('#daily-create-mode').selectOption('weekly');
    for(const day of [1,3,5])await page.locator(`#daily-create-weekdays input[value="${day}"]`).check();
    await page.locator('#daily-create-clock').fill('08:30');
    const future=new Date(Date.now()+30*86400000+8*3600000).toISOString().slice(0,16);
    await page.locator('#daily-create-end').fill(future);
    let response=page.waitForResponse(r=>r.url().endsWith('/api/assistive/action'));
    await page.locator('#care-reminder-form button[type=submit]').click();
    let data=await(await response).json();assert.equal(data.ok,true,JSON.stringify(data));let reminder=data.assistive.record;
    assert.deepEqual(reminder.calendar.weekdays,[1,3,5]);assert.equal(reminder.calendar.local_time,'08:30');assert.ok(reminder.end_at);
    let row=page.locator('#care-reminders article').filter({hasText:'每周生活安排V7'});
    await row.getByRole('button',{name:'修改提醒',exact:true}).click();
    await page.locator('#daily-edit-title').fill('每周饮水安排V7');
    await image('v7-reminder-calendar.png','#daily-editor');
    assert.equal((await saveEditor()).ok,true);
    await page.locator('#daily-editor').waitFor({state:'hidden'});
    let updated=await record(reminder.id);assert.equal(updated.due_at,reminder.due_at);assert.deepEqual(updated.calendar,reminder.calendar);
    checks.push('weekly calendar creation with cutoff; title-only editing preserves exact due time and cadence');
    row=page.locator('#care-reminders article').filter({hasText:'每周饮水安排V7'});
    await row.getByRole('button',{name:'修改提醒',exact:true}).click();
    await act({op:'reminder.update',id:reminder.id,title:'其他会话已修改V7',expected_revision:updated.revision});
    await page.locator('#daily-edit-title').fill('过期覆盖V7');
    assert.equal((await saveEditor()).ok,false);
    assert.equal((await record(reminder.id)).title,'其他会话已修改V7');
    assert.equal(await page.locator('#daily-editor').isVisible(),true);
    await page.locator('#daily-edit-reload').click();
    await page.waitForFunction(()=>document.querySelector('#daily-edit-title').value==='其他会话已修改V7');
    await page.locator('#daily-edit-mode').selectOption('once');
    await page.locator('#daily-edit-time').fill(new Date(Date.now()+86400000+8*3600000).toISOString().slice(0,16));
    assert.equal((await saveEditor()).ok,true);
    await page.locator('#daily-editor').waitFor({state:'hidden'});
    updated=await record(reminder.id);assert.equal(updated.calendar??null,null);assert.equal(updated.repeat_seconds??null,null);
    checks.push('concurrent edit conflict remains visible, reload recovers, weekly reminder can become one-off');
    await page.locator('#daily-history-kind').selectOption('reminder');
    await page.locator('#daily-history-query').fill('其他会话已修改V7');
    await page.locator('#daily-history-filter button[type=submit]').click();
    await page.waitForFunction(()=>document.querySelector('#daily-history-status').textContent.includes('共 1 条'));
    await page.locator('#daily-history-list button').first().click();
    await page.getByRole('button',{name:'修改这条提醒',exact:true}).click();
    await page.locator('#daily-edit-title').fill('从历史修改V7');
    assert.equal((await saveEditor()).ok,true);
    await page.locator('#daily-editor').waitFor({state:'hidden'});
    await page.waitForFunction(()=>document.querySelector('#daily-history-detail h3')?.textContent==='从历史修改V7');
    checks.push('editing from history refreshes the open detail and its revision after saving');
    for(let i=0;i<31;i++)await act({op:'need.add',title:'分页用品V7 '+String(i).padStart(2,'0')});
    await page.locator('#daily-history-kind').selectOption('need');
    await page.locator('#daily-history-query').fill('分页用品V7');
    await page.locator('#daily-history-filter button[type=submit]').click();
    await page.waitForFunction(()=>document.querySelector('#daily-history-status').textContent.includes('共 31 条'));
    assert.equal(await page.locator('#daily-history-list article').count(),25);
    const firstTitles=await page.locator('#daily-history-list strong').allTextContents();
    await page.locator('#daily-history-limit').selectOption('10');
    await page.locator('#daily-history-next').click();
    await page.waitForFunction(()=>document.querySelector('#daily-history-status').textContent.includes('第 2 页'));
    assert.equal(await page.locator('#daily-history-list article').count(),6);
    assert.ok((await page.locator('#daily-history-list strong').allTextContents()).every(x=>!firstTitles.includes(x)));
    await page.locator('#daily-history-list button').first().click();
    await page.locator('#daily-history-detail h3').waitFor();
    const downloadPromise=page.waitForEvent('download');await page.getByRole('button',{name:'下载这条记录',exact:true}).click();
    assert.match((await downloadPromise).suggestedFilename(),/^living-record-/);
    await image('v7-history-desktop.png','#daily-history');
    await page.locator('#daily-history-state').selectOption('completed');
    await page.locator('#daily-history-filter button[type=submit]').click();
    await page.waitForFunction(()=>document.querySelector('#daily-history-status').textContent.includes('共 0 条'));
    checks.push('complete record history paging with committed filters, kind/state/text filters, details and explicit local download');
    const one=await act({op:'reminder.create',title:'语音同名V7',delay_seconds:0});
    const two=await act({op:'reminder.create',title:'语音同名V7',delay_seconds:0});
    const clarified=await command('确认语音同名V7提醒');assert.equal(clarified.needs_clarification,true);assert.equal(clarified.options.length,2);
    await page.locator('#care-draft button').nth(1).waitFor();
    await page.locator('#care-voice').click();
    await page.locator('#mic-button').click();
    await page.evaluate(()=>window.__dailyVoice.at(-1).emit('第',false));
    assert.equal((await record(one.id)).acknowledged_count,0);assert.equal((await record(two.id)).acknowledged_count,0);
    await page.evaluate(()=>window.__dailyVoice.at(-1).emit('第二个'));
    await page.waitForFunction(()=>document.querySelector('#command-input').value==='第二个');
    response=page.waitForResponse(r=>r.url().endsWith('/api/command'));
    await page.locator('#send-button').click();data=await(await response).json();
    assert.equal(data.assistive.record.state,'acknowledged');
    assert.equal((await record(one.id)).acknowledged_count+(await record(two.id)).acknowledged_count,1);
    const previewed=await act({op:'reminder.create',title:'预览绑定V7',delay_seconds:0});
    await page.locator('#command-input').fill('确认预览绑定V7提醒');
    await page.locator('#preview-button').click();
    await page.locator('#plan-preview').waitFor({state:'visible'});
    assert.equal((await record(previewed.id)).acknowledged_count,0);
    await act({op:'reminder.cancel',id:previewed.id});
    const replacement=await act({op:'reminder.create',title:'预览绑定V7',delay_seconds:0});
    response=page.waitForResponse(r=>r.url().endsWith('/api/command'));
    await page.locator('#send-button').click();data=await(await response).json();
    assert.match(data.message,/旧选择|变化/);
    assert.equal((await record(replacement.id)).acknowledged_count,0);
    await page.evaluate(()=>window.PatrolUI.switchView('assistive'));
    const stale=await act({op:'reminder.create',title:'旧列表保护V7',delay_seconds:0});
    await page.locator('#care-refresh').click();
    const staleButton=page.locator('#care-reminders article').filter({hasText:'旧列表保护V7'}).getByRole('button',{name:'我已知晓',exact:true});
    await staleButton.focus();
    await act({op:'reminder.update',id:stale.id,title:'旧列表已修改V7'});
    response=page.waitForResponse(r=>r.url().endsWith('/api/assistive/action'));
    await staleButton.click();assert.equal((await(await response).json()).ok,false);
    assert.equal((await record(stale.id)).acknowledged_count,0);
    checks.push('main console preview binds the record; stale preview and stale reminder buttons cannot affect a replacement or revised record');
    const waiting=await act({op:'wellbeing.start',seconds:1800});
    assert.equal((await command('我在')).assistive.record.state,'completed');
    assert.equal((await record(waiting.id)).state,'completed');
    const need=await act({op:'need.add',title:'语音纸巾V7'});
    assert.equal((await command('语音纸巾V7已备好')).assistive.record.state,'completed');
    assert.equal((await record(need.id)).state,'completed');
    checks.push('same session across care and voice console; fake interim speech does not mutate; spoken ordinal selects only one reminder; wellbeing and supplies finish');
    await page.locator('#daily-history-state').selectOption('');
    await page.locator('#daily-history-limit').selectOption('10');
    await page.locator('#daily-history-filter button[type=submit]').click();
    await page.waitForFunction(()=>document.querySelector('#daily-history-list').children.length===10);
    await page.setViewportSize({width:390,height:844});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await image('v7-history-mobile.png','#daily-history');
    assert.deepEqual(errors,[]);checks.push('390px layout has no horizontal overflow or browser errors');
    const report={ok:true,scope:'local_mock_and_fake_speech_only',checkpoints:checks,page_errors:errors};
    if(process.env.UI_REPORT)fs.writeFileSync(process.env.UI_REPORT,JSON.stringify(report,null,2));
    console.log(JSON.stringify(report));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
