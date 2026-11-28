const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
(async()=>{
const html=fs.readFileSync('index.html','utf8');
assert.equal(html,fs.readFileSync('VisionValidation_Module/camera_manager.html','utf8'));
assert.doesNotMatch(html,/<summary>Advanced testing|id="comparisonLogsList"|ota-repo-path-input|Edit\/Reassign NVS|Delete Node|Add \/ Provision|Build &amp; Flash All|1-Click Build/);
for(const retained of ['cctvCameraManagerPanel','exportEdgeCalibrations()','installEdgeCalibrations()','importLocalRois()'])assert.ok(html.includes(retained),retained);
for(const script of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g))new vm.Script(script[1]);
let now=0;const timers=[];
async function advance(ms){now+=ms;for(const timer of timers.filter(t=>!t.done&&t.at<=now)){timer.done=true;timer.fn();}await new Promise(resolve=>setImmediate(resolve));}
const nodes={},element=()=>({textContent:'',innerHTML:'',disabled:false,insertAdjacentText(){},replaceChildren(){},append(){}});
const context={ALL_SHELF_IDS:['SHELF_R1_C1','SHELF_R1_C2','SHELF_R1_C3'],esp32CameraNodesList:[],
 location:{hostname:'example.vercel.app',pathname:'/',origin:'https://example.vercel.app'},
 document:{getElementById:id=>nodes[id]??=element()},cleanCameraHost:ip=>String(ip||'').split('/')[0],
 setTimeout:(fn,delay)=>timers.push({fn,at:now+delay,delay}),getBridgeUrl:()=>'http://bridge-fixture',AbortSignal,DASHBOARD_PAGE:'dashboard',captureLiveSnapshotUrl:()=>{throw Error('Cards must never capture directly');},setInterval(){},clearInterval(){},showToast(){},
 upsertCamera(){throw Error('Probe cannot mutate registrations');},fetch(){throw Error('Remote page cannot call wired routes');}};
vm.createContext(context);vm.runInContext(fs.readFileSync('esp32_camera_manager.js','utf8'),context);
assert.equal((nodes.esp32CameraManagerGrid.innerHTML.match(/<article /g)||[]).length,6);
assert.doesNotMatch(html,/wiredReplacementButton|replacementDialog|openReplacement\(/);
assert.equal(typeof context.replacementFetch,'undefined');
assert.doesNotMatch(html,/uploadNodeOtaBin|uploadCameraFirmware|fileToBase64DataUrl/);
assert.doesNotMatch(nodes.esp32CameraManagerGrid.innerHTML,/Wireless OTA|type="file"|Choose File/);
assert.match(nodes.esp32CameraManagerGrid.innerHTML,/Reload picture/);
assert.doesNotMatch(nodes.esp32CameraManagerGrid.innerHTML,/Baseline\/Live|Test Trigger/);
context.esp32CameraNodesList=JSON.parse(vm.runInContext('JSON.stringify(FIXED_CAMERA_SLOTS)',context));
let matched=context.fixedCameraRegistrations(context.esp32CameraNodesList);
assert.ok(matched.every(r=>r.status==='Registered'));
context.esp32CameraNodesList.push({...context.esp32CameraNodesList[0]});
assert.equal(context.fixedCameraRegistrations(context.esp32CameraNodesList)[0].status,'Configuration error');
context.esp32CameraNodesList.pop();const before=JSON.stringify(context.esp32CameraNodesList.map(r=>[r.device_id,r.shelf_id,r.angle_index,r.ip_address]));
context.probeCameraStatus=async()=>({device_id:'wrong',shelf_id:'wrong',ip_address:'192.168.0.250',camera_role:'wrong'});
await context.probeAllEsp32CameraNodes();
assert.equal(JSON.stringify(context.esp32CameraNodesList.map(r=>[r.device_id,r.shelf_id,r.angle_index,r.ip_address])),before);
assert.ok(context.esp32CameraNodesList.every(r=>r.status===undefined&&r.probe_issue===undefined),'probe results must not overwrite shared camera rows');
assert.equal((nodes.esp32CameraManagerGrid.innerHTML.match(/>Identity mismatch</g)||[]).length,6);
let cacheReads=0,picture='AAAA';
context.fetch=async url=>{cacheReads++;assert.match(url,/\/native_observation\?shelf_id=/);const shelf=new URL(url).searchParams.get('shelf_id');return {ok:true,json:async()=>({observation:{shelf_id:shelf,timestamp:1000,image_back:'data:image/jpeg;base64,'+picture,image_front:'data:image/jpeg;base64,'+picture}})};};
context.DASHBOARD_PAGE='esp32';context.renderEsp32CameraManager();
for(let i=0;i<20;i++)context.renderEsp32CameraManager();
assert.equal(cacheReads,0,'page load and status updates must not request any pictures');
assert.equal((nodes.esp32CameraManagerGrid.innerHTML.match(/<img /g)||[]).length,0);
const initialClicks=context.esp32CameraNodesList.map(slot=>context.refreshCachedCameraPicture(slot.device_id));
assert.equal(cacheReads,3,'simultaneous clicks can share a cached pair request');
await new Promise(resolve=>setImmediate(resolve));
assert.match(nodes.esp32CameraManagerGrid.innerHTML,/aria-busy="true"/);
await advance(999);
assert.equal((nodes.esp32CameraManagerGrid.innerHTML.match(/<img /g)||[]).length,0,'fast responses stay hidden for the full loading interval');
await advance(1);await Promise.all(initialClicks);
assert.equal((nodes.esp32CameraManagerGrid.innerHTML.match(/<img /g)||[]).length,6);
const first=nodes.esp32CameraManagerGrid.innerHTML;
picture='BBBB';for(let i=0;i<20;i++)context.renderEsp32CameraManager();
assert.equal(cacheReads,3);assert.equal(nodes.esp32CameraManagerGrid.innerHTML,first);
const refresh=context.refreshCachedCameraPicture('ESP32_R1_C1_CAM1');
await context.refreshCachedCameraPicture('ESP32_R1_C1_CAM1'); // double click must not duplicate a pending request
assert.equal(cacheReads,4);
await advance(999);
assert.equal((nodes.esp32CameraManagerGrid.innerHTML.match(/<img /g)||[]).length,5,'the selected card shows loading instead of its previous picture');
await advance(1);await refresh;
assert.equal(vm.runInContext("esp32CachedPictures.get('ESP32_R1_C1_CAM1').image",context),'data:image/jpeg;base64,BBBB');
assert.equal(vm.runInContext("esp32CachedPictures.get('ESP32_R1_C1_CAM2').image",context),'data:image/jpeg;base64,AAAA');
// A slow request stays in loading after one second; the timer is a minimum, not a network timeout.
let respond;context.fetch=()=>{cacheReads++;return new Promise(resolve=>respond=resolve);};
const slow=context.refreshCachedCameraPicture('ESP32_R1_C1_CAM1');await advance(1000);
assert.match(nodes.esp32CameraManagerGrid.innerHTML,/aria-busy="true"/);
respond({ok:true,json:async()=>({observation:{shelf_id:'SHELF_R1_C1',timestamp:2000,image_back:'data:image/jpeg;base64,CCCC'}})});
await slow;assert.equal(vm.runInContext("esp32CachedPictures.get('ESP32_R1_C1_CAM1').image",context),'data:image/jpeg;base64,CCCC');
context.esp32CameraNodesList[2].ip_address='192.168.0.250';context.renderEsp32CameraManager();
assert.equal(vm.runInContext("esp32CachedPictures.has('ESP32_R1_C2_CAM1')",context),false);
context.fetch=async()=>{cacheReads++;return {ok:true,json:async()=>({observation:null})};};
const missing=context.refreshCachedCameraPicture('ESP32_R1_C1_CAM1');await advance(1000);await missing;
assert.match(nodes.esp32CameraManagerGrid.innerHTML,/No cached picture yet/);
const readsAfterMissing=cacheReads;for(let i=0;i<20;i++)context.renderEsp32CameraManager();
assert.equal(cacheReads,readsAfterMissing,'missing cache does not trigger automatic retries');
assert.ok(timers.every(t=>t.delay===1000),'each requested picture uses the requested one-second minimum');
console.log('PASS fixed-camera cards: click-only pictures, no background image requests, one-second loading, slow/missing cache, duplicate clicks, pinned images and parity');
})().catch(error=>{console.error(error);process.exitCode=1;});
