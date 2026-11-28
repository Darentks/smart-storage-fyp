const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
(async()=>{for(const file of ['index.html','VisionValidation_Module/camera_manager.html']){
 const source=fs.readFileSync(file,'utf8'),elements={},requests=[];
 const classList=()=>({names:new Set(),toggle(name,on){if(on)this.names.add(name);else this.names.delete(name);},contains(name){return this.names.has(name);}});
 const element=()=>({style:{},classList:classList(),removeAttribute(){delete this.src;}});
 const red=el=>el.classList.contains('status-error');
 let status={state:'Running',authoritative_processor:'service',startup_id:'run-1'},offline=false,observation=null;
 const previews=[],classifications=[],banners=[];
 const picture={image:'combined',image_back:'back',image_front:'front',timestamp:Date.now()/1000-5};
 const ctx={document:{getElementById:id=>elements[id] ||= element()},console:{info(){}},URLSearchParams,Number,Date,
  AbortSignal:{timeout:()=>undefined},getBridgeUrl:()=>'',refShelfSelect:{value:'SHELF_R1_C1'},
  refEspIpInput:{value:'192.168.0.201'},refEspIp2Input:{value:'192.168.0.202'},
  setTimeout(fn){fn();return 1;},
  fetch:async(url)=>{
   requests.push(url);
   if(offline)throw Error('offline');
   if(url.includes('/edge_status'))return {ok:true,json:async()=>status};
   if(url.includes('/native_observation'))return {ok:true,json:async()=>({observation})};
   if(url.includes('/diagnostic_classify')){const reply=classifications.shift();return {ok:true,status:200,json:async()=>reply};}
   const [code,reply]=previews.shift();return {ok:code===200,status:code,json:async()=>reply};
  }};
 for(const key of ['previewOverlayText','backPreviewImg','frontPreviewImg','livePreviewImg','backPlaceholder','frontPlaceholder','previewPlaceholder','backPredBadge','frontPredBadge'])ctx[key]=element();
 ctx.tmLivePredictionBanner=element();let banner='';
 Object.defineProperty(ctx.tmLivePredictionBanner,'textContent',{get:()=>banner,set:value=>{banner=value;banners.push(value);}});
 vm.createContext(ctx);
 vm.runInContext(source.slice(source.indexOf('    let bridgeStatus='),source.indexOf('    async function fetchLiveAnglePreviewLocked')),ctx);
 vm.runInContext(source.slice(source.indexOf('    async function pollEdgeProcessor()'),source.indexOf('    function exportEdgeCalibrations()')),ctx);
 ctx.refreshLocalOperations=async()=>{};ctx.renderLiveActivity=()=>{};
 await ctx.pollEdgeProcessor();assert.equal(vm.runInContext('bridgeStatus.online',ctx),true);
 const done={...picture,classification:'done',inference_source:'native',candidate:{back_class:'Back_11X',front_class:'Front_111',back_confidence:96,front_confidence:97,hold:null}};

 // D6: a busy shelf is retried with a visible "Waiting" message, then the preview completes.
 previews.push([409,{code:'BUSY',error:'This shelf is being scanned right now; retrying'}],[409,{code:'BUSY',error:'This shelf is being scanned right now; retrying'}],[200,done]);
 banners.length=0;await ctx.fetchLiveAnglePreview();
 assert.ok(banners.some(text=>text==='Waiting: This shelf is being scanned right now; retrying (1/15).'));
 assert.equal(ctx.frontPredBadge.textContent,'Front_111 (97.0%)');assert.equal(red(ctx.tmLivePredictionBanner),false);
 assert.match(ctx.previewOverlayText.textContent,/^Diagnostic preview · captured .+ \(\d+ s ago\)$/,'D3: capture time and age are shown');
 previews.push(...Array.from({length:15},()=>[409,{code:'BUSY',error:'busy'}]));
 await ctx.fetchLiveAnglePreview();assert.match(banner,/Diagnostic failed: The cameras stayed busy/);assert.equal(red(ctx.tmLivePredictionBanner),true);

 // D5: a capture error names the camera.
 previews.push([502,{code:'CAMERA_NO_PICTURE',camera:'back',error:'The Back camera (192.168.0.201) did not return a picture; the Front camera worked.'}]);
 await ctx.fetchLiveAnglePreview();
 assert.equal(banner,'Diagnostic failed: The Back camera (192.168.0.201) did not return a picture; the Front camera worked.');assert.equal(red(ctx.tmLivePredictionBanner),true);
 assert.equal(ctx.trainingPreviewMissing(),true,'a failed capture does not count as a preview');

 // D2: pictures stay on screen when classification fails; the cameras did answer.
 ctx.clearPanelFrames();
 previews.push([200,{...picture,image_back:'back-2',classification:'failed',classification_message:'The box-reading model could not classify these pictures.',preview_id:'p1'}]);
 await ctx.fetchLiveAnglePreview();
 assert.equal(ctx.backPreviewImg.src,'back-2');assert.equal(ctx.backPreviewImg.style.display,'block');
 assert.match(banner,/^Pictures captured, but classification failed: The box-reading model could not classify/);assert.equal(red(ctx.tmLivePredictionBanner),true);
 assert.equal(ctx.trainingPreviewMissing(),false,'both pictures arrived, so training may start');

 // D1: while scans use the model, classification is deferred and retried on the same pictures (no new capture).
 previews.push([200,{...picture,classification:'deferred',classification_message:'An automatic check is using the box-reading model; classification will run when it finishes.',preview_id:'p2'}]);
 classifications.push({classification:'deferred'},{classification:'done',candidate:{back_class:'Back_100',front_class:'Front_110',back_confidence:91,front_confidence:93,hold:null}});
 banners.length=0;const before=requests.length;await ctx.fetchLiveAnglePreview();
 assert.ok(banners.some(text=>/^Pictures captured\. An automatic check is using the box-reading model.*Waiting \(1\/30\)/.test(text)));
 assert.equal(requests.slice(before).filter(url=>url.includes('/diagnostic_preview')).length,1,'one capture only');
 assert.equal(requests.slice(before).filter(url=>url.includes('/diagnostic_classify?preview_id=p2')).length,2);
 assert.equal(ctx.backPredBadge.textContent,'Back_100 (91.0%)');assert.match(banner,/same native models/);

 // D3: the pinned preview is not replaced by automatic pictures; its age keeps updating.
 observation={...picture,image_back:'auto-back',source:'count_check',age_seconds:12,candidate:{back_class:'Back_000',front_class:'Front_000',back_confidence:99,front_confidence:99}};
 const pollStart=requests.length;await ctx.pollEdgeProcessor();
 assert.ok(!requests.slice(pollStart).some(url=>url.includes('/native_observation')));assert.equal(ctx.backPreviewImg.src,'back');
 assert.match(ctx.previewOverlayText.textContent,/^Diagnostic preview · captured/);
 // A single missed status check (brief disconnect) keeps the preview and pictures.
 offline=true;await ctx.pollEdgeProcessor();offline=false;
 assert.equal(ctx.backPreviewImg.src,'back');await ctx.pollEdgeProcessor();assert.equal(ctx.manualPreviewPinned(),true,'same bridge run: still pinned');

 // D7: once the preview is released (another shelf), the panel shows the latest automatic picture with its age.
 ctx.clearManualPreview();await ctx.pollEdgeProcessor();
 assert.equal(ctx.backPreviewImg.src,'auto-back');assert.equal(elements.previewModeBadge.textContent,'Automatic count check picture');
 assert.match(ctx.previewOverlayText.textContent,/^Automatic count check picture · .+ · last picture 12 s ago$/);
 observation={...picture,image_back:'train-back',source:'training',age_seconds:3,candidate:null};
 await ctx.pollEdgeProcessor();
 assert.equal(elements.previewModeBadge.textContent,'Training capture picture');assert.equal(ctx.backPredBadge.style.display,'none');
 assert.match(banner,/^Training capture picture: no prediction/);
 observation=null;await ctx.pollEdgeProcessor();assert.equal(elements.previewModeBadge.textContent,'No recent picture of this shelf yet');

 // D3: a bridge restart (new run ID) clears the pictures and the pinned preview.
 previews.push([200,done]);await ctx.fetchLiveAnglePreview();assert.equal(ctx.manualPreviewPinned(),true);
 status={...status,startup_id:'run-2'};observation=null;await ctx.pollEdgeProcessor();
 assert.equal(ctx.manualPreviewPinned(),false);assert.equal(ctx.backPreviewImg.src,undefined);assert.equal(ctx.trainingPreviewMissing(),true);
 console.log('PASS '+file+' busy retry, camera-named errors, pictures kept on classification failure, deferred classification, pinned preview, disconnect vs restart, labelled automatic pictures');
}})().catch(e=>{console.error(e);process.exitCode=1;});
