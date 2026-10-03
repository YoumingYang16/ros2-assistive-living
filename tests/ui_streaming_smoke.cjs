/* Browser UI integration with fake media and ASR endpoints; no device access. */
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const assert=require('node:assert/strict'),fs=require('node:fs');
async function run(){
 const browser=await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE?{executablePath:process.env.BROWSER_EXECUTABLE}:{})}),page=await browser.newPage();const errors=[],requests=[],checkpoints=[];
 page.on('pageerror',error=>errors.push(error.message));
 try{
  await page.addInitScript(()=>{
   window.__mediaCalls=0;window.__trackStops=0;window.__worklets=[];
   Object.defineProperty(navigator,'mediaDevices',{value:{getUserMedia:async()=>{window.__mediaCalls++;return{getTracks:()=>[{stop(){window.__trackStops++;}}]};}}});
   class Context{constructor(){this.state='running';this.audioWorklet={addModule:async()=>{}};this.destination={};}createMediaStreamSource(){return{connect(){},disconnect(){}};}async resume(){}async close(){this.state='closed';}}
   class Worklet{constructor(){window.__worklets.push(this);this.port={postMessage:message=>{if(message.type==='flush')this.port.onmessage({data:{type:'flushed'}});}};}connect(){}disconnect(){}}
   window.AudioContext=Context;window.AudioWorkletNode=Worklet;
  });
  let segment='去会议室',created=0;const result=state=>({ok:true,state,session_id:'fixture'+created,partial:'',text:segment,segments:[{id:'seg001',text:segment}],simulated:true,requires_confirmation:true});
  await page.route('**/api/voice/**',async route=>{const request=route.request(),url=request.url();requests.push({url,method:request.method(),sequence:request.headers()['x-audio-sequence']});let body;
   if(url.endsWith('/capabilities'))body={ok:true,asr:[{id:'vosk',available:true,local:true,simulated:true}]};
   else if(url.endsWith('/sessions')){created++;segment='去会议室';body={ok:true,session_id:'fixture'+created};}
   else if(url.endsWith('/chunk'))body={...result('listening'),partial:'去会议'};
   else if(url.endsWith('/finish'))body=result('finished');
   else if(url.endsWith('/correct')){segment=request.postDataJSON().text;body=result('finished');}
   else if(request.method()==='DELETE')body={ok:true,state:'cancelled',text:''};else throw new Error('Unexpected ASR route '+url);
   await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
  });
  let missionPosts=0;page.on('request',request=>{if(request.method()==='POST'&&/\/api\/(command|workflow\/submit|queue)$/.test(request.url()))missionPosts++;});
  await page.goto((process.env.UI_URL||'http://127.0.0.1:8773')+'/#mission',{waitUntil:'networkidle'});assert.equal(await page.evaluate(()=>window.__mediaCalls),0);await page.locator('#voice-provider').selectOption('vosk');assert.equal(await page.evaluate(()=>window.__mediaCalls),0);await page.locator('#stream-start').click();await page.locator('#stream-finish').waitFor({state:'visible'});assert.equal(await page.evaluate(()=>window.__mediaCalls),1);
  await page.evaluate(()=>{window.__worklets[0].port.onmessage({data:{type:'pcm',buffer:new ArrayBuffer(6400)}});});await page.locator('#stream-state').filter({hasText:'正在识别'}).waitFor();await page.locator('#stream-finish').click();await page.waitForFunction(()=>document.querySelector('#command-input').value==='去会议室');assert.equal(await page.evaluate(()=>window.__trackStops),1);assert.equal(missionPosts,0);checkpoints.push('selecting local provider does not capture; explicit start streams ordered PCM; finish fills editable text without execution');
  await page.getByLabel('纠正 seg001').fill('去仓库');await page.getByRole('button',{name:'纠正片段',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#command-input').value==='去仓库');assert.equal(missionPosts,0);checkpoints.push('segment correction revises complete transcript without posting a task');
  await page.locator('#stream-start').click();await page.locator('#stream-finish').waitFor({state:'visible'});await page.locator('[data-view=queue]:visible').first().click();await page.waitForFunction(()=>window.PatrolStreamingVoice.state==='cancelled');assert.equal(await page.evaluate(()=>window.__trackStops),2);assert.ok(requests.some(item=>item.method==='DELETE'));checkpoints.push('leaving task view immediately stops fake tracks and cancels server session');
  await page.locator('[data-view=mission]:visible').first().click();await page.locator('#stream-start').click();await page.locator('#stream-finish').waitFor({state:'visible'});await page.evaluate(()=>window.dispatchEvent(new CustomEvent('patrol:speaking')));await page.waitForFunction(()=>window.PatrolStreamingVoice.state==='cancelled');assert.equal(await page.evaluate(()=>window.__trackStops),3);checkpoints.push('speech-output interrupt cancels local streaming to avoid recapturing output');assert.deepEqual(errors,[]);assert.equal(missionPosts,0);
  const report={version:'7.0.0',generated_at:new Date().toISOString(),ok:true,scope:'Browser integration with explicit fake getUserMedia, AudioContext and ASR HTTP fixtures; no microphone, speaker or real ASR',checkpoints};if(process.env.STREAM_UI_REPORT)fs.writeFileSync(process.env.STREAM_UI_REPORT,JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report,null,2));
 }finally{await browser.close();}
}
run().catch(error=>{console.error(error);process.exitCode=1;});
