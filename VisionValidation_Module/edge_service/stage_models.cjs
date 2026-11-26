'use strict';
// Explicit local staging: never silently falls back to a different or combined model.
// Model links come only from the settings file (default deploy/tm-model-sources.json, or --sources <file>).
// On failure the last stdout line is JSON {ok:false, role, stage, reason} so the caller can say what failed.
const fs=require('node:fs');const path=require('node:path');
const {checkLabels,sha}=require('./inference.cjs');
const SOURCE_RULE=/^https:\/\/teachablemachine\.withgoogle\.com\/models\/[A-Za-z0-9_-]+\/$/;
const ATTEMPTS=3;

class StageError extends Error {
  constructor(stage,reason,role){super(reason);this.stage=stage;this.role=role;}
}

function argument(name){const i=process.argv.indexOf(name);return i>0?process.argv[i+1]:undefined;}

function loadSources(file) {
  let data;
  try{data=JSON.parse(fs.readFileSync(file,'utf8'));}
  catch(_){throw new StageError('settings',`Cannot read the model settings file ${path.basename(file)}`);}
  if(!data || data.version!==1)throw new StageError('settings','Model settings file must have "version": 1');
  for(const role of ['Front','Back'])
    if(typeof data[role]!=='string' || !SOURCE_RULE.test(data[role]))
      throw new StageError('settings',`Please input the correct Teachable Machine link for the ${role} model (https://teachablemachine.withgoogle.com/models/<id>/)`,role);
  if(data.Front===data.Back)throw new StageError('settings','Front and Back links must be different models');
  return {Front:data.Front,Back:data.Back};
}

async function download(url,role,name) {
  // Bounded retries for network errors and server errors; a missing file (4xx) is not retried.
  let last='';
  for(let attempt=1;attempt<=ATTEMPTS;attempt++) {
    try{
      const response=await fetch(url,{signal:AbortSignal.timeout(15000)});
      if(response.ok)return Buffer.from(await response.arrayBuffer());
      last=`HTTP ${response.status} for ${name}`;
      if(response.status<500)break;
    }catch(error){last=(error && error.name==='TimeoutError')?`timed out after 15 s on ${name}`:`network error on ${name}`;}
    if(attempt<ATTEMPTS)await new Promise(resolve=>setTimeout(resolve,1000*attempt));
  }
  throw new StageError('download',`${last} (${ATTEMPTS} attempts at most)`,role);
}

async function main() {
  const positional=process.argv.slice(2).filter((value,i,all)=>!value.startsWith('--') && all[i-1]!=='--sources');
  const destination=path.resolve(positional[0]||'.runtime/edge-models');
  const sources=loadSources(path.resolve(argument('--sources')||path.join(__dirname,'../../deploy/tm-model-sources.json')));
  const localRoot=path.resolve(__dirname,'../../.runtime/edge-models');
  const fromCache=process.argv.includes('--from-cache');
  const localConfig=fromCache?JSON.parse(fs.readFileSync(path.join(localRoot,'manifest.json'),'utf8')):null;
  fs.mkdirSync(destination,{recursive:true});const config={version:1};
  for(const role of ['Front','Back']) {
    const folder=path.join(destination,role);fs.mkdirSync(folder,{recursive:true});
    async function get(name) {
      name=name.replace(/^\.\//,'');
      if(!/^[\w.-]+$/.test(name)) throw new StageError('check','Unsafe model file name',role);
      let bytes;
      if(fromCache) {
        // Backward-compatible flag: reuse the single staged bundle, not a duplicate proxy cache.
        const spec=localConfig[role];
        const entry=name==='model.json'?spec.model:name==='metadata.json'?spec.metadata:spec.weights.find(x=>x.name.replace(/^\.\//,'')===name);
        if(!entry || spec.source!==sources[role])throw new StageError('check','Local model source differs from the settings file',role);
        const file=path.resolve(localRoot,entry.path);
        if(!file.startsWith(localRoot+path.sep))throw new StageError('check','Unsafe local model path',role);
        bytes=fs.readFileSync(file);
        if(sha(bytes)!==entry.sha256)throw new StageError('check','Local model hash mismatch',role);
      } else bytes=await download(sources[role]+name,role,name);
      if(bytes.length>50_000_000)throw new StageError('check',`${name} is larger than 50 MB`,role);
      fs.writeFileSync(path.join(folder,name),bytes);
      return {path:`${role}/${name}`,sha256:sha(bytes)};
    }
    const metadata=await get('metadata.json');
    let labels;
    try{labels=JSON.parse(fs.readFileSync(path.join(destination,metadata.path))).labels;checkLabels(role,labels);}
    catch(_){throw new StageError('check',`Class names do not match the ${role} model (expected ${role}_... labels)`,role);}
    const model=await get('model.json');let json;
    try{json=JSON.parse(fs.readFileSync(path.join(destination,model.path)));}
    catch(_){throw new StageError('check','model.json is not valid JSON',role);}
    const weights=[];
    for(const name of json.weightsManifest.flatMap(group=>group.paths)) weights.push({name,...await get(name)});
    config[role]={source:sources[role],model,metadata,weights};
  }
  const pose=fs.readFileSync(path.join(__dirname,'../../models/yolo11n-pose.onnx'));
  fs.writeFileSync(path.join(destination,'yolo11n-pose.onnx'),pose);
  config.pose={path:'yolo11n-pose.onnx',sha256:sha(pose)};
  const temp=path.join(destination,'manifest.json.tmp');fs.writeFileSync(temp,JSON.stringify(config,null,2));
  fs.renameSync(temp,path.join(destination,'manifest.json'));
  console.log(JSON.stringify({ok:true,sources}));
}
main().catch(error=>{
  const known=error instanceof StageError;
  console.log(JSON.stringify({ok:false,role:known?error.role||null:null,stage:known?error.stage:'download',
    reason:known?error.message:'Model staging failed unexpectedly'}));
  process.exitCode=1;
});
