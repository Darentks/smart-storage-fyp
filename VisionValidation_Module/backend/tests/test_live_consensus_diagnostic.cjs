const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
(async()=>{for(const file of ['index.html','VisionValidation_Module/camera_manager.html']){
 const source=fs.readFileSync(file,'utf8'),nodes={},requests=[],notifications=[];
 const el=()=>({style:{},children:[],textContent:'',append(...xs){this.children.push(...xs);},appendChild(x){this.children.push(x);},replaceChildren(){this.children=[];}});
 const ctx={module:{exports:{}},structuredClone,AbortController,performance,crypto:{randomUUID:()=>String(Math.random())},clearTimeout(){},clearInterval(){},
  document:{createElement:()=>el(),getElementById:id=>nodes[id] ||= el()},localStorage:{getItem:()=>null},
  fetch:async(...args)=>{requests.push(args);throw Error('unexpected fetch');},showToast:(...x)=>notifications.push(x),
  localIntentPresentation:b=>({location:'Rack RACK_01 · Compartment 2',action:b.expected_action==='RETURN'?'Return':'Borrow'})};
 vm.createContext(ctx);vm.runInContext(source.slice(source.indexOf('class ThreeCameraDashboard'),source.indexOf('</script>',source.indexOf('class ThreeCameraDashboard'))),ctx);
 const Dashboard=ctx.module.exports;let votes=0,resets=0;
 const dashboard=new Dashboard({bridge:()=>'',resetTracker:()=>({}),sourceBinding:x=>x,status(){},event(){},consume(){throw Error('must not consume');},diagnosticVote(){votes++;},resetDiagnostic(){resets++;}});
 dashboard.poll=()=>{};dashboard.schedule=()=>{};dashboard.telemetry=()=>{throw Error('diagnostic telemetry must not run');};ctx.threeCameraDashboard=dashboard;
 const cameras=['CCTV_01','CCTV_02','CCTV_03'].map((id,i)=>({cctv_id:id,rtsp_url:'fixture'+i,camera_role:i===2?'SIDE_PRESENCE':'GRID_6_ROI',is_active:true}));
 await dashboard.start(cameras,{SHELF_R1_C2:{}},{diagnosticOnly:true});assert.equal(requests.length,0);assert.equal(dashboard.contexts.size,3);
 await assert.rejects(()=>dashboard.post('/esp32_trigger',{}),/cannot send POST/);
 for(const c of dashboard.contexts.values())c.received=c.processed=performance.now();
 dashboard.vote(dashboard.contexts.get('CCTV_01'),'SHELF_R1_C2','BORROW',1.3,.9);await dashboard.votes;assert.equal(votes,1);assert.equal(requests.length,0);assert.equal(dashboard.validations.size,0);
 await dashboard.invalidate(dashboard.contexts.get('CCTV_01'));assert.equal(resets,1);assert.equal(requests.length,0);
 vm.runInContext(source.slice(source.indexOf('    let diagnosticConsensusVotes='),source.indexOf('    let roiDraft =')),ctx);
 const now=Date.now()/1000,make=(id,action='BORROW',shelf='SHELF_R1_C2')=>({camera_id:id,camera_role:id==='CCTV_03'?'SIDE_PRESENCE':'GRID_6_ROI',shelf_id:id==='CCTV_03'?'ANY_SHELF':shelf,expected_action:action,detected_at:now,oks_confidence:.9,dwell_duration_seconds:1.3});
 const good=Object.fromEntries(cameras.map(c=>[c.cctv_id,make(c.cctv_id)]));
 assert.equal(ctx.evaluateDiagnosticConsensus(good,now).matched,true);
 assert.equal(ctx.evaluateDiagnosticConsensus(good,now+4.01).matched,false);
 for(const bad of [{...good,CCTV_02:make('CCTV_02','RETURN')},{...good,CCTV_02:make('CCTV_02','BORROW','SHELF_R1_C3')},{...good,CCTV_03:undefined}]){
  const votes=Object.fromEntries(Object.entries(bad).filter(([,x])=>x));assert.equal(ctx.evaluateDiagnosticConsensus(votes,now).matched,false);
 }
 for(const patch of [{dwell_duration_seconds:1},{oks_confidence:NaN},{detected_at:now+2},{camera_role:'GRID_6_ROI'}])assert.equal(ctx.evaluateDiagnosticConsensus({...good,CCTV_03:{...good.CCTV_03,...patch}},now).matched,false);
 for(const c of dashboard.contexts.values())c.received=c.processed=performance.now();
 for(const body of Object.values(good))ctx.recordDiagnosticConsensusVote(body);
 assert.equal(notifications.length,1);assert.match(notifications[0][0],/TEST ONLY: All 3 matched/);
 ctx.renderDiagnosticConsensus();assert.equal(notifications.length,1);
 const flatten=n=>[n,...n.children.flatMap(flatten)];assert.match(flatten(nodes.diagnosticConsensusStatus).map(n=>n.textContent).join(' '),/Last TEST match: Rack RACK_01/);
 assert.equal(requests.length,0);dashboard.stop();ctx.renderDiagnosticConsensus();assert.match(nodes.diagnosticConsensusStatus.children[0].textContent,/stopped/);
 // Loading camera cards must not attempt forbidden alignment writes in runtime mode.
 ctx.window={inventoryMode:'runtime'};
 vm.runInContext(source.slice(source.indexOf('    async function syncCctvAlignmentConfig()'),source.indexOf('    const cctvSeenConsensusResults')),ctx);
 await ctx.syncCctvAlignmentConfig();assert.equal(requests.length,0);
 console.log('PASS '+file+' read-only three-feed startup/vote/invalidate; no POST/validation; 4s TTL, mismatch, side presence, dwell and duplicate notification checks');
}})().catch(e=>{console.error(e);process.exitCode=1;});
