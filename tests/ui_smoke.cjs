/* Optional end-to-end UI check against an explicitly started mock server.
   npm install playwright, then UI_URL=http://127.0.0.1:8773 node tests/ui_smoke.cjs
   All browser speech interfaces are replaced with fakes before page load. */
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const url = process.env.UI_URL || 'http://127.0.0.1:8773';
const artifacts = process.env.UI_ARTIFACTS;
const checkpoints = [];

function silentWav() {
  const b = Buffer.alloc(44 + 320); b.write('RIFF'); b.writeUInt32LE(b.length - 8, 4); b.write('WAVEfmt ', 8);
  b.writeUInt32LE(16, 16); b.writeUInt16LE(1, 20); b.writeUInt16LE(1, 22); b.writeUInt32LE(16000, 24);
  b.writeUInt32LE(32000, 28); b.writeUInt16LE(2, 32); b.writeUInt16LE(16, 34); b.write('data', 36); b.writeUInt32LE(320, 40); return b;
}
async function run() {
  const browser = await chromium.launch({headless: true, ...(process.env.BROWSER_EXECUTABLE ? {executablePath: process.env.BROWSER_EXECUTABLE} : {})});
  const page = await browser.newPage({viewport: {width: 1440, height: 1100}});
  const pageErrors = []; page.on('pageerror', error => pageErrors.push(error.message));
  let savedConfig;
  async function screenshot(name) { if (artifacts) { fs.mkdirSync(artifacts, {recursive: true}); await page.screenshot({path: path.join(artifacts, name), fullPage: true, animations: 'disabled'}); } }
  await page.addInitScript(() => {
    window.__voiceInstances = [];
    class FakeRecognition {
      constructor() { window.__voiceInstances.push(this); }
      start() { this.onstart?.(); }
      stop() { this.onend?.(); }
      abort() { this.onend?.(); }
      emit(text, final = true) { const result = [{transcript: text}]; result.isFinal = final; this.onresult?.({results: [result]}); }
    }
    window.SpeechRecognition = FakeRecognition; window.webkitSpeechRecognition = FakeRecognition;
  });
  const switchView = async view => { await page.locator(`[data-view="${view}"]:visible`).first().click(); await page.locator(`#view-${view}`).waitFor({state: 'visible'}); };
  async function sendAndWait() {
    const [response] = await Promise.all([page.waitForResponse(response => response.url().endsWith('/api/command') && response.request().method() === 'POST'), page.locator('#send-button').click()]);
    const data = await response.json(); assert.equal(data.ok, true); assert.ok(data.state?.mission?.id);
    const id = data.state.mission.id.slice(0, 10);
    await page.waitForFunction(id => document.querySelector('#mission-id').textContent.includes(id), id);
    await page.waitForFunction(() => document.querySelector('#mission-state').textContent === '任务完成', {timeout: 20000});
  }
  try {
    const stateResponse = await page.request.get(url + '/api/state');
    assert.equal((await stateResponse.json()).mode, 'mock', 'UI test may only run against mock backend');
    await page.request.post(url + '/api/control', {data: {action: 'stop'}});
    savedConfig = (await (await page.request.get(url + '/api/config')).json()).config;
    await page.goto(url+"/#mission", {waitUntil: 'networkidle'});
    await page.getByText('服务已连接', {exact: true}).waitFor();
    assert.equal(await page.evaluate(() => window.__voiceInstances.length), 0);
    checkpoints.push('page load does not activate microphone');
    await page.locator('[data-command*="没找到"]').click();
    await page.locator('#preview-button').click();
    await page.locator('#plan-preview').waitFor({state: 'visible'});
    assert.ok(await page.locator('#preview-steps .branch-note').count() >= 2);
    await sendAndWait();
    assert.ok(await page.locator('#timeline li.skipped').count() >= 2);
    assert.ok((await page.locator('#mission-report').innerText()).includes('跳过'));
    await page.waitForFunction(() => Math.abs(document.querySelector('#progress-fill').getBoundingClientRect().width - document.querySelector('#progress-track').getBoundingClientRect().width) < 1);
    await screenshot('v2-mission.png');
    checkpoints.push('conditional plan, observed branch, skipped steps and report');
    await switchView('history');
    await page.locator('.history-item').first().click();
    await page.locator('#history-export').waitFor({state: 'visible'});
    assert.ok((await page.locator('#history-detail').innerText()).includes('观测时间'));
    const exportPath = await page.locator('#history-export').getAttribute('href');
    assert.equal((await page.request.get(url + exportPath)).status(), 200);
    await screenshot('v2-history.png');
    checkpoints.push('persistent history details and JSON export');
    await switchView('mission');
    const missionBeforeClarification = await page.locator('#mission-id').innerText();
    await page.locator('#command-input').fill('去会议室还是仓库'); await page.locator('#send-button').click();
    await page.locator('#clarification').waitFor({state:'visible'});
    assert.equal(await page.locator('#mission-id').innerText(), missionBeforeClarification);
    assert.equal(await page.locator('#clarification-options button').count(), 2);
    const clarifiedResponse = page.waitForResponse(response => response.url().endsWith('/api/command'));
    await page.locator('#clarification-options').getByRole('button', {name:'会议室', exact:true}).click();
    const clarified = await (await clarifiedResponse).json(); assert.ok(clarified.state.mission.id);
    await page.waitForFunction(id => document.querySelector('#mission-id').textContent.includes(id), clarified.state.mission.id.slice(0,10));
    await page.waitForFunction(() => document.querySelector('#mission-state').textContent === '任务完成');
    await page.locator('#clarification').waitFor({state:'hidden'});
    checkpoints.push('clarification does not move; selecting a destination executes the resolved task');
    await switchView('settings');
    await page.waitForFunction(() => document.querySelector('#config-editor').value.includes('locations'));
    await page.locator('#config-editor').fill('{ broken JSON'); await page.locator('#config-save').click();
    await page.locator('#config-feedback.error').waitFor();
    const updated = structuredClone(savedConfig); updated.locations.home.aliases.push('测试起点');
    await page.locator('#config-editor').fill(JSON.stringify(updated)); await page.locator('#config-save').click();
    await page.locator('#config-feedback.success').waitFor();
    assert.ok((await (await page.request.get(url + '/api/config')).json()).config.locations.home.aliases.includes('测试起点'));
    await page.locator('#config-editor').fill(JSON.stringify(savedConfig)); await page.locator('#config-save').click();
    await page.locator('#config-feedback.success').waitFor();
    await screenshot('v2-settings.png');
    checkpoints.push('invalid JSON rejected, configuration saved and restored');
    await switchView('health'); await page.locator('.health-tile').first().waitFor();
    assert.equal(await page.locator('.health-tile').count(), 4); assert.ok(await page.locator('.metric-card').count() >= 4);
    await screenshot('v2-health.png'); checkpoints.push('runtime diagnostics and metrics');
    await switchView('mission');
    const posts = []; page.on('request', request => { if (request.url().endsWith('/api/command')) posts.push(request.postDataJSON()); });
    await page.locator('#mic-button').click();
    await page.evaluate(() => window.__voiceInstances.at(-1).emit('去会', false));
    assert.equal(posts.length, 0);
    await page.evaluate(() => window.__voiceInstances.at(-1).emit('去会议室然后返回起点'));
    await page.waitForFunction(() => document.querySelector('#mic-button').getAttribute('aria-pressed') === 'false');
    assert.equal(posts.length, 0); assert.equal(await page.locator('#command-input').inputValue(), '去会议室然后返回起点');
    checkpoints.push('mocked browser interim/final transcript needs manual send');
    await page.locator('#wav-input').setInputFiles({name: 'silence.wav', mimeType: 'audio/wav', buffer: silentWav()});
    await page.waitForFunction(() => !document.querySelector('#wav-input').disabled);
    assert.ok((await page.locator('#wav-status').innerText()).length > 5);
    assert.equal(posts.length, 0); checkpoints.push('WAV endpoint result is displayed without execution');
    let intercepted = 0; const ids = [];
    await page.route('**/api/command', async route => { ids.push(route.request().postDataJSON().request_id); if (++intercepted === 1) await route.abort('failed'); else await route.continue(); });
    await page.locator('#command-input').fill('去前台然后返回起点'); await page.locator('#send-button').click();
    await page.locator('#feedback.error').waitFor(); await page.waitForFunction(() => !document.querySelector('#send-button').disabled);
    await sendAndWait();
    assert.equal(ids.length, 2); assert.equal(ids[0], ids[1]); await page.unroute('**/api/command');
    checkpoints.push('uncertain HTTP retry preserves request_id');
    await page.locator('#handsfree-toggle').check();
    const voiceTask = page.waitForResponse(response => response.url().endsWith('/api/command'));
    await page.evaluate(() => window.__voiceInstances.at(-1).emit('开始巡逻两圈'));
    assert.equal((await (await voiceTask).json()).ok, true);
    const listeningCount = await page.evaluate(() => window.__voiceInstances.length);
    await page.waitForFunction(count => window.__voiceInstances.length > count, listeningCount);
    const voiceStop = page.waitForResponse(response => response.url().endsWith('/api/control'));
    await page.evaluate(() => window.__voiceInstances.at(-1).emit('停止'));
    assert.equal((await (await voiceStop).json()).ok, true);
    await page.waitForFunction(() => document.querySelector('#mission-state').textContent === '已停止');
    await switchView('health');
    assert.equal(await page.locator('#mic-button').getAttribute('aria-pressed'), 'false');
    assert.equal(await page.locator('#handsfree-toggle').isChecked(), false);
    await switchView('mission');
    checkpoints.push('explicit handsfree starts a task; standalone voice stop uses control API');
    await page.setViewportSize({width: 390, height: 844});
    for (const view of ['mission', 'history', 'settings', 'health']) {
      await switchView(view);
      const widths = await page.evaluate(() => [document.documentElement.scrollWidth, innerWidth]);
      assert.ok(widths[0] <= widths[1], `${view}: ${widths}`);
    }
    await switchView('mission'); await screenshot('v2-mobile.png');
    assert.deepEqual(pageErrors, []); checkpoints.push('four mobile views have no overflow; no JavaScript errors');
    console.log(JSON.stringify({ok: true, checkpoints, screenshot_directory: artifacts || null}, null, 2));
  } finally {
    await page.request.post(url + '/api/control', {data: {action: 'stop'}}).catch(() => {});
    if (savedConfig) await page.request.put(url + '/api/config', {data: savedConfig}).catch(() => {});
    await browser.close();
  }
}
run().catch(error => { console.error(error); process.exitCode = 1; });
