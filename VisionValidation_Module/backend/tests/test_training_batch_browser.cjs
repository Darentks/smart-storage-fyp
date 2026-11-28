const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),{spawnSync}=require('node:child_process');
(async()=>{for(const file of ['index.html','VisionValidation_Module/camera_manager.html']){
 const source=fs.readFileSync(file,'utf8'),elements={},requests=[],preprocessed=[],feedback=[],sessions=[],asked=[];
 let decodeFailures=0,timeouts=0,busyLeft=0,waitingLeft=0,failFromPair=0,cancelOnFetch=false,onCapture=null,answers=[];
 const el=()=>({style:{},replaceChildren(){this.children=[];},appendChild(x){(this.children ||= []).push(x);},click(){throw Error('downloads must not start automatically');}});
 for(const [key,value] of Object.entries({tmBatchCount:'8',tmBatchBack:'Back_11X',tmBatchFront:'Front_010'}))elements[key]={...el(),value};
 const zips=[],history=[];
 elements.tmBatchStatus=el();let statusText='';
 Object.defineProperty(elements.tmBatchStatus,'textContent',{get:()=>statusText,set:value=>{statusText=value;history.push(value);}});
 const ctx={Blob,TextEncoder,Uint8Array,DataView,URLSearchParams,AbortController,Promise,console:{info(){}},
  URL:{createObjectURL(blob){zips.push(blob);return 'blob:test'+zips.length;},revokeObjectURL(){}},
  document:{getElementById:id=>elements[id] ||= el(),createElement:()=>el()},
  confirm(message){asked.push(message);return answers.length?answers.shift():true;},
  setTimeout(fn,ms){if(ms===2000)fn();return 1;},clearTimeout(){},
  bridgeStatus:{online:true,authoritative_processor:'native',startup_id:'run-1'},bridgeMissedPolls:0,previewGeneration:1,
  diagnosticRunning:false,tmComparisonRunning:false,tmBatchRunning:false,tmBatchCancelled:false,tmBatchController:null,tmBatchUrls:[],
  refShelfSelect:{value:'SHELF_R1_C1'},refEspIpInput:{value:'192.168.0.201'},refEspIp2Input:{value:'192.168.0.202'},
  panelState(){},getBridgeUrl:()=>'',previewMissing:false,markStatusError(element,on){element.red=!!on;},trainingPreviewMissing:()=>ctx.previewMissing,updateTrainingBatchAvailability(){},
  TRAINING_PREVIEW_REQUIRED:'Please run Diagnostic Preview first: check both camera pictures for the selected shelf before training capture.',
  decodeTMImage:async src=>{if(decodeFailures>0){decodeFailures--;throw Error('decode failed');}if(!src)throw Error('missing image');return {src,naturalWidth:640,naturalHeight:480};},
  preprocessForTM(image,boost){preprocessed.push({image,boost});return {};},tmCanvasPNG:async()=>new Blob(['pixels']),
  fetch:async(url,options)=>{
   if(url==='/training_session'){
    // The shelf reservation API: begin returns waiting until this shelf's check drains, then ready; end releases.
    const body=JSON.parse(options.body);sessions.push(body);assert.equal(options.method,'POST');
    assert.equal(body.shelf_id,'SHELF_R1_C1');assert.equal(body.ip,'192.168.0.201');assert.equal(body.ip2,'192.168.0.202');
    if(body.operation==='end')return {ok:true,json:async()=>({released:true,state:'released'})};
    return {ok:true,json:async()=>({state:waitingLeft-->0?'waiting':'ready',token:'reservation-1',shelf_id:body.shelf_id})};
   }
   requests.push({url,options});feedback.push(elements.tmBatchStatus.textContent);
   assert.equal(options.headers['X-Training-Session'],'reservation-1','every capture carries the reservation token');
   assert.equal(sessions.at(-1)?.operation,'begin','capture only after a ready reservation');
   if(onCapture)onCapture(requests.length);
   if(timeouts>0){timeouts--;throw Object.assign(Error('aborted'),{name:'AbortError'});}
   if(cancelOnFetch){ctx.cancelTMTrainingBatch();assert.match(elements.tmBatchStatus.textContent,/Cancelling/);}
   if(busyLeft-->0)return {ok:false,status:409,json:async()=>({error:'Automatic checks in progress'})};
   const pair=Number(new URLSearchParams(url.split('?')[1]).get('shelf_id')?1:0) && requests.length;
   if(failFromPair && ctx.capturedPairs>=failFromPair-1)return {ok:false,status:502,json:async()=>({error:'Back camera did not answer'})};
   return {ok:true,status:200,json:async()=>({purpose:'training',authorizes_write:false,image_back:'back',image_front:'front',timestamp:123})};}};
 Object.defineProperty(ctx,'capturedPairs',{get:()=>Math.floor(preprocessed.length/2)});
 vm.createContext(ctx);
 vm.runInContext(source.slice(source.indexOf('    async function buildTMTrainingZip('),source.indexOf('    async function fetchLiveAnglePreviewLocked(')),ctx);
 const build=ctx.buildTMTrainingZip;
 ctx.buildTMTrainingZip=async files=>{assert.match(elements.tmBatchStatus.textContent,/Preparing Back and Front ZIP|Batch failed|PARTIAL|pair/i);return build(files);};
 const reset=()=>{requests.length=0;preprocessed.length=0;sessions.length=0;asked.length=0;feedback.length=0;zips.length=0;history.length=0;answers=[];};
 const links=()=>elements.tmBatchDownload.children || [];
 const zipNames=async index=>{
  const check=spawnSync('python',['-c','import io,sys,zipfile,json\nz=zipfile.ZipFile(io.BytesIO(sys.stdin.buffer.read()))\nassert z.testzip() is None\nprint(json.dumps(z.namelist()))'],{input:Buffer.from(await zips[index].arrayBuffer())});
  assert.equal(check.status,0,check.stderr?.toString());return JSON.parse(check.stdout.toString());
 };

 // C8: label compatibility, derived from the same slot tables as the Python/native decoder.
 const python=spawnSync('python',['-c','import sys,json;sys.path.insert(0,"VisionValidation_Module/backend");from src.dual_cam_inventory import FRONT_STATES,BACK_STATES;print(json.dumps({"front":FRONT_STATES,"back":BACK_STATES}))']);
 assert.equal(python.status,0,python.stderr?.toString());
 const states=JSON.parse(python.stdout.toString());
 assert.deepEqual(JSON.parse(JSON.stringify(ctx.TRAINING_FRONT_STATES ?? vm.runInContext('TRAINING_FRONT_STATES',ctx))),states.front,'Front table matches the Python decoder');
 assert.deepEqual(JSON.parse(JSON.stringify(vm.runInContext('TRAINING_BACK_STATES',ctx))),states.back,'Back table matches the Python decoder');
 const compatible={Back_000:['Front_000','Front_010','Front_100','Front_110'],Back_100:['Front_000','Front_010','Front_100','Front_110'],
  Back_X01:['Front_011','Front_111'],Back_11X:['Front_000','Front_010','Front_011','Front_100','Front_110','Front_111']};
 for(const back of Object.keys(states.back))for(const front of Object.keys(states.front))
  assert.equal(ctx.trainingLabelConflict(back,front)===null,compatible[back].includes(front),`${back} + ${front}`);
 assert.match(ctx.trainingLabelConflict('Back_X01','Front_100'),/Back label says M2 is occupied, the Front label says it is empty/);
 elements.tmBatchBack.value='Back_X01';elements.tmBatchFront.value='Front_100';ctx.updateTrainingBatchPlan();
 assert.match(elements.tmBatchLabelCheck.textContent,/disagree on the shared M2 slot/);assert.equal(elements.tmBatchLabelCheck.red,true);
 elements.tmBatchBack.value='Back_11X';ctx.updateTrainingBatchPlan();assert.match(elements.tmBatchLabelCheck.textContent,/cannot be cross-checked/);assert.equal(elements.tmBatchLabelCheck.red,false);
 reset();elements.tmBatchBack.value='Back_X01';answers=[false];await ctx.captureTMTrainingBatch();
 assert.equal(requests.length+sessions.length,0,'incompatible labels are blocked unless confirmed');assert.equal(asked.length,1);
 assert.match(elements.tmBatchStatus.textContent,/Batch not started: the Back and Front labels disagree/);assert.equal(elements.tmBatchStatus.red,true);
 reset();answers=[true,false];await ctx.captureTMTrainingBatch();assert.equal(asked.length,2,'explicit confirmation passes the label check');assert.equal(sessions.length,0);
 elements.tmBatchBack.value='Back_11X';elements.tmBatchFront.value='Front_010';

 // C6 + C7: estimated time in the plan line, and a warning about re-counting before anything is reserved.
 elements.tmBatchCount.value='32';ctx.updateTrainingBatchPlan();assert.match(elements.tmBatchPlan.textContent,/^32 pairs = 64 training pictures, about 2 minutes/);
 elements.tmBatchCount.value='8';ctx.updateTrainingBatchPlan();assert.match(elements.tmBatchPlan.textContent,/^8 pairs = 16 training pictures, about 26 seconds/);
 reset();answers=[false];await ctx.captureTMTrainingBatch();
 assert.equal(sessions.length,0);assert.match(asked[0],/SHELF_R1_C1 pauses its automatic checks and its current count is cleared; it is counted fresh after the batch/);
 assert.equal(elements.tmBatchStatus.textContent,'Batch not started.');assert.equal(elements.tmBatchStatus.red,false);

 ctx.previewMissing=true;reset();await ctx.captureTMTrainingBatch();
 assert.equal(requests.length+sessions.length,0,'no reservation or capture without a diagnostic preview');
 assert.match(elements.tmBatchStatus.textContent,/Please run Diagnostic Preview first/);assert.equal(elements.tmBatchStatus.red,true,'shown in red');ctx.previewMissing=false;

 // Complete batch: click-only download links (C5), paired ZIPs, reservation released.
 reset();waitingLeft=2;await ctx.captureTMTrainingBatch();
 assert.deepEqual(sessions.map(s=>s.operation),['begin','begin','begin','end'],'waits for this shelf, then releases it');
 assert.equal(requests.length,8);assert.equal(preprocessed.length,16);assert.ok(preprocessed.every(x=>x.boost==='off'));
 assert.ok(requests.every(r=>r.url.startsWith('/diagnostic_preview?')&&r.url.includes('run_mode=diagnostic')&&r.url.includes('purpose=training')&&!r.options.method));
 assert.equal(zips.length,2);assert.match(elements.tmBatchStatus.textContent,/Ready: Back ZIP and Front ZIP, 8 training PNGs each\. Click both links/);
 assert.equal(elements.tmBatchStatus.red,false,'success is not shown as an error');
 assert.equal(links().length,2);assert.match(links()[0].download,/^TM_Back_Back_11X_SHELF_R1_C1_.*\.zip$/);assert.match(links()[1].download,/^TM_Front_Front_010_/);
 assert.doesNotMatch(links()[0].download,/PARTIAL/);assert.equal(links()[0].className,'btn-action');
 assert.ok(history.some(text=>/Captured 1\/8 pairs \(2 images\)\. about \d+ seconds left/.test(text)),'remaining time is shown');
 for(const [i,label] of ['Back_11X','Front_010'].entries()){
  const names=await zipNames(i);assert.equal(names.length,8);assert.ok(names.every(n=>n.startsWith(label+'_')&&n.endsWith('.png')&&!n.includes('/')));
 }
 assert.equal(elements.tmBatchProgress.value,8);assert.equal(elements.tmBatchProgress.max,8);assert.equal(elements.tmBatchProgress.hidden,false);

 // C1: a failed pair is retried; success after two failures keeps the whole batch.
 reset();decodeFailures=1;timeouts=1;await ctx.captureTMTrainingBatch();
 assert.equal(requests.length,10,'two retries');assert.match(elements.tmBatchStatus.textContent,/^Ready:/);
assert.ok(history.some(text=>/Pair 1\/8 failed \(camera capture timed out after 15 seconds\); retrying \(attempt 2 of 3\)/.test(text)));
 // A pair that keeps failing stops after three attempts.
 reset();decodeFailures=5;await ctx.captureTMTrainingBatch();
 assert.equal(requests.length,3);assert.match(elements.tmBatchStatus.textContent,/^Batch failed: pair 1 failed 3 times \(last: decode failed\)\.$/);
 assert.equal(elements.tmBatchStatus.red,true);assert.equal(links().length,0,'nothing captured, nothing offered');assert.equal(sessions.at(-1).operation,'end','an error releases the reservation');
 decodeFailures=0;

 // C2: a failure after some pairs offers only the completed pairs as clearly marked PARTIAL ZIPs.
 reset();failFromPair=3;await ctx.captureTMTrainingBatch();failFromPair=0;
 assert.match(elements.tmBatchStatus.textContent,/^Batch failed: pair 3 failed 3 times \(last: Back camera did not answer\)\. 2 of 8 pairs were captured; the PARTIAL ZIPs below contain only those pairs\./);
 assert.equal(links().length,2);assert.match(links()[0].download,/_PARTIAL_2of8\.zip$/);assert.match(links()[1].textContent,/Download PARTIAL Front ZIP \(2 of 8 pairs, Front_010\)/);
 assert.equal((await zipNames(0)).length,2);assert.equal((await zipNames(1)).length,2);

 // C3: one missed status check is tolerated; two in a row, or a different bridge run, stop the batch.
 reset();onCapture=n=>{if(n===3){ctx.bridgeStatus={online:false};ctx.bridgeMissedPolls=1;}if(n===5){ctx.bridgeStatus={online:true,startup_id:'run-1'};ctx.bridgeMissedPolls=0;}};
 await ctx.captureTMTrainingBatch();assert.match(elements.tmBatchStatus.textContent,/^Ready:/,'one missed poll does not discard the batch');
 reset();onCapture=n=>{if(n===3){ctx.bridgeStatus={online:false};ctx.bridgeMissedPolls=2;}};
 await ctx.captureTMTrainingBatch();assert.match(elements.tmBatchStatus.textContent,/^Batch failed: The bridge stopped answering during the batch\. 2 of 8 pairs/);
 reset();ctx.bridgeStatus={online:true,startup_id:'run-1'};ctx.bridgeMissedPolls=0;onCapture=n=>{if(n===2)ctx.bridgeStatus={online:true,startup_id:'run-2'};};
 await ctx.captureTMTrainingBatch();assert.match(elements.tmBatchStatus.textContent,/^Batch failed: The bridge restarted during the batch\. 1 of 8 pairs/);
 reset();ctx.bridgeStatus={online:true,startup_id:'run-2'};onCapture=n=>{if(n===2)ctx.refEspIp2Input.value='192.168.0.209';};
 await ctx.captureTMTrainingBatch();assert.match(elements.tmBatchStatus.textContent,/^Batch failed: The shelf or a camera address changed/);
 ctx.refEspIp2Input.value='192.168.0.202';onCapture=null;

 // Cancel discards; busy retries are separate from failure retries and still bounded.
 reset();cancelOnFetch=true;await ctx.captureTMTrainingBatch();cancelOnFetch=false;
 assert.equal(requests.length,1);assert.match(elements.tmBatchStatus.textContent,/cancelled/);assert.equal(links().length,0);
 assert.equal(sessions.at(-1).operation,'end','cancel releases the reservation');
 reset();busyLeft=2;await ctx.captureTMTrainingBatch();assert.equal(requests.length,10);assert.match(elements.tmBatchStatus.textContent,/Ready:/);
 assert.ok(feedback.some(message=>message.includes('Waiting for automatic camera checks')));
 reset();busyLeft=40;await ctx.captureTMTrainingBatch();busyLeft=0;
 assert.equal(requests.length,31);assert.match(elements.tmBatchStatus.textContent,/still using the cameras/);
 reset();ctx.bridgeStatus.online=false;await ctx.captureTMTrainingBatch();assert.equal(requests.length,0);ctx.bridgeStatus.online=true;
 elements.tmBatchBack.value='';await ctx.captureTMTrainingBatch();assert.equal(requests.length,0);elements.tmBatchBack.value='Back_11X';
 assert.equal(ctx.diagnosticRunning,false);assert.equal(ctx.tmBatchRunning,false);
 console.log('PASS '+file+' label check vs decoder, estimate, re-count warning, pair retries, partial ZIPs, missed poll, click-only links, cancel and busy limits');
}})().catch(e=>{console.error(e);process.exitCode=1;});
