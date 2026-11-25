'use strict';
// Native inference only. This module has no HTTP, MQTT, database or event writer.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const tf = require('@tensorflow/tfjs');
const ort = require('onnxruntime-node');
const {createCanvas, loadImage} = require('@napi-rs/canvas');
const LABELS = {
  Front: ['Front_000','Front_010','Front_011','Front_100','Front_110','Front_111'],
  Back: ['Back_000','Back_100','Back_X01','Back_11X']
};
const sha = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
function checkLabels(role, labels) {
  if (!LABELS[role] || !Array.isArray(labels) || labels.length !== LABELS[role].length ||
      new Set(labels).size !== labels.length || labels.some(x=>!LABELS[role].includes(x)))
    throw Error('MODEL_LABEL_MISMATCH');
}
function enhance(canvas, boost) {
  if (!['off','auto','high'].includes(boost)) throw Error('INVALID_BOOST');
  if (boost==='off') return canvas;
  const ctx=canvas.getContext('2d'), pixels=ctx.getImageData(0,0,canvas.width,canvas.height), d=pixels.data;
  let l=0, count=0;
  for(let i=0;i<d.length;i+=16) {l+=.299*d[i]+.587*d[i+1]+.114*d[i+2]; count++;}
  const mean=count?l/count:128;
  if(boost==='auto' && mean>=115) return canvas;
  const gamma=mean<55?.42:mean<85?.55:boost==='high'?.45:.72;
  const lut=Uint8Array.from({length:256},(_,i)=>Math.min(255,Math.max(0,Math.round((i/255)**gamma*255))));
  for(let i=0;i<d.length;i+=4) {d[i]=lut[d[i]];d[i+1]=lut[d[i+1]];d[i+2]=lut[d[i+2]];}
  ctx.putImageData(pixels,0,0); return canvas;
}
async function decode(image) {
  if(typeof image!=='string' || image.length>8_000_000 || !/^data:image\/(png|jpeg);base64,/.test(image))
    throw Error('INVALID_IMAGE');
  const img=await loadImage(Buffer.from(image.split(',')[1],'base64'));
  if(!img.width || !img.height || img.width*img.height>16_000_000) throw Error('IMAGE_TOO_LARGE');
  return img;
}
function rgb(canvas) {
  const d=canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data;
  const result=new Uint8Array(canvas.width*canvas.height*3);
  for(let i=0,j=0;i<d.length;i+=4) {result[j++]=d[i];result[j++]=d[i+1];result[j++]=d[i+2];}
  return result;
}
function tmFrame(image, boost) {
  const c=createCanvas(224,224);
  c.getContext('2d').drawImage(image,0,0,image.width,image.height,0,0,224,224);
  return enhance(c,boost); // Full-frame stretch, RGB, no mirror or crop.
}
function poseFrame(image) {
  const scale=Math.min(320/image.width,320/image.height);
  const newW=Math.round(image.width*scale),newH=Math.round(image.height*scale);
  const padX=(320-newW)/2,padY=(320-newH)/2;
  const canvas=createCanvas(320,320),ctx=canvas.getContext('2d');
  ctx.fillStyle='#727272';ctx.fillRect(0,0,320,320);ctx.drawImage(image,padX,padY,newW,newH);
  const pixels=rgb(canvas), area=320*320, input=new Float32Array(3*area);
  for(let i=0;i<area;i++) for(let channel=0;channel<3;channel++) input[channel*area+i]=pixels[i*3+channel]/255;
  return {pixels,input,newW,newH,padX,padY};
}
function poseLandmarks(data,dims,frame) {
  if(dims.length!==3 || dims[0]!==1 || dims[1]!==56) throw Error('POSE_OUTPUT_MISMATCH');
  const anchors=dims[2], candidates=[];
  for(let a=0;a<anchors;a++) if(data[4*anchors+a]>.45)
    candidates.push([data[a],data[anchors+a],data[2*anchors+a],data[3*anchors+a],data[4*anchors+a],a]);
  candidates.sort((a,b)=>b[4]-a[4]);
  const kept=[];
  for(const c of candidates) {
    const [x,y,w,h]=c;
    const overlaps=kept.some(([ox,oy,ow,oh])=>{
      const i=Math.max(0,Math.min(x+w/2,ox+ow/2)-Math.max(x-w/2,ox-ow/2))*
        Math.max(0,Math.min(y+h/2,oy+oh/2)-Math.max(y-h/2,oy-oh/2));
      const u=w*h+ow*oh-i;return u>0 && i/u>=.50;
    });
    if(!overlaps) kept.push(c);
    if(kept.length>=5) break;
  }
  const mapping=[0,2,5,7,8,11,12,13,14,15,16,23,24,25,26,27,28];
  const clamp=x=>Math.max(0,Math.min(1,x));
  return kept.map(([cx,cy,,,score,a])=>{
    const landmarks=Array.from({length:33},()=>({x:0,y:0,visibility:0}));
    mapping.forEach((index,k)=>{landmarks[index]={
      x:clamp((data[(5+k*3)*anchors+a]-frame.padX)/frame.newW),
      y:clamp((data[(6+k*3)*anchors+a]-frame.padY)/frame.newH),visibility:data[(7+k*3)*anchors+a]};});
    return {landmarks,personBox:{x:clamp((cx-frame.padX)/frame.newW),y:clamp((cy-frame.padY)/frame.newH),visibility:score,name:'Body'}};
  });
}
function pair(front,back) {
  const top=(items,role)=>{
    checkLabels(role,items.map(x=>x.className));
    if(items.some(x=>!Number.isFinite(x.probability)||x.probability<0||x.probability>1)) throw Error('INVALID_PROBABILITY');
    return items.reduce((a,b)=>a.probability>b.probability?a:b);
  };
  const f=top(front,'Front'),b=top(back,'Back');
  const base={front_class:f.className,back_class:b.className,front_confidence:f.probability*100,back_confidence:b.probability*100,
    authorizes_write:false};
  if(f.probability<.8 || b.probability<.8) return {...base,hold:'LOW_CONFIDENCE'};
  const [f1,m1,m2]=({Front_000:[0,0,0],Front_010:[0,1,0],Front_011:[0,1,1],Front_100:[1,0,0],Front_110:[1,1,0],Front_111:[1,1,1]})[f.className];
  const [b1,b2,observed]=({Back_000:[0,0,0],Back_100:[1,0,0],Back_X01:[null,0,1],Back_11X:[1,1,null]})[b.className];
  const m2_check=observed===null?'SKIPPED_OCCLUDED':observed===m2?'PASS':'CONFLICT';
  if(m2_check==='CONFLICT') return {...base,m2_check,hold:'M2_CONFLICT'};
  const slots=[f1,m1,m2,b1,b2];
  if(b1===null) return {...base,m2_check,slots,count_range:[f1+m1+m2,f1+m1+m2+1],hold:'UNKNOWN_B1'};
  return {...base,m2_check,slots,total:slots.reduce((a,b)=>a+b,0),hold:null};
}
async function weightsHash(model) {
  const hash=crypto.createHash('sha256');
  for(const weight of model.getWeights()) {const data=await weight.data();hash.update(Buffer.from(data.buffer,data.byteOffset,data.byteLength));}
  return hash.digest('hex');
}
function verifiedFile(root,entry) {
  const target=path.resolve(root,entry.path), prefix=path.resolve(root)+path.sep;
  if(!target.startsWith(prefix) || !/^[a-f0-9]{64}$/.test(entry.sha256)) throw Error('INVALID_MODEL_MANIFEST');
  const bytes=fs.readFileSync(target);
  if(sha(bytes)!==entry.sha256) throw Error('MODEL_HASH_MISMATCH');
  return bytes;
}
class Inference {
  async init(configPath, kind='all') {
    if(!['all','pair','pose'].includes(kind)) throw Error('INVALID_WORKER_KIND');
    const root=path.dirname(path.resolve(configPath)), config=JSON.parse(fs.readFileSync(configPath,'utf8'));
    if(config.version!==1) throw Error('INVALID_MODEL_MANIFEST');
    this.models={};this.versions={};
    await tf.setBackend('cpu');await tf.ready();
    for(const role of kind==='pose'?[]:['Front','Back']) {
      const spec=config[role], metadata=JSON.parse(verifiedFile(root,spec.metadata)), json=JSON.parse(verifiedFile(root,spec.model));
      checkLabels(role,metadata.labels);
      if(metadata.imageSize!==224) throw Error('MODEL_IMAGE_SIZE_MISMATCH');
      const shards=json.weightsManifest.flatMap(group=>group.paths);
      if(new Set(shards).size!==shards.length || shards.length!==spec.weights.length) throw Error('MODEL_SHARDS_MISMATCH');
      const weights=Buffer.concat(shards.map(name=>{
        const entry=spec.weights.find(x=>x.name===name);if(!entry) throw Error('MODEL_SHARDS_MISMATCH');
        return verifiedFile(root,entry);
      }));
      const model=await tf.loadLayersModel(tf.io.fromMemory({modelTopology:json.modelTopology,
        weightSpecs:json.weightsManifest.flatMap(group=>group.weights),weightData:weights.buffer.slice(weights.byteOffset,weights.byteOffset+weights.byteLength)}));
      if(model.inputs.length!==1 || String(model.inputs[0].shape.slice(1))!=='224,224,3' ||
        model.outputs.length!==1 || model.outputs[0].shape.at(-1)!==metadata.labels.length) throw Error('MODEL_SHAPE_MISMATCH');
      this.models[role]={model,labels:metadata.labels};
      this.versions[role]={weights_sha256:await weightsHash(model),labels:metadata.labels,asset_sha256:spec.model.sha256};
    }
    if(kind!=='pair') {
      const poseBytes=verifiedFile(root,config.pose);
      this.pose=await ort.InferenceSession.create(poseBytes,{executionProviders:['cpu'],intraOpNumThreads:1,interOpNumThreads:1});
      this.versions.pose={asset_sha256:config.pose.sha256};
    }
    this.versions.runtime={tfjs:tf.version.tfjs,onnxruntime:'1.19.2',canvas:'0.1.80',preprocessing:'full-frame-v1'};
    return this.versions;
  }
  async tm(role,image,boost='off',includePixels=false) {
    const c=tmFrame(await decode(image),boost),pixels=rgb(c), {model,labels}=this.models[role]||{};
    if(!model) throw Error('MODEL_NOT_READY');
    const values=tf.tidy(()=>{
      // @teachablemachine/image capture() divides by 127, not 127.5.
      const tensor=tf.tensor3d(pixels,[224,224,3],'int32').toFloat().div(tf.scalar(127)).sub(tf.scalar(1)).expandDims();
      const output=model.predict(tensor);return Array.from(output.dataSync());
    });
    if(values.length!==labels.length || values.some(x=>!Number.isFinite(x))) throw Error('INVALID_MODEL_OUTPUT');
    return {predictions:labels.map((className,i)=>({className,probability:values[i]})),
      ...(includePixels?{rgb_base64:Buffer.from(pixels).toString('base64')}:{})};
  }
  async infer(job) {
    const start=performance.now(),cpu=process.cpuUsage();let result;
    if(job.kind==='pair') {
      const back=await this.tm('Back',job.image_back,job.boost,job.include_pixels);
      const front=await this.tm('Front',job.image_front,job.boost,job.include_pixels);
      result={front,back,candidate:pair(front.predictions,back.predictions)};
    } else if(job.kind==='pose') {
      const frame=poseFrame(await decode(job.image));
      const outputs=await this.pose.run({[this.pose.inputNames[0]]:new ort.Tensor('float32',frame.input,[1,3,320,320])});
      const output=outputs[this.pose.outputNames[0]];
      result={persons:poseLandmarks(output.data,output.dims,frame),
        ...(job.include_pixels?{rgb_base64:Buffer.from(frame.pixels).toString('base64'),output:Array.from(output.data),dims:output.dims}:{})};
    } else throw Error('INVALID_JOB_KIND');
    const usage=process.cpuUsage(cpu);
    return {...result,metrics:{latency_ms:performance.now()-start,cpu_ms:(usage.user+usage.system)/1000,rss_bytes:process.memoryUsage().rss,tf_tensors:tf.memory().numTensors},authorizes_write:false};
  }
}
module.exports={Inference,LABELS,checkLabels,pair,enhance,rgb,tmFrame,poseFrame,poseLandmarks,sha,verifiedFile};
