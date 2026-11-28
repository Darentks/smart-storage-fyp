// "All 6 cameras" overview (formerly the ESP32 CAMs page) at the bottom of Panel 2 on the Inventory Dashboard.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
(async()=>{
for(const file of ['index.html','VisionValidation_Module/camera_manager.html']){
  const html=fs.readFileSync(file,'utf8');
  // Placement: a native <details>, closed by default, last thing inside Panel 2's section, on the dashboard tab.
  const panel2=html.indexOf('Front/Back Camera Diagnostics');assert.ok(panel2>0);
  const panelEnd=html.indexOf('\n    </section>',panel2);
  const details=html.indexOf('<details class="esp32-overview" id="all-cameras"');
  assert.ok(details>panel2 && details<panelEnd,`${file}: overview must sit inside Panel 2`);
  assert.match(html.slice(details,html.indexOf('>',details)+1),/^<details class="esp32-overview" id="all-cameras" ontoggle="onEsp32OverviewToggle\(this\)">$/,'closed by default');
  assert.match(html.slice(html.lastIndexOf('</details>',panelEnd),panelEnd),/^<\/details>\s*$/,`${file}: overview must be at the bottom of Panel 2`);
  const viewStart=html.lastIndexOf('data-dashboard-view=',details);
  assert.equal(html.slice(viewStart,viewStart+31),'data-dashboard-view="dashboard"',`${file}: Panel 2 is on the Inventory Dashboard tab`);
  for(const id of ['probeAllNodesBtn','ignoredCameraRows','esp32CameraManagerGrid'])assert.ok(html.indexOf(`id="${id}"`)>details && html.indexOf(`id="${id}"`)<panelEnd,id);
  assert.ok(/<dialog id="esp32PictureDialog"/.test(html)&&/id="esp32PictureFull"/.test(html),'full-size picture dialog');
  assert.match(html,/esp32IpSaveInProgress = true; renderAllCamerasOverview\(\);/,'Save Assigned IPs marks the reassignment in progress');
  // The page must load a script that exists; a missing overview script once blanked Panel 1's counts.
  const scripts=[...html.matchAll(/<script src="\/([^"]+\.js)"><\/script>/g)].map(m=>m[1]);
  assert.ok(scripts.includes('esp32_camera_manager.js'),`${file}: overview script tag`);
  for(const name of scripts)assert.ok(fs.existsSync(name),`${file}: /${name} must exist next to index.html`);
  // Inventory code reaches the overview only through the guard, which survives a missing or failing renderer.
  assert.deepStrictEqual([...html.matchAll(/(?<![\w.])renderEsp32CameraManager\(\)/g)].length,1,`${file}: only the guard calls the renderer directly`);
  const guardStart=html.indexOf('    function renderAllCamerasOverview() {'),guardEnd=html.indexOf('\n    }',guardStart)+6;
  const guard=html.slice(guardStart,guardEnd);assert.ok(guardStart>0&&guard.trimEnd().endsWith('}'),'guard function found');
  const missing={console:{warn(){}}};vm.createContext(missing);vm.runInContext(guard,missing);
  assert.doesNotThrow(()=>missing.renderAllCamerasOverview(),'missing overview script must not throw');
  const failing={console:{warn(){}},renderEsp32CameraManager(){throw Error('broken overview');}};vm.createContext(failing);vm.runInContext(guard,failing);
  assert.doesNotThrow(()=>failing.renderAllCamerasOverview(),'failing overview must not throw into inventory polling');
}

// Behaviour of esp32_camera_manager.js with all I/O mocked.
const el=()=>({textContent:'',innerHTML:'',disabled:false,title:''});
const nodes={},overview={open:false},dialog={open:false,showModal(){this.open=true;}},full={src:'',alt:''};
let fetches=[],probes=0,holdProbes=false;const waiting=[];
const ctx={ALL_SHELF_IDS:['SHELF_R1_C1','SHELF_R1_C2','SHELF_R1_C3'],esp32CameraNodesList:[],AbortSignal,URL,
  document:{getElementById:id=>id==='all-cameras'?overview:id==='esp32PictureDialog'?dialog:id==='esp32PictureFull'?full:(nodes[id]??=el())},
  cleanCameraHost:ip=>String(ip||'').split('/')[0],getBridgeUrl:()=>'http://bridge',setTimeout:fn=>fn(),
  fetch:async url=>{fetches.push(url);const shelf=new URL(url).searchParams.get('shelf_id');
    return {ok:true,json:async()=>({observation:{shelf_id:shelf,timestamp:1000,image_back:'data:image/jpeg;base64,BACK',image_front:'data:image/jpeg;base64,FRONT'}})};},
  probeCameraStatus:async ip=>{probes++;if(holdProbes)await new Promise(r=>waiting.push(r));return {device_id:'wrong',shelf_id:'wrong',ip_address:ip,camera_role:'wrong',rssi_dbm:-50};}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync('esp32_camera_manager.js','utf8'),ctx);
const grid=()=>nodes.esp32CameraManagerGrid.innerHTML;
const settle=()=>new Promise(r=>setImmediate(r));

// Opened before registrations arrive: nothing to load yet, and nothing loads while closed.
overview.open=true;ctx.onEsp32OverviewToggle(overview);await settle();assert.equal(fetches.length,0);
overview.open=false;
ctx.esp32CameraNodesList=JSON.parse(vm.runInContext('JSON.stringify(FIXED_CAMERA_SLOTS)',ctx));
for(let i=0;i<5;i++)ctx.renderEsp32CameraManager();await settle();
assert.equal(fetches.length,0,'no cached pictures load while the section is closed');
// First opening loads every registered camera once (one cache read per shelf serves Back and Front).
overview.open=true;ctx.onEsp32OverviewToggle(overview);await settle();await settle();
assert.equal(fetches.length,3);assert.ok(fetches.every(u=>/\/native_observation\?shelf_id=SHELF_R1_C[1-3]$/.test(u)));
assert.equal((grid().match(/<img /g)||[]).length,6);
assert.equal((grid().match(/class="camera-shelf-group"/g)||[]).length,3);
assert.equal((grid().match(/>Not checked</g)||[]).length,6,'registration and cached pictures do not establish connectivity');
assert.equal((grid().match(/<button type="button" class="esp32-thumb" onclick="openEsp32Picture\('ESP32_R1_C\d_CAM\d'\)"/g)||[]).length,6,'each 110px thumbnail opens full size');
overview.open=false;ctx.onEsp32OverviewToggle(overview);overview.open=true;ctx.onEsp32OverviewToggle(overview);ctx.renderEsp32CameraManager();await settle();
assert.equal(fetches.length,3,'reopening does not reload pictures');
// Same behaviour when registrations arrive after the section was opened.
vm.runInContext('esp32OverviewLoaded=false;esp32CachedPictures.clear();',ctx);fetches=[];
const saved=ctx.esp32CameraNodesList;ctx.esp32CameraNodesList=[];ctx.renderEsp32CameraManager();await settle();assert.equal(fetches.length,0);
ctx.esp32CameraNodesList=saved;ctx.renderEsp32CameraManager();await settle();await settle();assert.equal(fetches.length,3,'late registrations load once the open section can');

// Full-size picture.
ctx.openEsp32Picture('ESP32_R1_C2_CAM2');
assert.equal(dialog.open,true);assert.equal(full.src,'data:image/jpeg;base64,FRONT');
assert.match(nodes.esp32PictureCaption.textContent,/^CAM4 · SHELF_R1_C2 Front/);

// Probe all 6: button-only, blocked during Save Assigned IPs, never overwrites shared rows.
assert.equal(probes,0,'opening the section never probes');
const before=JSON.stringify(ctx.esp32CameraNodesList);
ctx.esp32IpSaveInProgress=true;ctx.renderEsp32CameraManager();
assert.equal(nodes.probeAllNodesBtn.disabled,true);assert.match(nodes.probeAllNodesBtn.title,/Save Assigned IPs/);
await ctx.probeAllEsp32CameraNodes();assert.equal(probes,0,'no probe while a reassignment is saving');
ctx.esp32IpSaveInProgress=false;ctx.renderEsp32CameraManager();assert.equal(nodes.probeAllNodesBtn.disabled,false);
holdProbes=true;const running=ctx.probeAllEsp32CameraNodes();await settle();
assert.equal(nodes.probeAllNodesBtn.disabled,true);assert.equal(nodes.probeAllNodesBtn.textContent,'Probing…');
await ctx.probeAllEsp32CameraNodes();assert.equal(probes,6,'a second click while probing is ignored');
holdProbes=false;waiting.splice(0).forEach(r=>r());
for(let i=0;i<6;i++)await settle();await running;
assert.equal(JSON.stringify(ctx.esp32CameraNodesList),before,'probe results stay out of the shared rows used by Panel 2');
assert.equal((grid().match(/>Identity mismatch</g)||[]).length,6);assert.match(grid(),/-50 dBm/);
assert.equal(nodes.probeAllNodesBtn.disabled,false);
console.log('PASS All 6 cameras overview: bottom of Panel 2 on the dashboard tab, closed by default, pictures load on first open only, full-size thumbnails, probe blocked during IP save and kept out of shared rows');
})().catch(error=>{console.error(error);process.exitCode=1;});
