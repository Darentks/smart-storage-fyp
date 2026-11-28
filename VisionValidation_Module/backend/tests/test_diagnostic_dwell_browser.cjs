const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const grab=(src,head)=>{const i=src.indexOf(head);assert.ok(i>=0,head);let d=0;for(let k=src.indexOf('{',i);;k++){if(src[k]==='{')d++;if(src[k]==='}'&&--d===0)return src.slice(i,k+1);}};
(async()=>{for(const file of ['index.html','VisionValidation_Module/camera_manager.html']){
 const s=fs.readFileSync(file,'utf8'),nodes={},requests=[],toasts=[];let syncs=0,consumed=0;
 const el=()=>({style:{},children:[],appendChild(x){this.children.push(x);},replaceChildren(){this.children=[];}});
 const ctx={window:{inventoryMode:'runtime'},document:{getElementById:id=>nodes[id] ||= el(),createElement:()=>el()},
  compartmentsData:[{shelf_id:'SHELF_R1_C2',rack_id:'RACK_01'}],activeCctvId:'CCTV_01',activeCameraRole:'GRID_6_ROI',
  syncCctvAlignmentConfig:async()=>{syncs++;},getBridgeUrl:()=>'',
  showToast:(...x)=>toasts.push(x),consumeCctvTrigger:async()=>{consumed++;},showCctvConsensusResult(){},
  fetch:async(url,options)=>{requests.push({url,options});return {ok:false,json:async()=>({error:'Service owns processing; normal dashboard operations are monitoring-only'})};}};
 vm.createContext(ctx);
 vm.runInContext(grab(s,'    async function dispatchDwellTrigger('),ctx);
 const result=await ctx.dispatchDwellTrigger('SHELF_R1_C2','BORROW',1.4,.9);
 assert.equal(result.status,'diagnostic_only');assert.equal(result.authorizes_write,false);assert.equal(result.consensus_met,false);
 assert.equal(syncs,0);assert.equal(requests.length,0);assert.equal(consumed,0);assert.equal(toasts.length,0);
 assert.match(nodes.cctvLastTriggerText.textContent,/Preview gesture observed.*no vote sent/);
 for(let i=0;i<8;i++)assert.equal((await ctx.dispatchDwellTrigger('SHELF_R1_C2','RETURN',1.5,.9)).status,'diagnostic_only');
 assert.equal((await ctx.dispatchDwellTrigger('ANY_SHELF','BORROW',1.5,.9)).authorizes_write,false);
 assert.equal(requests.length,0);assert.ok(!/recordDiagnosticWristGesture|cctvDiagnosticActivity/.test(s),'undisplayed gesture list removed');
 assert.ok(!/isRecordedCctvTest|cctvReplayRequest/.test(s),'recorded-clip replay removed');
 ctx.window.inventoryMode='legacy-test-fixture';await ctx.dispatchDwellTrigger('SHELF_R1_C2','BORROW',1.4,.9);
 assert.equal(requests.length,1);assert.equal(syncs,1);assert.match(nodes.cctvLastTriggerText.textContent,/Vote rejected.*Service owns processing/);
 assert.equal(consumed,0);
 console.log('PASS '+file+' runtime dwell zero network/mutations, no gesture list, no replay path, legacy rejection reason');
}})().catch(e=>{console.error(e);process.exitCode=1;});
