'use strict';
// Explicit local staging: never silently falls back to a different or combined model.
const fs=require('node:fs');const path=require('node:path');
const {checkLabels,sha}=require('./inference.cjs');
const {loadSources,fetchExportAsset}=require('./tm_sources.cjs');
const destination=path.resolve(process.argv[2]||'.runtime/edge-models');
async function main() {
  // One snapshot for both roles; each manifest entry records the source it was built from.
  const sources=loadSources(path.resolve(__dirname,'../..'));
  const localRoot=path.resolve(__dirname,'../../.runtime/edge-models');
  const localConfig=process.argv.includes('--from-cache')?JSON.parse(fs.readFileSync(path.join(localRoot,'manifest.json'),'utf8')):null;
  fs.mkdirSync(destination,{recursive:true});const config={version:1};
  for(const role of ['Front','Back']) {
    const folder=path.join(destination,role);fs.mkdirSync(folder,{recursive:true});
    async function get(name) {
      name=name.replace(/^\.\//,'');
      if(!/^[\w.-]+$/.test(name)) throw Error('Unsafe model shard name');
      let bytes;
      if(process.argv.includes('--from-cache')) {
        // Backward-compatible flag: reuse the single staged bundle, not a duplicate proxy cache.
        const spec=localConfig[role];
        const entry=name==='model.json'?spec.model:name==='metadata.json'?spec.metadata:spec.weights.find(x=>x.name.replace(/^\.\//,'')===name);
        if(!entry || spec.source!==sources[role])throw Error('Local model source mismatch');
        const file=path.resolve(localRoot,entry.path);
        if(!file.startsWith(localRoot+path.sep))throw Error('Unsafe local model path');
        bytes=fs.readFileSync(file);
        if(sha(bytes)!==entry.sha256)throw Error('Local model hash mismatch');
      } else {
        bytes=await fetchExportAsset(sources[role],name);
      }
      if(bytes.length>50_000_000)throw Error('Model asset too large');
      fs.writeFileSync(path.join(folder,name),bytes);
      return {path:`${role}/${name}`,sha256:sha(bytes)};
    }
    const metadata=await get('metadata.json');checkLabels(role,JSON.parse(fs.readFileSync(path.join(destination,metadata.path))).labels);
    const model=await get('model.json'),json=JSON.parse(fs.readFileSync(path.join(destination,model.path)));
    const weights=[];
    for(const name of json.weightsManifest.flatMap(group=>group.paths)) weights.push({name,...await get(name)});
    config[role]={source:sources[role],model,metadata,weights};
  }
  const pose=fs.readFileSync(path.join(__dirname,'../../models/yolo11n-pose.onnx'));
  fs.writeFileSync(path.join(destination,'yolo11n-pose.onnx'),pose);
  config.pose={path:'yolo11n-pose.onnx',sha256:sha(pose)};
  const temp=path.join(destination,'manifest.json.tmp');fs.writeFileSync(temp,JSON.stringify(config,null,2));
  fs.renameSync(temp,path.join(destination,'manifest.json'));
  console.log('Staged independent model assets; cutover remains disabled.');
}
main().catch(()=>{console.error('Model staging failed; no new manifest accepted.');process.exitCode=1;});
