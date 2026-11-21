'use strict';
// Explicit local staging: never silently falls back to a different or combined model.
const fs=require('node:fs');const path=require('node:path');
const {checkLabels,sha}=require('./inference.cjs');
const destination=path.resolve(process.argv[2]||'.runtime/edge-models');
const sources={Front:'https://teachablemachine.withgoogle.com/models/0cQO4TjOm/',Back:'https://teachablemachine.withgoogle.com/models/40mk-G4Oq/'};
async function main() {
  fs.mkdirSync(destination,{recursive:true});const config={version:1};
  for(const role of ['Front','Back']) {
    const folder=path.join(destination,role);fs.mkdirSync(folder,{recursive:true});
    async function get(name) {
      name=name.replace(/^\.\//,'');
      if(!/^[\w.-]+$/.test(name)) throw Error('Unsafe model shard name');
      let bytes;
      if(process.argv.includes('--from-cache')) {
        const cache=sha(Buffer.from(sources[role]));
        bytes=fs.readFileSync(path.join(__dirname,'../models/tm_cache',cache,name));
      } else {
        const response=await fetch(sources[role]+name,{signal:AbortSignal.timeout(15000)});
        if(!response.ok) throw Error('Model download failed');
        bytes=Buffer.from(await response.arrayBuffer());
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
