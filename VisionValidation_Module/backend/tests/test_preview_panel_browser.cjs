const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
(async()=>{for(const file of ['index.html','VisionValidation_Module/camera_manager.html']){
 const source=fs.readFileSync(file,'utf8'),elements={},requests=[];let inferences=0,mode='disabled';
 const classList=()=>({names:new Set(),toggle(name,on){if(on)this.names.add(name);else this.names.delete(name);},contains(name){return this.names.has(name);}});
 const element=()=>({style:{},classList:classList(),removeAttribute(){delete this.src;}});
 const red=el=>el.classList.contains('status-error');
 const ctx={document:{getElementById:id=>elements[id] ||= element()},console:{info(){}},URLSearchParams,
  AbortSignal:{timeout:()=>undefined},getBridgeUrl:()=>'',refShelfSelect:{value:'SHELF_R1_C1'},
  refEspIpInput:{value:'192.168.0.201'},refEspIp2Input:{value:'192.168.0.202'},
  decodeTMImage:async x=>x,predictDualCamerasWithTM:async()=>{assert.equal(ctx.backPreviewImg.src,'back','show pictures before classification');assert.match(ctx.previewOverlayText.textContent,/classifying/);inferences++;return {backClass:'Back_11X',frontClass:'Front_111',backConfidence:95,frontConfidence:96};},
  fetch:async(url,options)=>{requests.push({url,options});if(mode==='offline')throw Error('offline');
   let row=url.includes('/edge_status')?{state:mode==='native'?'Enabled':'Disabled',authoritative_processor:mode==='native'?'native':'none'}:
    url.includes('/native_observation')?{observation:{image:'combined',image_back:'back',image_front:'front',timestamp:1000,candidate:{back_class:'Back_11X',front_class:'Front_111',back_confidence:96,front_confidence:97}}}:
    {image:'combined',image_back:'back',image_front:'front',timestamp:1000,...(mode==='native'?{inference_source:'native',candidate:{back_class:'Back_11X',front_class:'Front_111',back_confidence:96,front_confidence:42,hold:'LOW_CONFIDENCE'}}:{})};
   return {ok:true,json:async()=>row};}};
 for(const key of ['previewOverlayText','backPreviewImg','frontPreviewImg','livePreviewImg','backPlaceholder','frontPlaceholder','previewPlaceholder','backPredBadge','frontPredBadge','tmLivePredictionBanner'])ctx[key]=element();
 vm.createContext(ctx);
 vm.runInContext(source.slice(source.indexOf('    let bridgeStatus='),source.indexOf('    async function fetchLiveAnglePreviewLocked')),ctx);
 vm.runInContext(source.slice(source.indexOf('    async function pollEdgeProcessor()'),source.indexOf('    function exportEdgeCalibrations()')),ctx);
 await ctx.pollEdgeProcessor();assert.match(elements.previewProcessorBanner.textContent,/Automatic inventory checks are paused/);assert.equal(elements.diagnosticPreviewButton.disabled,false);
 await ctx.fetchLiveAnglePreview({runMode:'diagnostic'});assert.equal(inferences,1);assert.equal(elements.previewModeBadge.textContent,'Diagnostic preview');
 assert.equal(ctx.backPreviewImg.src,'back');assert.match(ctx.frontPredBadge.textContent,/96/);
 assert.ok(requests.every(r=>!r.options?.method || r.options.method==='GET'));
 assert.ok(requests.every(r=>!r.url.includes('esp32_validate')&&!r.url.includes('esp32_trigger')));
 mode='native';await ctx.pollEdgeProcessor();assert.equal(inferences,1);assert.equal(elements.previewModeBadge.textContent,'Automatic scan picture');
 assert.equal(elements.tmBatchButton.disabled,false,'the button stays clickable so a click can explain what is missing');
 assert.doesNotMatch(elements.tmBatchAvailability.textContent,/Please run Diagnostic Preview first/,'no error before the operator clicks');
 assert.ok(!red(elements.tmBatchAvailability));
 const beforeClick=requests.length;await ctx.captureTMTrainingBatch();
 assert.equal(requests.length,beforeClick,'a processor/bridge change requires a new preview: no reservation or capture');
 assert.match(elements.tmBatchStatus.textContent,/Please run Diagnostic Preview first/);assert.ok(red(elements.tmBatchStatus),'shown in red on click');
 assert.equal(elements.diagnosticPreviewButton.disabled,false,'the quick bright preview stays available beside the native processor');assert.equal(elements.diagnosticPreviewButton.textContent,'Run preview');
 assert.match(elements.diagnosticPreviewButton.title,/diagnostic only.*native processor keeps counting/i);
 ctx.decodeTMImage=async x=>x;  // an earlier step deliberately made decoding fail
 const beforeQuick=requests.length,beforeNativeInferences=inferences;await ctx.fetchLiveAnglePreview();
 assert.equal(inferences,beforeNativeInferences,'native preview must not run different browser models');
 assert.match(ctx.frontPredBadge.textContent,/42/);assert.match(ctx.tmLivePredictionBanner.textContent,/same native models.*LOW_CONFIDENCE/);
 const quick=requests.slice(beforeQuick).filter(r=>r.url.includes('/diagnostic_preview?'));
 assert.equal(quick.length,1,'one bridge capture, then Teachable Machine runs in the browser');assert.ok(!quick[0].options?.method || quick[0].options.method==='GET');
 assert.ok(!requests.slice(beforeQuick).some(r=>/esp32_validate|esp32_trigger|esp32_preview\?/.test(r.url)),'never the native-only capture or any write route');
 assert.equal(elements.previewModeBadge.textContent,'Diagnostic preview');
 assert.equal(ctx.trainingPreviewMissing(),false,'a successful preview for this shelf allows training');
 assert.equal(elements.tmBatchStatus.textContent,'','the earlier preview-required error clears after a successful preview');assert.ok(!red(elements.tmBatchStatus));
 assert.ok(!red(elements.tmBatchAvailability),'the ready message is not shown as an error');assert.ok(!red(ctx.tmLivePredictionBanner));
 assert.match(elements.tmBatchAvailability.textContent,/Other shelves keep counting; the selected shelf pauses and is counted fresh afterwards/);
 assert.doesNotMatch(elements.tmBatchAvailability.textContent,/automatic checks paused/i,'training pauses one shelf, not every shelf');
 ctx.refShelfSelect.value='SHELF_R1_C2';assert.equal(ctx.trainingPreviewMissing(),true,'a preview of another shelf does not count');
 ctx.refShelfSelect.value='SHELF_R1_C1';ctx.refEspIp2Input.value='192.168.0.209';assert.equal(ctx.trainingPreviewMissing(),true,'changed camera address needs a new preview');
 ctx.refEspIp2Input.value='192.168.0.202';assert.equal(ctx.trainingPreviewMissing(),false);
 const nativeCalls=requests.length;await ctx.pollEdgeProcessor();assert.ok(!requests.slice(nativeCalls).some(r=>r.url.includes('/native_observation')),'a fresh manual preview is not overwritten by the automatic native poll');
 assert.equal(elements.previewModeBadge.textContent,'Diagnostic preview');
 ctx.clearPanelFrames();
 assert.equal(ctx.trainingPreviewMissing(),false,'the panel clearing its pictures does not undo a successful preview');
 const restartedPanel={...ctx.bridgeStatus,startup_id:'new-run'};ctx.panelState(restartedPanel,true);
 assert.equal(ctx.trainingPreviewMissing(),true,'a bridge restart needs a new preview');
 await ctx.fetchLiveAnglePreview();assert.equal(ctx.trainingPreviewMissing(),false);
 ctx.clearManualPreview();await ctx.pollEdgeProcessor();assert.equal(elements.previewModeBadge.textContent,'Automatic scan picture','native observations return once the manual preview is released');
 mode='offline';await ctx.pollEdgeProcessor();assert.equal(elements.previewProcessorBanner.textContent,'Bridge Disconnected');assert.equal(elements.diagnosticPreviewButton.disabled,true);assert.equal(ctx.backPreviewImg.src,'back','a disconnect keeps the last pictures; only a restart clears them');
 assert.match(elements.tmBatchAvailability.textContent,/bridge disconnected/);
 ctx.panelState({state:'Disabled',authoritative_processor:'none'},true);
 ctx.fetch=async()=>({ok:false,json:async()=>({error:'Native service owns cameras; consume native observations'})});
 await ctx.fetchLiveAnglePreview();assert.equal(ctx.trainingPreviewMissing(),true,'a failed preview does not allow training');
 assert.ok(red(ctx.tmLivePredictionBanner),'a failed preview is shown in the red error style');assert.match(ctx.tmLivePredictionBanner.textContent,/older preview restriction.*Restart/);assert.doesNotMatch(ctx.previewOverlayText.textContent,/Capturing/);
 console.log('PASS '+file+' disabled/diagnostic zero writes/native no inference/offline states; immediate frames and stale-bridge feedback');
}})().catch(e=>{console.error(e);process.exitCode=1;});
