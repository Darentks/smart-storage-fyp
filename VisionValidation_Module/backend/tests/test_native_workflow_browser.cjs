const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const grab=(src,head)=>{const i=src.indexOf(head);assert(i>=0,head);let d=0;for(let k=src.indexOf('{',i);;k++){if(src[k]==='{')d++;if(src[k]==='}'&&--d===0)return src.slice(i,k+1);}};
(async()=>{for(const file of ['index.html','VisionValidation_Module/camera_manager.html']){
 const s=fs.readFileSync(file,'utf8'),els={},toasts=[],posts=[];let reply;
 const el=id=>els[id] ||= {textContent:'',hidden:true};
 const ctx={window:{},previewGeneration:1,bridgeStatus:{online:true,authoritative_processor:'native'},document:{getElementById:el},Date,Number,Set,
  AbortSignal:{timeout:()=>undefined},getBridgeUrl:()=>'http://bridge:8008',showToast:(m,t)=>toasts.push([t,m]),pollShelfReadiness:()=>posts.push('poll'),
  fetch:async(url,options)=>{posts.push([url,options]);return reply;}};
 vm.createContext(ctx);
 // The browser-only view, its native extension and the new live-activity helpers, exactly as shipped.
 vm.runInContext(s.slice(s.indexOf('    function runtimeInventoryView('),s.indexOf('    function renderCompartments(')),ctx);
 vm.runInContext(['async function requestNativeScan('].map(h=>grab(s,h)).join('\n'),ctx);

 const comp={shelf_id:'SHELF_R1_C2',max_capacity:5,status:'ONLINE'};
 const ready=(extra={})=>({state:'Ready',runtime:{state:'Ready',authoritative:true,precision:'EXACT',current_quantity:4,count_range:[4,4],observed_at:1700000000,baseline:null,latest_observation:null,...extra}});
 const native=(extra={})=>({actions_ready:true,actions_paused_reason:null,scan:{state:'Count ready',message:'Count ready'},...extra});

 let view=ctx.runtimeInventoryView(comp,{...ready(),native:native()});
 assert.match(view.display,/^4 \/ 5 boxes/);assert.equal(view.quantity,4);assert.equal(view.actions,'Actions ready');assert.equal(view.canRetry,false);assert.match(view.display,/counted/);
 view=ctx.runtimeInventoryView(comp,{state:'Ready',runtime:{...ready().runtime,precision:'RANGE',current_quantity:null,count_range:[2,3]},native:native({actions_ready:false,actions_paused_reason:'MQTT unavailable'})});
 assert.match(view.display,/^2–3 boxes \(B1 hidden — range; capacity 5\)/);assert.equal(view.quantity,null,'a range never becomes an integer');
 assert.equal(view.actions,'Actions paused: MQTT unavailable');assert.equal(view.actionsReady,false,'count stays visible while actions are paused');

 view=ctx.runtimeInventoryView(comp,{state:'Ready',native:native(),runtime:{...ready().runtime,
   precision:'RANGE',current_quantity:null,count_range:[2,3],baseline:{slots:[0,1,1,null,0]},
   latest_observation:{slots:[0,1,1,null,0]},historical:{total:3,precision:'EXACT',observed_at:1699999900}}});
 assert.match(view.display,/^2–3 boxes/);assert.doesNotMatch(view.display,/Last known|not verified/);
 assert.match(view.details,/historical, not verified\): 3 \/ 5 at/);
 assert.match(view.details,/Fill B2 before placing M2/);assert.match(view.details,/every possible B1/);
 assert.equal(view.actionsReady,true,'a verified range alone cannot pause actions');

 const failed={state:'Held',runtime:{state:'Held',authoritative:false,baseline:null,latest_observation:null},native:native({scan:{state:'Needs retry',message:'Count unavailable — front camera offline. Use Retry scan.'},actions_ready:false,actions_paused_reason:'Waiting for a fresh count'})};
 view=ctx.runtimeInventoryView(comp,failed);
 assert.equal(view.display,'Count unavailable — front camera offline. Use Retry scan.');assert.equal(view.quantity,null);assert.equal(view.canRetry,true);
 assert.ok(!/\d \/ 5/.test(view.display),'a failed scan must not display a guessed number');
 view=ctx.runtimeInventoryView(comp,{...failed,native:native({scan:{state:'Scanning',message:'Scanning'},actions_ready:false})});
 assert.match(view.display,/Scanning/);assert.equal(view.canRetry,false,'automatic attempts are not interrupted');
 view=ctx.runtimeInventoryView(comp,{...failed,native:native({scan:{state:'Unavailable',message:'Count unavailable — back camera is not assigned to this shelf.'},actions_ready:false})});
 assert.equal(view.canRetry,true);assert.match(view.display,/back camera/);

 // Old counts are history, never current.
 view=ctx.runtimeInventoryView(comp,{state:'Offline',runtime:{state:'Offline',authoritative:false,baseline:null,latest_observation:null,historical:{total:4,count_range:[4,4],precision:'EXACT',observed_at:1}},
   native:native({scan:{state:'Scanning',message:'Scanning'},actions_ready:false})});
 assert.equal(view.quantity,null);assert.match(view.details,/Last count \(historical, not verified\): 4 \/ 5/);assert.ok(!/^4 \/ 5/.test(view.display));

 // Without a native block the legacy view is unchanged (browser-only fallback).
 view=ctx.runtimeInventoryView(comp,ready());assert.equal(view.label,'Exact 4');assert.equal(view.display,undefined);

 assert.ok(!/nativeProcessorLabel|renderLiveActivity/.test(s),'removed processor/live-activity panel code');

 // Retry scan is a request to the native scanner, not an inventory write.
 reply={ok:true,status:200,json:async()=>({status:'scheduled',shelves:['SHELF_R1_C2'],busy:[]})};
 await ctx.requestNativeScan('SHELF_R1_C2');
 let [url,options]=posts.find(p=>Array.isArray(p));assert.equal(url,'http://bridge:8008/scan_request');assert.equal(options.method,'POST');assert.deepEqual(JSON.parse(options.body),{shelf_id:'SHELF_R1_C2'});
 assert.equal(options.headers.Authorization,undefined);assert.match(toasts.pop()[1],/hands clear/);
 ctx.window.smartStorageDeviceAdminSession={access_token:'user-session'};posts.length=0;
 await ctx.requestNativeScan();[url,options]=posts.find(p=>Array.isArray(p));assert.deepEqual(JSON.parse(options.body),{});assert.equal(options.headers.Authorization,'Bearer user-session');
 reply={ok:false,status:401,json:async()=>({error:'Sign in to configure the shared bridge'})};await ctx.requestNativeScan('SHELF_R1_C1');
 assert.deepEqual(toasts.pop(),['error','Sign in to configure the shared bridge']);
 reply={ok:true,status:200,json:async()=>({status:'scheduled',shelves:[],busy:['SHELF_R1_C1']})};await ctx.requestNativeScan('SHELF_R1_C1');assert.match(toasts.pop()[1],/already running/);

 // Shipped markup and card template.
 for(const text of ["onclick=\"requestNativeScan('${comp.shelf_id}')\"",'Retry scan'])assert.ok(s.includes(text),text);
 assert.ok(!/scanInitialCounts|initialCountScans|Scan initial counts/.test(s),'browser-only initial scan removed; native counting is automatic');
 assert.doesNotMatch(s,/id="(?:cameraAgreementStatus|processorSetupIssue|liveActivityStage|edgeProcessorStatus)"/);
 assert.ok(!s.includes('UNACCEPTED_DEVELOPMENT'),'no development-activation wording');
 console.log('PASS '+file+' native shelf cards (exact/range/failed/history/actions paused), Retry scan request');
}})().catch(e=>{console.error(e);process.exitCode=1;});
