// The latest-activity panel was removed; its two shared helpers stay: friendly intent wording and the
// CCTV agreement line shown under the camera view.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const grab=(src,head)=>{const i=src.indexOf(head);assert.ok(i>=0,head);let d=0;for(let k=src.indexOf('{',i);;k++){if(src[k]==='{')d++;if(src[k]==='}'&&--d===0)return src.slice(i,k+1);}};
for(const file of ['index.html','VisionValidation_Module/camera_manager.html']){
 const source=fs.readFileSync(file,'utf8'),nodes={},toasts=[];
 const el=()=>({style:{},children:[],textContent:''});
 const ctx={document:{getElementById:id=>nodes[id] ||= el()},compartmentsData:[{shelf_id:'SHELF_R1_C2',rack_id:'RACK_01'}],showToast:(...args)=>toasts.push(args)};
 vm.createContext(ctx);
 vm.runInContext(grab(source,'    function localIntentPresentation(')+'\n    const cctvSeenConsensusResults = new Set();\n'+grab(source,'    function showCctvConsensusResult('),ctx);
 const record={id:'event',timestamp:1234,shelf_id:'SHELF_R1_C2',action:'BORROW',status:'CONSENSUS',camera_id:'CCTV_02'};
 const view=ctx.localIntentPresentation(record);
 assert.equal(view.location,'Rack RACK_01 · Compartment 2 (SHELF_R1_C2)');assert.equal(view.status,'Camera agreement confirmed');
 assert.match(view.explanation,/checking the box count now.*not a completed pick/);
 assert.equal(ctx.localIntentPresentation({...record,status:'ACCEPTED'}).status,'Borrow confirmed');
 assert.equal(ctx.localIntentPresentation({...record,status:'RECONCILED'}).status,'Shelf verification completed');
 assert.equal(ctx.localIntentPresentation({...record,shelf_id:'ANY_SHELF'}).action,'Presence');
 assert.equal(ctx.localIntentPresentation({...record,shelf_id:'SHELF_R1_C3'}).location,'Rack RACK_01 · Compartment 3 (SHELF_R1_C3)');
 assert.equal(ctx.localIntentPresentation({...record,shelf_id:'SHELF_R1_C6'}).location,'Compartment not identified (entry-zone observation)'); // retired column
 const notice={trigger_id:'agreement',shelf_id:'SHELF_R1_C2',consensus_met:true,confirmed_count:3,required_cameras:3,confirmed_cameras:['CCTV_01','CCTV_02','CCTV_03'],expected_action:'BORROW',inventory_eligible:false};
 ctx.showCctvConsensusResult(notice);
 assert.match(nodes.cctvConsensusResult.textContent,/RACK_01.*Compartment 2/);
 assert.match(nodes.cctvConsensusResult.textContent,/Inventory confirmation is paused/);
 const shown=nodes.cctvConsensusResult.textContent;nodes.cctvConsensusResult.textContent='';
 ctx.showCctvConsensusResult(notice);assert.equal(nodes.cctvConsensusResult.textContent,'','duplicate agreement is shown once');
 ctx.showCctvConsensusResult({...notice,trigger_id:'partial',consensus_met:false});assert.equal(nodes.cctvConsensusResult.textContent,'');
 assert.equal(toasts.length,0,'agreement must not interrupt testing with a toast');assert.ok(shown.length>0);
 assert.doesNotMatch(source,/cctvConsensusPopup|All 3 cameras agree/,'agreement modal is removed');
 assert.doesNotMatch(source,/<h3>Processor &amp; live activity<|id="(?:edgeProcessorStatus|processorSetupIssue|cameraAgreementStatus|liveActivityStage|confirmedActionBanner|localOperationsStatus|cctvDiagnosticActivity)"/);
 assert.doesNotMatch(source,/renderLocalOperations|refreshLocalOperations|showLocalOperations|notifyLiveConsensus|\/local_operations`/,'removed activity panel code and polling');
 console.log('PASS '+file+' friendly intent statuses/location, CCTV agreement line once per trigger without toasts, removed activity panel stays removed');
}
