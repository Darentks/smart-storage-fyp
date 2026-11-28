const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
(async()=>{
  for(const file of ['index.html','VisionValidation_Module/camera_manager.html']) {
    const html=fs.readFileSync(file,'utf8'), requests=[];
    const ctx={validatingShelves:new Set(),rememberProcessedTrigger(){},showValidationQueueError(){},
      bridgePost:async(path,payload)=>{requests.push({path,payload});return {claimed:true};},
      executeFullDualCamValidation:async(...args)=>{ctx.execution=args;},
      ValidationQueue:class{constructor(options){ctx.worker=options;}}};
    vm.createContext(ctx);
    vm.runInContext(html.slice(html.indexOf('    const validationWorker ='),html.indexOf('    function decodeTMImage(')),ctx);
    const item={shelfId:'s',source:'CCTV',action:'BORROW',trigger:{trigger_id:'selected'},claimed:true};
    await ctx.worker.claim(item,'lease');
    assert.deepEqual(JSON.parse(JSON.stringify(requests.at(-1))),{path:'/cctv_trigger_claim',payload:{trigger_id:'selected',shelf_id:'s',validation_token:'lease'}});
    await ctx.worker.execute(item,'lease');
    assert.deepEqual(Array.from(ctx.execution),['s','BORROW',true,'lease','selected','CCTV']);
    await ctx.worker.release(item,'lease');
    assert.equal(requests.at(-1).payload.trigger_id,'selected');
    // Exercise the real final payload, with confirmation substituted for camera I/O.
    Object.assign(ctx,{window:{inventoryMode:'legacy-test-fixture'},bridgeStatus:{authoritative_processor:'native'},compartmentsData:[{shelf_id:'s',ip_address:'back',secondary_ip_address:'front'}],
      validationFailureCode:()=>"VALIDATION_REJECTED",readinessRun:"test-run",pollShelfReadiness(){},showToast(){},document:{getElementById:()=>null},tmLivePredictionBanner:{style:{}},
      livePreviewImg:null,previewPlaceholder:null,backPreviewImg:null,frontPreviewImg:null,backPlaceholder:null,frontPlaceholder:null,
      dualCamEvidence:()=>({}),prependLatestAction(){},refreshAllData(){},getBridgeUrl:()=>'',
      captureConfirmedPair:async()=>({samples:[],second:{prediction:{totalBoxes:3,confidence:95},
        preview:{image:'pair',image_back:'b',image_front:'f'},settings:{boost:'off'}}}),
      fetch:async(url,options)=>{requests.push({path:url,payload:JSON.parse(options.body)});
        return {ok:true,json:async()=>({status:'success',snapshot_initialized:true,comparison:{delta:0}})};}});
    vm.runInContext(html.slice(html.indexOf('    async function executeFullDualCamValidation('),html.indexOf('    // Trigger Test Capture')),ctx);
    const result=await ctx.executeFullDualCamValidation('s','BORROW',true,'lease','selected');
    assert.equal(result.status,'success',result.error);
    assert.equal(requests.at(-1).payload.trigger_id,'selected');
    assert.equal(requests.at(-1).payload.validation_token,'lease');
    assert.match(ctx.tmLivePredictionBanner.textContent,/Occupancy initialized successfully at 3 boxes/);
    ctx.fetch=async()=>({ok:false,json:async()=>({error:'Prior occupancy unavailable: cameras observed 4 boxes but stored quantity is 5'})});
    const failed=await ctx.executeFullDualCamValidation('s','BORROW',true,'lease','selected');
    assert.equal(failed.status,'failed');
    assert.match(ctx.tmLivePredictionBanner.textContent,/Full Validation failed.*cameras observed 4 boxes/);
    console.log('PASS: '+file+' CCTV trigger and lease propagated through claim, execution, validation payload and release');
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
