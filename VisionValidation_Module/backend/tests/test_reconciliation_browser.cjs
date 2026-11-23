// Inventory recovery no longer uses Auth or cloud recount; preserve this filename as a cutover regression.
const fs=require('node:fs'),assert=require('node:assert/strict'),vm=require('node:vm');
for(const file of ['index.html','VisionValidation_Module/camera_manager.html']) {
 const html=fs.readFileSync(file,'utf8');
 for(const old of ['async function operatorSession(', 'async function reconcileShelf(', '/reconciliation_context', '/reconcile_inventory', 'auth.signInWithPassword', 'auth.getSession']) assert.ok(!html.includes(old),old);
 const context={};vm.createContext(context);
 vm.runInContext(html.slice(html.indexOf('    function runtimeInventoryView('),html.indexOf('    function renderCompartments(')),context);
 const exact={state:'Ready',authoritative:true,precision:'EXACT',current_quantity:4,baseline:{slots:[0,1,1,1,1]},latest_observation:{slots:[0,1,1,1,1]}};
 assert.equal(context.runtimeInventoryView(exact).label,'Exact 4');
 const range={...exact,precision:'RANGE',current_quantity:null,count_range:[2,3],baseline:{slots:[0,1,1,null,0]}};
 const view=context.runtimeInventoryView(range);assert.equal(view.label,'Estimated 2\u20133');assert.equal(view.state,'Ready Limited');assert.match(view.details,/B1=unknown/);
 const held={...range,state:'Held',authoritative:false,latest_observation:{slots:[0,1,1,null,0],hold_code:'VISIBLE_CHANGE_NET_UNPROVEN'}};
 assert.equal(context.runtimeInventoryView(held).label,'Unverified');assert.match(context.runtimeInventoryView(held).details,/VISIBLE_CHANGE_NET_UNPROVEN/);
 assert.equal(context.runtimeInventoryView(exact,{state:'Offline'}).label,'Unverified');
 assert.match(html,/window.inventoryMode = 'runtime'/);
 assert.ok(!html.includes('Physical recount (operator)'));
 assert.ok(!html.includes('supabase.createClient'));
 assert.ok(!html.includes('supabaseClient'));
 console.log('PASS: '+file+' read-only recovery, exact/range/unknown/held rendering and no Auth inventory dependency');
}

(async()=>{
 for(const file of ['index.html','VisionValidation_Module/camera_manager.html','src/services/esp32CameraService.js']) {
  const source=fs.readFileSync(file,'utf8'),react=file.endsWith('.js');let calls=0,mode='ok';
  let sent;
  const ctx={window:{},TextEncoder,SUPABASE_URL:'https://fixture.invalid',SUPABASE_ANON_KEY:'fixture',URL,atob,
   fetch:async(url,options)=>{sent=options;calls++;if(mode==='offline')throw Error('offline');return {ok:mode==='ok',status:mode==='ok'?200:500,text:async()=>mode==='ok'?'[]':'sensitive server detail'};}};
  vm.createContext(ctx);
  const script=react?source.slice(source.indexOf('async function supabaseRestRequest('),source.indexOf('/**',source.indexOf('async function supabaseRestRequest('))):
   source.slice(source.indexOf('    const cloudRequestMetrics ='),source.indexOf('    // DOM Elements'));
  vm.runInContext(script,ctx);const request=react?ctx.supabaseRestRequest:ctx.sbRest;
  for(const table of ['compartments','rpc/commit_inventory_observation'])await assert.rejects(()=>request(table));
  await assert.rejects(()=>request('esp32_cameras','GET','?select=compartments%28current_quantity%29'));
  assert.equal(calls,0,'scope rejected before network/counter increment');
  await request('esp32_cameras','GET','?select=device_id',null,null);
  await request('esp32_cameras','DELETE','?id=eq.fixture',null,null);
  assert.equal(ctx.window.cloudRequestMetrics.calls,2);assert.equal(ctx.window.cloudRequestMetrics.decoded_response_body_bytes,4);
  mode='bad';await assert.rejects(()=>request('cctv_cameras'),error=>!error.message.includes('sensitive'));
  mode='offline';await assert.rejects(()=>request('cctv_cameras'));
  assert.equal(ctx.window.cloudRequestMetrics.calls,4);assert.equal(ctx.window.cloudRequestMetrics.failures,2);
  mode='ok';const jwt=claims=>'header.'+Buffer.from(JSON.stringify(claims)).toString('base64url')+'.fixture-signature';
  const token=jwt({role:'authenticated',app_metadata:{camera_admin:true},exp:Math.floor(Date.now()/1000)+300});
  ctx.window.smartStorageDeviceAdminSession={access_token:token};await request('esp32_cameras','PATCH','?id=eq.fixture',{camera_name:'Updated'},null);
  assert.equal(sent.headers.Authorization,'Bearer '+token);assert.equal(sent.headers.apikey,'fixture');
  for(const secret of ['sb_secret_forbidden',jwt({role:'service_role',exp:Date.now()/1000+300}),jwt({role:'authenticated',exp:1})]){
   ctx.window.smartStorageDeviceAdminSession={access_token:secret};await assert.rejects(()=>request('cctv_cameras'),/session required/);
  }
  assert.equal(calls,5,'server secret/expired session never reaches fetch');delete ctx.window.smartStorageDeviceAdminSession;
  if(!react){
   vm.runInContext(source.slice(source.indexOf('    function cctvSourceBinding('),source.indexOf('    function validateCctvBinding(')),ctx);
   assert.equal(ctx.cctvSourceBinding('rtsp://operator:password@HOST/stream?channel=1&token=secret&api_key=secret&subtype=0'),'rtsp://host:554/stream?channel=1&subtype=0');
  }
  console.log('PASS: '+file+' device-only REST metrics, no embedded inventory, null Prefer deletion and redacted failure; binding token exclusion');
 }
})().catch(error=>{console.error(error);process.exitCode=1;});
