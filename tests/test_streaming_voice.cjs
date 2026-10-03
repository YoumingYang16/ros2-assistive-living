/* Pure controller/worklet tests; never opens a microphone, browser or speaker. */
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
const {StreamingVoiceController}=require('../robot_voice_patrol/web/streaming-voice.js');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function harness(custom={}){
  const calls=[],results=[],errors=[],states=[],nodes=[],timers=new Map();let timer=0,stopped=0,closed=0,microphones=0;
  const stream={getTracks:()=>[{stop(){stopped++;}}]};
  class Context{constructor(){this.state='running';this.audioWorklet={addModule:async()=>{}};this.destination={};}createMediaStreamSource(){return{connect(){},disconnect(){}};}async resume(){}async close(){closed++;this.state='closed';}}
  class Worklet{constructor(){nodes.push(this);this.port={postMessage:message=>{if(message.type==='flush')this.port.onmessage({data:{type:'flushed'}});}};}connect(){}disconnect(){}}
  const send=async(url,body,method,headers)=>{calls.push({url,body,method,headers});if(custom.send){const response=await custom.send(url,body,method,headers);if(response!==undefined)return response;}if(url==='/api/voice/sessions')return{session_id:'fixture'};if(url.endsWith('/finish'))return{state:'finished',text:'去会议室',segments:[{id:'seg001',text:'去会议室'}]};return{state:'listening',partial:'去会',segments:[]};};
  const controller=new StreamingVoiceController({request:send,onResult:value=>results.push(value),onError:value=>errors.push(value),onState:value=>states.push(value),AudioContext:Context,WorkletNode:Worklet,mediaDevices:{getUserMedia:()=>{microphones++;return custom.permission?custom.permission():Promise.resolve(stream);}},timers:{setTimeout(fn){const id=++timer;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);}}});
  return{controller,calls,results,errors,states,nodes,timers,stream,stopped:()=>stopped,closed:()=>closed,microphones:()=>microphones};
}
test('construction never opens microphone or creates server session',()=>{const h=harness();assert.equal(h.microphones(),0);assert.equal(h.calls.length,0);assert.equal(h.controller.state,'idle');});
test('PCM uploads serialize sequence, finish releases tracks and emits transcript only',async()=>{
  const h=harness();await h.controller.start();assert.equal(h.microphones(),1);
  h.nodes[0].port.onmessage({data:{type:'pcm',buffer:new ArrayBuffer(6400)}});h.nodes[0].port.onmessage({data:{type:'pcm',buffer:new ArrayBuffer(6400)}});
  await h.controller.finish();assert.deepEqual(h.calls.filter(c=>c.url.endsWith('/chunk')).map(c=>c.headers['X-Audio-Sequence']),['0','1']);assert.equal(h.controller.state,'finished');assert.equal(h.stopped(),1);assert.equal(h.closed(),1);assert.equal(h.results.at(-1).text,'去会议室');assert.ok(h.calls.every(c=>c.url.startsWith('/api/voice/')));assert.equal(h.timers.size,0);
});
test('cancel during permission wait stops late stream and does not reconnect',async()=>{
  let release;const h=harness({permission:()=>new Promise(resolve=>release=resolve)});const opening=h.controller.start();await tick();await h.controller.cancel();release(h.stream);await opening;assert.equal(h.stopped(),1);assert.equal(h.nodes.length,0);assert.equal(h.controller.sessionId,null);assert.equal(h.controller.state,'cancelled');
});
test('cancel discards late chunk responses and prevents finalization',async()=>{
  let release;const h=harness({send:url=>url.endsWith('/chunk')?new Promise(resolve=>release=resolve):undefined});await h.controller.start();h.controller.enqueue(new ArrayBuffer(4));await tick();await h.controller.cancel();release({state:'listening',partial:'must discard',segments:[]});await h.controller.pending;await h.controller.finish();assert.equal(h.results.length,0);assert.ok(h.calls.some(c=>c.method==='DELETE'));assert.ok(!h.calls.some(c=>c.url.endsWith('/finish')));
});
test('bounded backlog cancels session instead of retaining unlimited PCM',async()=>{
  let release;const h=harness({send:url=>url.endsWith('/chunk')?new Promise(resolve=>release=resolve):undefined});await h.controller.start();h.controller.enqueue(new ArrayBuffer(4));await tick();for(let i=0;i<8;i++)h.controller.enqueue(new ArrayBuffer(4));await tick();assert.equal(h.controller.state,'cancelled');assert.equal(h.stopped(),1);assert.ok(h.errors.some(message=>message.includes('上传速度不足')));release({});await h.controller.pending;
});
test('correction stays in ASR session and cannot call task APIs',async()=>{const h=harness();await h.controller.start();await h.controller.finish();await h.controller.correct('seg001','去仓库');assert.deepEqual(h.calls.at(-1).body,{segment_id:'seg001',text:'去仓库'});assert.ok(h.calls.at(-1).url.endsWith('/correct'));await h.controller.cancel();await assert.rejects(()=>h.controller.correct('seg001','文字'));});
test('48 kHz worklet resamples across render boundaries to signed little-endian 16 kHz',()=>{
  let Processor;const emitted=[];const context={sampleRate:48000,AudioWorkletProcessor:class{constructor(){this.port={postMessage:value=>emitted.push(value)};}},registerProcessor:(name,type)=>{assert.equal(name,'patrol-pcm');Processor=type;},ArrayBuffer,DataView};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../robot_voice_patrol/web/pcm-worklet.js'),'utf8'),context);const p=new Processor();let remaining=48000;while(remaining){const count=Math.min(128,remaining);p.process([[new Float32Array(count).fill(-.5)]]);remaining-=count;}p.port.onmessage({data:{type:'flush'}});const pcm=emitted.filter(value=>value.type==='pcm');assert.equal(pcm.reduce((n,item)=>n+item.buffer.byteLength,0),32000);assert.ok(pcm.every(item=>item.buffer.byteLength<=6400));assert.equal(new DataView(pcm[0].buffer).getInt16(0,true),-16384);assert.equal(emitted.at(-1).type,'flushed');
});
