/* Fixed 16 kHz mono PCM16LE chunks; accumulation survives render boundaries. */
class PatrolPCM extends AudioWorkletProcessor {
  constructor() {
    super(); this.ratio=sampleRate/16000; this.weight=0; this.sum=0; this.samples=[];
    this.port.onmessage=event=>{if(event.data?.type==="flush"){this.flush();this.port.postMessage({type:"flushed"});}};
  }
  flush(){if(!this.samples.length)return;const buffer=new ArrayBuffer(this.samples.length*2),view=new DataView(buffer);this.samples.forEach((sample,i)=>view.setInt16(i*2,sample,true));this.samples=[];this.port.postMessage({type:"pcm",buffer},[buffer]);}
  process(inputs){const input=inputs[0]?.[0];if(!input)return true;for(const sample of input){let remaining=1;while(remaining>1e-9){const amount=Math.min(remaining,this.ratio-this.weight);this.sum+=sample*amount;this.weight+=amount;remaining-=amount;if(this.weight>=this.ratio-1e-9){const value=Math.max(-1,Math.min(1,this.sum/this.ratio));this.samples.push(Math.round(value*(value<0?32768:32767)));this.weight=0;this.sum=0;if(this.samples.length>=3200)this.flush();}}}return true;}
}
registerProcessor("patrol-pcm",PatrolPCM);
