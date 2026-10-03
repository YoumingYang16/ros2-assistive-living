"use strict";
(function(root){
  async function request(path,body,method="POST",headers={}){
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),10000);
    try{const response=await fetch(path,{method,signal:controller.signal,cache:"no-store",headers:{"Accept":"application/json","Content-Type":body instanceof ArrayBuffer?"application/octet-stream":"application/json",...headers},body:body===undefined?undefined:body instanceof ArrayBuffer?body:JSON.stringify(body)});const data=await response.json();if(!response.ok||data.ok===false)throw new Error(data.message||`语音请求失败 (${response.status})`);return data;}finally{clearTimeout(timer);}
  }
  class StreamingVoiceController{
    constructor({request:send=request,onState=()=>{},onResult=()=>{},onError=()=>{},mediaDevices=root.navigator?.mediaDevices,AudioContext=root.AudioContext||root.webkitAudioContext,WorkletNode=root.AudioWorkletNode,timers=root}={}){
      Object.assign(this,{send,onState,onResult,onError,mediaDevices,AudioContext,WorkletNode,timers});this.generation=0;this.state="idle";this.sessionId=null;this.pending=Promise.resolve();this.queued=0;this.sequence=0;
    }
    transition(state,message=""){this.state=state;this.onState({state,message});}
    async release(){this.timers.clearTimeout(this.limitTimer);this.limitTimer=null;this.source?.disconnect();this.source=null;this.node?.disconnect();this.node=null;this.stream?.getTracks().forEach(track=>track.stop());this.stream=null;const context=this.context;this.context=null;if(context&&context.state!=="closed")await context.close().catch(()=>{});}
    async start(){
      if(!["idle","finished","cancelled","error"].includes(this.state))return;
      if(!this.mediaDevices?.getUserMedia||!this.AudioContext||!this.WorkletNode){this.onError("当前浏览器不支持本地流式收音，请使用上传 WAV。");return;}
      const generation=++this.generation;this.transition("starting","正在建立本地语音会话");this.pending=Promise.resolve();this.sequence=0;this.queued=0;
      try{
        const session=await this.send("/api/voice/sessions",{sample_rate:16000,channels:1,provider:"vosk"});
        if(generation!==this.generation){await this.send(`/api/voice/sessions/${session.session_id}`,{},"DELETE");return;}
        this.sessionId=session.session_id;
        const stream=await this.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false});
        if(generation!==this.generation){stream.getTracks().forEach(track=>track.stop());return;}this.stream=stream;
        this.context=new this.AudioContext();await this.context.audioWorklet.addModule("/pcm-worklet.js");
        if(generation!==this.generation)return;
        this.node=new this.WorkletNode(this.context,"patrol-pcm");this.source=this.context.createMediaStreamSource(stream);
        this.node.port.onmessage=event=>{if(generation!==this.generation)return;if(event.data?.type==="flushed")this.flushDone?.();else if(event.data?.type==="pcm")this.enqueue(event.data.buffer,generation);};
        this.source.connect(this.node);this.node.connect(this.context.destination);await this.context.resume();
        if(generation!==this.generation)return;
        this.transition("listening","本地收音中 · 最长60秒");this.limitTimer=this.timers.setTimeout(()=>void this.finish(),59000);
      }catch(error){if(generation===this.generation){await this.cancel();this.transition("error");this.onError(error.message||"无法开启本地流式语音");}}
    }
    enqueue(buffer,generation=this.generation){
      if(generation!==this.generation||!["listening","finishing"].includes(this.state))return;
      if(this.queued>=8){void this.cancel();this.onError("上传速度不足，本次收音已取消，请检查连接后重试。");return;}
      this.queued++;const id=this.sessionId,sequence=this.sequence++;
      this.pending=this.pending.then(async()=>{if(generation!==this.generation)return;const result=await this.send(`/api/voice/sessions/${id}/chunk`,buffer,"POST",{"X-Audio-Sequence":String(sequence)});if(generation===this.generation)this.onResult(result);}).catch(async error=>{if(generation===this.generation){await this.cancel();this.onError(error.message);}}).finally(()=>{if(generation===this.generation)this.queued--;});
    }
    async finish(){
      if(this.state!=="listening")return;const generation=this.generation,id=this.sessionId;this.transition("finishing","正在整理识别文字");
      this.source?.disconnect();
      if(this.node){await new Promise(resolve=>{const timer=this.timers.setTimeout(resolve,1000);this.flushDone=()=>{this.timers.clearTimeout(timer);this.flushDone=null;resolve();};this.node.port.postMessage({type:"flush"});});}
      await this.release();await this.pending;if(generation!==this.generation)return;
      try{const result=await this.send(`/api/voice/sessions/${id}/finish`,{});if(generation!==this.generation)return;this.transition("finished","识别完成，请核对文字后发送");this.onResult(result);}catch(error){await this.cancel();this.transition("error");this.onError(error.message);}
    }
    async cancel(){const id=this.sessionId;this.generation++;this.sessionId=null;this.flushDone?.();await this.release();this.transition("cancelled","收音已取消，音频与转写已丢弃");if(id)try{await this.send(`/api/voice/sessions/${id}`,{},"DELETE");}catch(error){this.onError(`本地收音已停止，服务端取消未确认：${error.message}`);}}
    async correct(segment_id,text){if(!this.sessionId)throw new Error("语音会话已结束或取消");const result=await this.send(`/api/voice/sessions/${this.sessionId}/correct`,{segment_id,text});this.onResult(result);return result;}
  }
  root.StreamingVoiceController=StreamingVoiceController;
  if(typeof module!=="undefined"&&module.exports)module.exports={StreamingVoiceController};
  if(!root.document)return;
  root.addEventListener("DOMContentLoaded",()=>{
    const $=id=>document.getElementById(id),U=root.PatrolUI;let available=false;
    const controller=new StreamingVoiceController({onState:({state,message})=>{const active=["starting","listening","finishing"].includes(state);$("stream-start").disabled=active||!available;$("stream-finish").hidden=state!=="listening";$("stream-cancel").hidden=!active;$("voice-provider").disabled=active;$("stream-state").textContent=message;if(state==="cancelled")$("stream-segments").replaceChildren();},onError:message=>{$("stream-state").textContent=message;U.feedback(message,"error");},onResult:result=>{
      $("stream-state").textContent=result.partial?`正在识别：${result.partial}`:result.state==="finished"?"识别完成，请核对文字后发送":`已识别 ${result.segments.length} 个片段`;
      const box=$("stream-segments");box.hidden=!result.segments.length;box.replaceChildren();
      for(const segment of result.segments){const row=document.createElement("div");row.className="stream-segment";const label=document.createElement("span");label.textContent=segment.id;const input=document.createElement("input");input.value=segment.text;input.maxLength=500;input.setAttribute("aria-label",`纠正 ${segment.id}`);const save=document.createElement("button");save.className="button secondary compact";save.textContent="纠正片段";save.onclick=()=>controller.correct(segment.id,input.value).catch(error=>U.feedback(error.message,"error"));row.append(label,input,save);box.append(row);}
      if(result.state==="finished"){if(result.text.length>500){U.feedback("识别文字超过500字，请从片段中提取一条完整任务后再发送。","error");return;}$("command-input").value=result.text;$("command-input").dispatchEvent(new Event("input",{bubbles:true}));U.feedback(result.text?"流式转写已填入输入框，请预览或确认后发送。":"没有识别到完整文字，请重新收音。");}
    }});
    function providerChanged(){const local=$("voice-provider").value==="vosk";U.stopVoice();if(!local&&controller.sessionId)void controller.cancel();$("mic-button").hidden=local;document.querySelector(".voice-options").hidden=local;$("stream-start").hidden=!local;$("stream-state").textContent=local?(available?"本地识别就绪；点击按钮后才开启麦克风":"Vosk 模型尚未配置，可使用浏览器语音或上传 WAV"):"";}
    $("voice-provider").onchange=providerChanged;$("stream-start").onclick=()=>{U.stopVoice();root.speechSynthesis?.cancel();void controller.start();};$("stream-finish").onclick=()=>void controller.finish();$("stream-cancel").onclick=()=>void controller.cancel();
    request("/api/voice/capabilities",undefined,"GET").then(data=>{available=data.asr.some(item=>item.id==="vosk"&&item.available===true);$("stream-start").disabled=!available;providerChanged();}).catch(error=>{$("stream-state").textContent=error.message;});
    root.addEventListener("patrol:view",event=>{if(event.detail!=="mission"&&(controller.sessionId||controller.state==="starting"))void controller.cancel();});root.addEventListener("patrol:speaking",()=>{if(["starting","listening","finishing"].includes(controller.state))void controller.cancel();});document.addEventListener("visibilitychange",()=>{if(document.hidden&&(controller.sessionId||controller.state==="starting"))void controller.cancel();});root.addEventListener("beforeunload",()=>{if(controller.sessionId||controller.state==="starting")void controller.cancel();});
    root.PatrolStreamingVoice=controller;
  });
})(typeof globalThis!=="undefined"?globalThis:window);
