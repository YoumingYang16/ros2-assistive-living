/* Run only against a separately started mock service. No microphone or speaker. */
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const base = process.env.UI_URL || 'http://127.0.0.1:8773';
(async () => {
  const browser = await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE ? {executablePath:process.env.BROWSER_EXECUTABLE} : {})});
  const page = await browser.newPage({viewport:{width:1440,height:1100}});
  const errors = [], checkpoints = [];
  page.on('pageerror', error => errors.push(error.message));
  async function shot(name) {
    if (!process.env.UI_ARTIFACTS) return;
    fs.mkdirSync(process.env.UI_ARTIFACTS,{recursive:true});
    await page.screenshot({path:path.join(process.env.UI_ARTIFACTS,name),fullPage:true,animations:'disabled'});
  }
  try {
    const initial = await (await page.request.get(base+'/api/state')).json();
    assert.equal(initial.mode,'mock'); assert.equal(initial.version,'7.0.0');
    await page.goto(base+"/#mission",{waitUntil:'networkidle'});
    await page.getByText('服务已连接',{exact:true}).waitFor();
    await page.locator('[data-view="scenarios"]:visible').first().click();
    await page.locator('#scenario-catalog input').last().waitFor();
    assert.equal(await page.locator('#scenario-catalog input').count(),6);
    const baseline = (await (await page.request.get(base+'/api/metrics')).json()).missions_total;
    await page.locator('#scenario-preflight').click();
    await page.locator('#scenario-preflight-result strong').waitFor();
    assert.match(await page.locator('#scenario-preflight-result').innerText(),/配置超时合计/);
    const [response] = await Promise.all([
      page.waitForResponse(r=>r.url().endsWith('/api/scenarios/run')),
      page.locator('#scenario-run').click()
    ]);
    const report=await response.json(); assert.equal(report.ok,true); assert.equal(report.comparison.length,6);
    await page.locator('.scenario-table tbody tr').last().waitFor();
    assert.equal(await page.locator('.scenario-table tbody tr').count(),6);
    assert.match(await page.locator('#scenario-results').innerText(),/目标达成/);
    assert.match(await page.locator('#scenario-results').innerText(),/目标未知/);
    assert.equal((await (await page.request.get(base+'/api/metrics')).json()).missions_total,baseline);
    checkpoints.push('six scenario outcomes shown with production engine results; live history unchanged');
    const [download] = await Promise.all([page.waitForEvent('download'),page.locator('#scenario-export').click()]);
    assert.match(download.suggestedFilename(),/^scenario-.*\.json$/);
    checkpoints.push('preflight is read-only, current editor draft imports, comparison report exports');
    await shot('v4-scenarios-desktop.png');
    await page.request.post(base+'/api/queue/control',{data:{action:'pause'}});
    const queued=await (await page.request.post(base+'/api/queue',{data:{text:'去会议室',request_id:'v4-ui-'+Date.now()}})).json();
    assert.equal(queued.ok,true);
    await page.locator('[data-view="queue"]:visible').first().click();
    await page.waitForFunction(id=>[...document.querySelector('#queue-edit-job').options].some(option=>option.value===id),queued.job.id);
    await page.locator('#queue-edit-job').selectOption(queued.job.id);
    await page.locator('#queue-edit-priority').fill('91');
    await page.locator('#queue-edit-repeat').selectOption('interval');
    await page.locator('#queue-edit-interval').fill('120');
    await page.locator('#queue-edit-save').click();
    await page.getByText('已保存调整并记录审计事件。',{exact:true}).waitFor();
    const queue=await (await page.request.get(base+'/api/queue')).json();
    const edited=queue.jobs.find(job=>job.id===queued.job.id);
    assert.equal(edited.priority,91); assert.equal(edited.repeat.interval_seconds,120);
    await page.request.post(base+`/api/queue/${queued.job.id}/cancel`,{data:{}});
    checkpoints.push('queued job editor persists priority and recurrence changes to audited backend');
    await page.setViewportSize({width:390,height:900});
    await page.locator('[data-view="scenarios"]:visible').first().click();
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await shot('v4-scenarios-mobile.png');
    await page.locator('#scenario-current').click();
    assert.ok(JSON.parse(await page.locator('#scenario-workflow').inputValue()).steps.length);
    assert.equal(await page.locator('#scenario-export').isDisabled(),true);
    assert.match(await page.locator('#scenario-results').innerText(),/输入已更新/);
    await page.locator('[data-view="queue"]:visible').first().click();
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    checkpoints.push('new scenario and queue edit views fit 390px without horizontal page overflow');
    assert.deepEqual(errors,[]);
    checkpoints.push('no browser JavaScript exceptions');
    const result={version:'7.0.0',generated_at:new Date().toISOString(),ok:true,checkpoints,microphone:false,speaker:false,hardware:false};
    if(process.env.UI_REPORT) fs.writeFileSync(process.env.UI_REPORT,JSON.stringify(result,null,2)+'\n');
    console.log(JSON.stringify(result,null,2));
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
