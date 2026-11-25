'use strict';
// Which exported TM models may be installed. Keep the rules identical to backend/src/tm_sources.py.
const fs=require('node:fs');const path=require('node:path');
const EXPORT_URL=/^https:\/\/teachablemachine\.withgoogle\.com\/models\/([A-Za-z0-9_-]{4,64})\/?$/;
const ROLES=['Front','Back'];

function exportId(value) {
  const match=typeof value==='string' && EXPORT_URL.exec(value);
  if(!match) throw Error('INVALID_MODEL_URL');
  return match[1];
}
function validateSources(value) {
  const ids=ROLES.map(role=>exportId(value?.[role]));
  if(ids[0]===ids[1]) throw Error('FRONT_BACK_MUST_DIFFER');
  return Object.fromEntries(ROLES.map((role,i)=>[role,`https://teachablemachine.withgoogle.com/models/${ids[i]}/`]));
}
// A saved choice wins over the tracked defaults. A broken saved file stops staging: never fall back silently.
function loadSources(root) {
  let file=path.join(root,'.runtime/tm-model-sources.json');
  if(!fs.existsSync(file)) file=path.join(root,'VisionValidation_Module/edge_service/tm-sources.default.json');
  const value=JSON.parse(fs.readFileSync(file,'utf8'));
  if(value.version!==1) throw Error('INVALID_MODEL_SOURCES');
  return validateSources(value);
}
// Teachable Machine answers with one redirect to its own storage, for the same model ID and file. Nothing else is followed.
async function fetchExportAsset(source,name,fetchImpl=fetch) {
  const id=exportId(source);
  if(!/^[\w.-]+$/.test(name) || /^\.+$/.test(name)) throw Error('Unsafe model shard name');
  const options=()=>({redirect:'manual',signal:AbortSignal.timeout(15000)});
  let response=await fetchImpl(`https://teachablemachine.withgoogle.com/models/${id}/${name}`,options());
  if(response.status>=300 && response.status<400) {
    const allowed=`https://storage.googleapis.com/tm-model/${id}/${name}`;
    if(response.headers.get('location')!==allowed) throw Error('UNSUPPORTED_REDIRECT');
    response=await fetchImpl(allowed,options());
  }
  if(response.status!==200) throw Error('Model download failed');
  return Buffer.from(await response.arrayBuffer());
}
module.exports={ROLES,exportId,validateSources,loadSources,fetchExportAsset};
