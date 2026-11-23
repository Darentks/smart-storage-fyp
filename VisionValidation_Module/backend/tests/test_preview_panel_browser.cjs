const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
(async()=>{for(const file of ['index.html','VisionValidation_Module/camera_manager.html']){
 const source=fs.readFileSync(file,'utf8'),elements={},requests=[];let inferences=0,mode='disabled';
 const element=()=>({style:{},removeAttribute(){delete this.src;}});
 const ctx={document:{getElementById:id=>elements[id] ||= element()},console:{info(){}},URLSearchParams,
  AbortSignal:{timeout:()=>undefined},getBridgeUrl:()=>'',refShelfSelect:{value:'SHELF_R1_C1'},
  refEspIpInput:{value:'192.168.0.201'},refEspIp2Input:{value:'192.168.0.202'},
  decodeTMImage:async x=>x,predictDualCamerasWithTM:async()=>{inferences++;return {backClass:'Back_11X',frontClass:'Front_111',backConfidence:95,frontConfidence:96};},
  fetch:async(url,options)=>{requests.push({url,options});if(mode==='offline')throw Error('offline');
   let row=url.includes('/edge_status')?{state:mode==='native'?'Enabled':'Disabled',authoritative_processor:mode==='native'?'native':'none'}:
    url.includes('/native_observation')?{observation:{image:'combined',image_back:'back',image_front:'front',timestamp:1000,candidate:{back_class:'Back_11X',front_class:'Front_111',back_confidence:96,front_confidence:97}}}:
    {image:'combined',image_back:'back',image_front:'front',timestamp:1000};
   return {ok:true,json:async()=>row};}};
 for(const key of ['previewOverlayText','backPreviewImg','frontPreviewImg','livePreviewImg','backPlaceholder','frontPlaceholder','previewPlaceholder','backPredBadge','frontPredBadge','tmLivePredictionBanner'])ctx[key]=element();
 vm.createContext(ctx);
 vm.runInContext(source.slice(source.indexOf('    let bridgeStatus='),source.indexOf('    async function fetchLiveAnglePreviewLocked')),ctx);
 vm.runInContext(source.slice(source.indexOf('    async function pollEdgeProcessor()'),source.indexOf('    function exportEdgeCalibrations()')),ctx);
 await ctx.pollEdgeProcessor();assert.match(elements.previewProcessorBanner.textContent,/Native Processor Disabled/);assert.equal(elements.diagnosticPreviewButton.disabled,false);
 await ctx.fetchLiveAnglePreview({runMode:'diagnostic'});assert.equal(inferences,1);assert.equal(elements.previewModeBadge.textContent,'Diagnostic Only (No Inventory Write)');
 assert.equal(ctx.backPreviewImg.src,'back');assert.match(ctx.frontPredBadge.textContent,/96/);
 assert.ok(requests.every(r=>!r.options?.method || r.options.method==='GET'));
 assert.ok(requests.every(r=>!r.url.includes('esp32_validate')&&!r.url.includes('esp32_trigger')));
 mode='native';await ctx.pollEdgeProcessor();assert.equal(inferences,1);assert.equal(elements.previewModeBadge.textContent,'Native Authoritative');assert.equal(elements.diagnosticPreviewButton.disabled,true);
 mode='offline';await ctx.pollEdgeProcessor();assert.equal(elements.previewProcessorBanner.textContent,'Bridge Disconnected');assert.equal(elements.diagnosticPreviewButton.disabled,true);assert.equal(ctx.backPreviewImg.src,undefined);
 console.log('PASS '+file+' disabled/diagnostic zero writes/native no inference/offline states');
}})().catch(e=>{console.error(e);process.exitCode=1;});
