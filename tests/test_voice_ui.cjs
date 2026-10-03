/* State-machine tests. Fake recognition only; no browser or microphone opens. */
const test = require('node:test');
const assert = require('node:assert/strict');
const {VoiceController, isStopOnlyCommand} = require('../robot_voice_patrol/web/voice.js');

function harness(onFinal = () => {}) {
  const instances = [], states = [], errors = [], transcripts = [], timers = new Map();
  let timerId = 0;
  class Recognition {
    constructor() { instances.push(this); }
    start() { this.started = true; this.onstart?.(); }
    stop() { this.stopped = true; this.onend?.(); }
    abort() { this.aborted = true; this.onend?.(); }
    result(text, final = true) { const result = [{transcript: text}]; result.isFinal = final; this.onresult?.({results: [result]}); }
    fail(error) { this.onerror?.({error}); this.onend?.(); }
  }
  const controller = new VoiceController({Recognition, onFinal, onState: value => states.push(value),
    onError: message => errors.push(message), onTranscript: value => transcripts.push(value),
    timers: {setTimeout(fn, ms) { const id = ++timerId; timers.set(id, {fn, ms}); return id; }, clearTimeout(id) { timers.delete(id); }}});
  function nextTimer() {
    const [id, item] = [...timers.entries()].sort((a, b) => a[1].ms - b[1].ms)[0] || [];
    assert.ok(item, 'Expected a scheduled timer'); timers.delete(id); item.fn(); return item.ms;
  }
  return {controller, instances, states, errors, transcripts, timers, nextTimer};
}
const settle = () => new Promise(resolve => setImmediate(resolve));

test('constructing voice controller never starts a microphone', () => {
  const h = harness(); assert.equal(h.instances.length, 0); assert.equal(h.controller.active, false);
});
test('manual final text requires confirmation and interim never submits', async () => {
  const calls = []; const h = harness((text, options) => calls.push({text, ...options}));
  h.controller.start(); h.instances[0].result('去会', false);
  assert.equal(calls.length, 0); assert.equal(h.transcripts[0].final, false);
  h.instances[0].result('去会议室'); await settle();
  assert.deepEqual(calls, [{text: '去会议室', autoSend: false, priority: false}]);
  assert.equal(h.controller.active, false); assert.equal(h.timers.size, 0);
});
test('handsfree does not submit duplicate final events', async () => {
  const calls = []; const h = harness(text => calls.push(text));
  h.controller.start({handsfree: true}); h.instances[0].result('去会议室'); h.instances[0].result('去会议室');
  await settle(); assert.deepEqual(calls, ['去会议室']); h.controller.stop();
});
test('listening resumes during an unresolved request so stop can be prioritized', async () => {
  const calls = [];
  const h = harness((text, options) => { calls.push({text, ...options}); return new Promise(() => {}); });
  h.controller.start({handsfree: true}); h.instances[0].result('去会议室'); await settle();
  h.nextTimer(); assert.equal(h.instances.length, 2); h.instances[1].result('停止'); await settle();
  assert.equal(calls[1].priority, true); assert.equal(calls[1].autoSend, true); h.controller.stop();
});
test('TTS aborts recording, discards echo, then resumes after cooldown', async () => {
  const calls = []; const h = harness(text => calls.push(text));
  h.controller.start({handsfree: true}); const old = h.instances[0];
  h.controller.speechStarted(); assert.equal(old.aborted, true); old.result('去仓库'); await settle();
  assert.equal(calls.length, 0); assert.equal(h.timers.size, 0);
  h.controller.speechEnded(); const delay = h.nextTimer(); assert.ok(delay >= 600); assert.equal(h.instances.length, 2);
  h.controller.stop();
});
test('permission failure stops retries until another explicit start', () => {
  const h = harness(); h.controller.start({handsfree: true}); h.instances[0].fail('not-allowed');
  assert.equal(h.controller.active, false); assert.equal(h.timers.size, 0); assert.equal(h.instances.length, 1);
});
test('no-speech retry backoff stops after three unsuccessful rounds', () => {
  const h = harness(); h.controller.start({handsfree: true});
  h.instances[0].fail('no-speech'); const first = h.nextTimer();
  h.instances[1].fail('no-speech'); const second = h.nextTimer();
  assert.ok(second > first); h.instances[2].fail('no-speech');
  assert.equal(h.controller.active, false); assert.equal(h.timers.size, 0); assert.equal(h.instances.length, 3);
});
test('stopping immediately cancels a queued final callback', async () => {
  const calls = []; const h = harness(text => calls.push(text));
  h.controller.start({handsfree: true}); h.instances[0].result('去会议室'); h.controller.stop(); await settle();
  assert.equal(calls.length, 0); assert.equal(h.timers.size, 0);
});
test('only complete standalone stop commands have priority', () => {
  assert.equal(isStopOnlyCommand('停止任务！'), true);
  for (const text of ['不要停止', '去会议室然后停止', '如果没找到就停止', '停止后去仓库']) assert.equal(isStopOnlyCommand(text), false);
});
