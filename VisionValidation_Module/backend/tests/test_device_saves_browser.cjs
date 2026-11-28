const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
(async()=>{
for(const file of ['index.html','VisionValidation_Module/camera_manager.html']) {
  const html=fs.readFileSync(file,'utf8'),toasts=[],calls=[],ipElements={};
  // A fake esp32_cameras table: PATCH changes rows one at a time, like the real per-row requests.
  let table,failPatch=null,patches=[];
  const reset=(back='10.0.0.1',front='10.0.0.2')=>{table={1:back,2:front};failPatch=null;patches=[];calls.length=0;};
  const sbRest=async(name,method,query,body)=>{
    assert.equal(name,'esp32_cameras');
    if(method==='GET')return [1,2].filter(a=>table[a]!==undefined).map(a=>({shelf_id:'s',angle_index:a,ip_address:table[a]}));
    const angle=Number(/angle_index=eq\.(\d)/.exec(query)[1]);patches.push([angle,body.ip_address]);
    if(failPatch && failPatch(angle,body.ip_address,patches.length))throw Error('Supabase HTTP 403');
    table[angle]=body.ip_address;return [{shelf_id:'s',angle_index:angle,ip_address:body.ip_address}];
  };
  const ctx={refShelfSelect:{value:'s'},refEspIpInput:{value:'10.0.0.11'},refEspIp2Input:{value:'10.0.0.12'},
    cleanCameraHost:x=>x,saveCamIpsBtn:{},showToast:(...x)=>toasts.push(x),deviceSaveError:e=>e.message,
    sbRest,fetch:async()=>{calls.push('bridge');return{ok:true,status:200,json:async()=>({})};},getBridgeUrl:()=>'',
    document:{getElementById:id=>ipElements[id] ||= {}},markStatusError(el,on){el.red=!!on;},
    compartmentsData:[],esp32CameraNodesList:[],renderAllCamerasOverview(){},populateAssignedIpDropdowns(){},renderCompartments(){}};
  vm.createContext(ctx);
  vm.runInContext(html.slice(html.indexOf('    // Back (CAM1, angle 1) and Front (CAM2, angle 2) addresses are saved as one operation'),html.indexOf('    let bridgeStatus=')),ctx);
  const line=()=>ipElements.camIpSaveStatus;

  // Both saved, then the bridge; the result names each camera.
  reset();await ctx.saveAssignedShelfIps();
  assert.deepEqual(table,{1:'10.0.0.11',2:'10.0.0.12'});assert.equal(calls.length,1);
  assert.equal(line().textContent,'s: Back (CAM1) saved (10.0.0.1 → 10.0.0.11); Front (CAM2) saved (10.0.0.2 → 10.0.0.12). Bridge updated.');
  assert.equal(line().red,false);assert.equal(toasts.at(-1)[1],'success');assert.equal(ctx.saveCamIpsBtn.disabled,false);
  assert.equal(ctx.compartmentsData.length,0);

  // Front fails: Back is restored, so neither changes, and the bridge is not touched.
  reset();failPatch=(angle,ip)=>angle===2;await ctx.saveAssignedShelfIps();
  assert.deepEqual(table,{1:'10.0.0.1',2:'10.0.0.2'},'neither camera changes');
  assert.deepEqual(patches,[[1,'10.0.0.11'],[2,'10.0.0.12'],[1,'10.0.0.1']],'Back is written back to its original address');
  assert.equal(calls.length,0);
  assert.equal(line().textContent,'s: Back (CAM1) not saved (restored to 10.0.0.1); Front (CAM2) not saved: Supabase HTTP 403. Both cameras keep their previous addresses. Bridge unchanged.');
  assert.equal(line().red,true);assert.equal(toasts.at(-1)[1],'error');

  // Restore also fails: the report says plainly that the database is mismatched.
  reset();failPatch=(angle,ip,n)=>n>=2;await ctx.saveAssignedShelfIps();
  assert.deepEqual(table,{1:'10.0.0.11',2:'10.0.0.2'});assert.equal(calls.length,0);
  assert.match(line().textContent,/Back \(CAM1\) SAVED as 10\.0\.0\.11 but could not be restored to 10\.0\.0\.1; Front \(CAM2\) not saved: Supabase HTTP 403\. The shelf now has mismatched cameras/);

  // First write fails: nothing to undo.
  reset();failPatch=angle=>angle===1;await ctx.saveAssignedShelfIps();
  assert.deepEqual(table,{1:'10.0.0.1',2:'10.0.0.2'});assert.deepEqual(patches,[[1,'10.0.0.11']]);assert.match(line().textContent,/^s: Back \(CAM1\) not saved: Supabase HTTP 403; Front \(CAM2\) not saved\./);

  // An unchanged camera is not written.
  reset('10.0.0.11','10.0.0.2');await ctx.saveAssignedShelfIps();
  assert.deepEqual(patches,[[2,'10.0.0.12']]);assert.match(line().textContent,/Back \(CAM1\) unchanged \(10\.0\.0\.11\); Front \(CAM2\) saved/);

  // Database saved but the bridge refused: reported per part, shown as a problem.
  reset();ctx.fetch=async()=>{calls.push('bridge');return {ok:false,status:400,json:async()=>({error:'Shelf and both camera addresses must be configured'})};};
  await ctx.saveAssignedShelfIps();assert.deepEqual(table,{1:'10.0.0.11',2:'10.0.0.12'});
  assert.match(line().textContent,/Front \(CAM2\) saved .*\. Bridge not updated \(Shelf and both camera addresses must be configured\); it picks up the saved addresses within 5 minutes/);
  assert.equal(line().red,true);
  ctx.fetch=async()=>{calls.push('bridge');return{ok:true,status:200,json:async()=>({})};};

  // Refused before any write: same address twice, a missing address, or an incomplete registration.
  reset();ctx.refEspIp2Input.value='10.0.0.11';await ctx.saveAssignedShelfIps();assert.deepEqual(patches,[]);assert.match(line().textContent,/need different IP addresses/);
  ctx.refEspIp2Input.value='';await ctx.saveAssignedShelfIps();assert.deepEqual(patches,[]);assert.equal(line().red,true);
  ctx.refEspIp2Input.value='10.0.0.12';reset();delete table[2];await ctx.saveAssignedShelfIps();
  assert.deepEqual(patches,[]);assert.equal(calls.length,0);assert.match(line().textContent,/^Nothing was saved\. s needs exactly one registered Back \(angle 1\) and one Front \(angle 2\) camera/);
  ctx.sbRest=async()=>{throw Error('database down');};await ctx.saveAssignedShelfIps();assert.match(line().textContent,/^Nothing was saved\. database down/);
  ctx.sbRest=sbRest;
  vm.runInContext(html.slice(html.indexOf('    async function getCameras('),html.indexOf('    async function upsertCamera')),ctx);
  ctx.CAMERA_COLS='id';ctx.sbRest=async()=>[];assert.equal((await ctx.getCameras()).length,0);
  ctx.sbRest=async()=>{throw Error('database down');};await assert.rejects(ctx.getCameras(),/database down/);
  const elements={cctvIdInput:{value:'CCTV_01'},cctvNameInput:{value:'Camera'},cctvIpInput:{value:'host'},cctvRtspInput:{value:'source'},cctvActiveCheck:{checked:true},cctvRoleSelect:{value:'GRID_6_ROI'},cctvEditSaveBtn:{}};
  Object.assign(ctx,{document:{getElementById:id=>elements[id]},validateCctvBinding(){},navigator:{onLine:true},cctvEditingId:'CCTV_01',cctvEditSaving:false,cctvCamerasList:[{cctv_id:'CCTV_01'}],cameraRole:c=>c.camera_role,renderCctvCameras(){}});
  vm.runInContext(html.slice(html.indexOf('    async function saveCctvCamera('),html.indexOf('    function renderCctvCameras()')),ctx);
  await ctx.saveCctvCamera();assert.equal(ctx.cctvCamerasList[0].cctv_id,'CCTV_01');assert.match(toasts.at(-1)[0],/not saved/);
  ctx.sbRest=async()=>[];await ctx.loadCctvCameras();assert.equal(ctx.cctvCamerasList.length,0);
  ctx.cctvCamerasList=[{cctv_id:'stale'}];ctx.sbRest=async()=>{throw Error('Supabase HTTP 403');};await ctx.loadCctvCameras();assert.equal(ctx.cctvCamerasList.length,0);assert.match(toasts.at(-1)[0],/403/);
}
console.log('PASS: Back+Front IP saves are all-or-nothing with rollback and per-camera results; failed CCTV saves remain unsaved; empty/error reads remove stale camera rows in both dashboards');
})().catch(e=>{console.error(e);process.exitCode=1;});
