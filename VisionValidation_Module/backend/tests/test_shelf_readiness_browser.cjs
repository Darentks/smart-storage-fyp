const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
(async()=>{for(const file of ['index.html','VisionValidation_Module/camera_manager.html']){
 const html=fs.readFileSync(file,'utf8');let run='one',state='Starting',online=true;const jobs=[],toasts=[],requests=[];
 const ctx={window:{tmModel:{}},compartmentsData:[],getBridgeUrl:()=>'',renderCompartments(){},showToast:(...x)=>toasts.push(x),
 validationWorker:{submit:job=>{jobs.push(job);return Promise.resolve();}},fetch:async(url,options)=>{requests.push({url,options});if(!online)throw Error('offline');return {ok:true,json:async()=>({run_id:run,shelves:{s:{state,reason:''}}})};}};
 vm.createContext(ctx);vm.runInContext(html.slice(html.indexOf('    function readinessText('),html.indexOf('    const MAX_PROCESSED_TRIGGER_IDS')),ctx);
 vm.runInContext(html.slice(html.indexOf('    function nativeShelfView('),html.indexOf('    function renderCompartments(')),ctx);
 const waiting='Waiting for the rack to clear: CCTV_02 detects someone in a shelf region';
 const card=ctx.nativeShelfView({details:''},{max_capacity:5},{native:{scan:{state:'Waiting',message:waiting}}},{},false,false);
 assert.equal(card.display,waiting,'compartment card must show the camera-specific waiting explanation');

 // A count check that could not verify keeps the last count visible, never just "Count ready".
 vm.runInContext(html.slice(html.indexOf('    function runtimeInventoryView('),html.indexOf('    function nativeShelfView(')),ctx);
 const comp={shelf_id:'s',max_capacity:5};
 const shelf=(runtime)=>ctx.runtimeInventoryView(comp,{state:'Ready',native:{scan:{state:'Count ready',message:'Count ready'}},
  runtime:{state:'Ready',precision:'EXACT',current_quantity:3,count_range:[3,3],observed_at:1000,baseline:{total:3,count_range:[3,3]},...runtime}});
 const failedCheck={status:'Check failed',message:'Low confidence',checked_at:1030};
 const failed=shelf({authoritative:false,count_check_unverified:true,count_check:failedCheck});
 assert.match(failed.display,/^Last known: 3 \/ 5 boxes \(not verified\) .*counted .*\nCheck failed$/);
 assert.equal(failed.reason,'Latest check not verified: Low confidence');
 assert.doesNotMatch(failed.display+failed.reason,/Count ready/);
 assert.equal(failed.quantity,null,'unverified counts must not fill the slot bars');
 const ranged=shelf({authoritative:false,count_check_unverified:true,precision:'RANGE',baseline:{total:null,count_range:[2,3]},
  count_check:{status:'Hidden box not checked',message:'Hidden B1 was not checked.',checked_at:1030}});
 assert.match(ranged.display,/^Last known: 2–3 \/ 5 boxes \(not verified\)[\s\S]*Hidden box not checked$/);
 const recovered=shelf({authoritative:true,count_check_unverified:false,count_check:{status:'Count unchanged',message:'Confirmed.',checked_at:1060}});
 assert.match(recovered.display,/^3 \/ 5 boxes[\s\S]*Count unchanged$/);assert.equal(recovered.quantity,3);assert.equal(recovered.reason,null);
 const deferred=shelf({authoritative:true,count_check_unverified:false,count_check:{status:'Check deferred',message:'CCTV pending',checked_at:1060}});
 assert.match(deferred.display,/^3 \/ 5 boxes/);assert.doesNotMatch(deferred.display,/not verified/);
 // A shelf that left Ready never presents its old baseline through this path.
 const held=ctx.runtimeInventoryView(comp,{state:'Held',native:{scan:{state:'Waiting',message:'Waiting to scan'}},
  runtime:{state:'Held',authoritative:false,count_check_unverified:true,baseline:{total:3,count_range:[3,3]}}});
 assert.equal(held.display,'Waiting to scan');
 await ctx.pollShelfReadiness();await ctx.pollShelfReadiness();assert.equal(jobs.length,0);
 state='Held';await ctx.pollShelfReadiness();assert.equal(jobs.length,0);
 run='two';state='Starting';await ctx.pollShelfReadiness();assert.equal(jobs.length,0);assert.match(toasts.at(-1)[0],/Bridge restarted/);
 online=false;await ctx.pollShelfReadiness();online=true;await ctx.pollShelfReadiness();assert.equal(jobs.length,0);assert.ok(requests.every(r=>r.url==='/shelf_readiness' && !r.options?.method));
 console.log('PASS: '+file+' monitoring-only startup/restart/reconnect with no jobs or mutation requests');
}})().catch(e=>{console.error(e);process.exitCode=1;});
