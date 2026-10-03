"use strict";

/* Browser voice lifecycle. It never starts recording from construction, page
   load, a server response, or a restored setting. start() requires a UI gesture. */
(function (root) {
  const stopOnly = text => /^(停止|停下|停一下|停止任务|取消任务|紧急停止)$/.test(String(text).replace(/[\s，。！？,.!?]/g, ""));
  class VoiceController {
    constructor({Recognition, onState = () => {}, onTranscript = () => {}, onError = () => {}, onFinal = () => {}, timers = root}) {
      this.Recognition = Recognition;
      this.onState = onState; this.onTranscript = onTranscript; this.onError = onError; this.onFinal = onFinal;
      this.timers = timers; this.generation = 0; this.state = "idle";
      this.active = false; this.handsfree = false; this.processing = false; this.speaking = false;
      this.failures = 0; this.recognition = null; this.restartTimer = null; this.limitTimer = null;
    }
    transition(state, message = "") { this.state = state; this.onState({state, message, active: this.active, handsfree: this.handsfree}); }
    clearTimers() { this.timers.clearTimeout(this.restartTimer); this.timers.clearTimeout(this.limitTimer); this.restartTimer = this.limitTimer = null; }
    stop() {
      this.active = false; this.handsfree = false; this.processing = false; this.speaking = false;
      this.generation++; this.clearTimers();
      const recognition = this.recognition; this.recognition = null;
      try { recognition?.abort(); } catch (_) {}
      this.transition("idle");
    }
    start({handsfree = false} = {}) {
      if (!this.Recognition) { this.onError("当前浏览器不支持语音识别，请输入文字或上传本地 WAV。"); return; }
      this.stop(); this.active = true; this.handsfree = handsfree; this.failures = 0;
      this.cycle();
    }
    schedule(delay = 450) {
      if (!this.active || !this.handsfree || this.processing || this.speaking || this.recognition || this.restartTimer) return;
      this.transition("backoff", "准备下一次聆听");
      this.restartTimer = this.timers.setTimeout(() => { this.restartTimer = null; this.cycle(); }, delay);
    }
    cycle() {
      if (!this.active || this.processing || this.speaking || this.recognition) return;
      const generation = ++this.generation;
      const recognition = new this.Recognition(); this.recognition = recognition;
      recognition.lang = "zh-CN"; recognition.continuous = false; recognition.interimResults = true; recognition.maxAlternatives = 1;
      let finalReceived = false, hadError = false;
      const current = () => generation === this.generation && this.active;
      this.transition("starting", "等待语音服务");
      recognition.onstart = () => { if (current()) this.transition("listening", "正在聆听"); };
      recognition.onresult = event => {
        if (!current() || this.speaking || finalReceived) return;
        const results = Array.from(event.results);
        const text = results.map(result => result[0].transcript).join("").trim();
        const final = results.length > 0 && results[results.length - 1].isFinal;
        this.onTranscript({text, final});
        if (!final || !text) return;
        finalReceived = true; this.failures = 0; this.processing = true;
        this.transition("processing", this.handsfree ? "正在处理指令" : "等待文字确认");
        try { recognition.stop(); } catch (_) {}
        Promise.resolve().then(() => current() ? this.onFinal(text, {autoSend: this.handsfree, priority: stopOnly(text)}) : undefined).catch(error => {
          this.onError(error?.message || "语音指令处理失败");
        }).finally(() => {
          if (!current()) return;
          this.processing = false;
          if (!this.handsfree) { this.active = false; this.transition("idle", "识别完成，等待确认"); }
          else this.schedule();
        });
        // Continue listening during a slow request so a subsequent complete stop
        // command can take the independent priority route in the application.
        if (this.handsfree) { this.processing = false; this.schedule(); }
      };
      recognition.onerror = event => {
        if (!current()) return;
        hadError = true;
        if (event.error === "aborted") return;
        const fatal = ["not-allowed", "service-not-allowed", "audio-capture", "language-not-supported"].includes(event.error);
        const messages = {"not-allowed": "麦克风权限被拒绝，请在浏览器设置中允许后手动重启。", "service-not-allowed": "浏览器语音服务被禁止，请使用文字或离线 WAV。", "audio-capture": "没有可用麦克风，请检查输入设备。", "network": "语音服务连接失败，浏览器识别可能需要联网。", "no-speech": "本轮未听到完整语音。", "language-not-supported": "当前语音服务不支持中文。"};
        this.failures++;
        this.onError(messages[event.error] || `语音识别失败：${event.error}`);
        if (fatal || this.failures >= 3) { this.stop(); this.onError("语音输入已停止，请点击按钮重新开启。"); }
      };
      recognition.onend = () => {
        if (!current()) return;
        this.timers.clearTimeout(this.limitTimer); this.limitTimer = null; this.recognition = null;
        if (!this.handsfree && !this.processing) { this.active = false; this.transition("idle"); return; }
        if (!finalReceived && !hadError) this.failures++;
        if (this.failures >= 3) { this.stop(); this.onError("连续三轮未获得可用语音，已停止自动聆听。"); return; }
        this.schedule(Math.min(4000, 450 * (2 ** this.failures)));
      };
      try {
        recognition.start();
        this.limitTimer = this.timers.setTimeout(() => {
          if (current() && !finalReceived) { hadError = true; this.failures++; recognition.abort(); }
        }, 20000);
      } catch (error) { this.stop(); this.onError(`无法启动语音服务：${error?.message || error}`); }
    }
    speechStarted() {
      this.speaking = true; this.generation++; this.clearTimers();
      const recognition = this.recognition; this.recognition = null;
      try { recognition?.abort(); } catch (_) {}
      if (this.active) this.transition("speaking", "播报期间暂停收音");
    }
    speechEnded() {
      this.speaking = false; this.processing = false;
      if (this.active && this.handsfree) this.schedule(650);
      else { this.active = false; this.transition("idle"); }
    }
  }
  root.VoiceController = VoiceController;
  root.isStopOnlyCommand = stopOnly;
  if (typeof module !== "undefined" && module.exports) module.exports = {VoiceController, isStopOnlyCommand: stopOnly};
})(typeof globalThis !== "undefined" ? globalThis : window);
