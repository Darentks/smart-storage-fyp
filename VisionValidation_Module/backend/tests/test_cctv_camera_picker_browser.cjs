// Toolbar Camera list: only CCTV_01..03, binds through useCctvAsTrackerSource, toolbar Edit/Calibrate act on the
// picked camera, camera cards and recorded-clip replay are gone.
const assert=require('assert'),fs=require('fs'),path=require('path'),vm=require('vm');
const root=path.resolve(__dirname,'../../..');
(async()=>{for(const file of ['index.html','VisionValidation_Module/camera_manager.html']){
  const s=fs.readFileSync(path.join(root,file),'utf8');
  const picker=s.match(/<select id="cctvCameraPicker"[\s\S]*?<\/select>/);
  assert.ok(picker,`${file}: Camera list missing`);
  assert.deepStrictEqual([...picker[0].matchAll(/<option value="([^"]*)"/g)].map(m=>m[1]),['','CCTV_01','CCTV_02','CCTV_03','ALL'],`${file}: Camera list must hold the three CCTVs plus the all-cameras test`);
  assert.ok(!/runSimulatedHandInteraction|runContinuousSimulationLoop|'simulation'|getUserMedia/.test(s),`${file}: webcam/simulation sources must be removed`);
  assert.ok(!/cctvPoseSourceSelect|video_file|cctvReplay|isRecordedCctvTest|setCctvReplayMode|Diagnostics \(not inventory evidence\)/.test(s),`${file}: recorded-clip replay and Diagnostics must be removed`);
  assert.ok(/id="cctvEditSelectedBtn"[^>]*onclick="editSelectedCctvCamera\(\)"/.test(s),`${file}: toolbar Edit button missing`);
  assert.ok(s.includes('id="cctvCalibrateRoiBtn"')&&s.includes('onclick="onCalibrateRoiClicked()"'),`${file}: toolbar Calibrate must bind the picked camera`);
  assert.ok(!/cctvCamerasListGrid|startCctvPipeline|editCctvCamera\('\$\{/.test(s),`${file}: camera cards must be removed`);

  const code=s.slice(s.indexOf('    // Toolbar Camera list'),s.indexOf('    // RTSP polling uses the camera picked'));
  const opts=['','CCTV_01','CCTV_02','CCTV_03','ALL'].map(value=>({value,disabled:false,textContent:value}));
  const pickerEl={options:opts,value:'',scrollIntoView(){},focus(){this.focused=true;}},editBtn={disabled:true};
  const calls=[],toasts=[];let bindOk=true;
  const ctx={cctvCamerasList:[{cctv_id:'CCTV_01',camera_name:'Left',rtsp_url:'rtsp://a'},{cctv_id:'CCTV_02',rtsp_url:'rtsp://b',is_active:false}],
    cctvLoadError:false,cctvConfigLoaded:true,isCalibratingRoi:false,
    activeCctvId:'CCTV_01',activeCctvSourceUrl:null,showToast:(...a)=>toasts.push(a),
    useCctvAsTrackerSource:(url,id)=>{calls.push(['bind',url,id]);if(!bindOk)return false;ctx.activeCctvId=id;ctx.activeCctvSourceUrl=url;return true;},
    editCctvCamera:encoded=>calls.push(['edit',JSON.parse(decodeURIComponent(encoded)).cctv_id]),
    calibrateCctvRoi:(id,url)=>calls.push(['calibrate',id,url]),
    toggleRoiCalibrationMode:on=>calls.push(['calibration',on]),
    exitRoiCalibration:()=>calls.push(['exit']),
    document:{getElementById:id=>els[id]}};
  const single={hidden:false},testView={hidden:true},poseBtn={style:{display:''}},calBtn={style:{display:''}},runs=[];let dashStops=0,renders=0,intervals=0;
  const els={cctvCameraPicker:pickerEl,cctvEditSelectedBtn:editBtn,cctvSingleView:single,cctvConsensusTest:testView,cctvPoseToggleBtn:poseBtn,cctvCalibrateRoiBtn:calBtn};
  Object.assign(ctx,{DEFAULT_COMPARTMENT_ROIS:{SHELF_R1_C1:{}},isPoseRunning:false,stopPoseTracker(){calls.push(['stopPose']);},diagnosticConsensusTimer:null,
    setInterval:()=>++intervals,clearInterval(){},renderDiagnosticConsensus:()=>renders++,localStorage:{getItem:()=>null},cctvSourceBinding:x=>x,cameraRole:c=>c.camera_role,
    threeCameraDashboard:{running:false,diagnosticOnly:false,async start(cameras,defaults,options){runs.push(options);this.running=true;this.diagnosticOnly=options.diagnosticOnly;},stop(){dashStops++;this.running=false;this.diagnosticOnly=false;}}});
  vm.createContext(ctx);vm.runInContext(code,ctx);
  ctx.syncCctvCameraPicker();
  assert.deepStrictEqual(opts.slice(1,4).map(o=>[o.textContent,o.disabled]),[['CCTV_01 · Left',false],['CCTV_02 (inactive)',false],['CCTV_03 (not registered)',true]]);
  assert.strictEqual(opts[0].textContent,'Choose a camera…');
  assert.strictEqual(opts[4].textContent,'All 3 cameras · consensus test (needs CCTV_03)',`${file}: All option must not be treated as a camera ID`);
  assert.strictEqual(opts[4].disabled,true);
  assert.strictEqual(pickerEl.value,'',`${file}: no camera selected before binding`);
  assert.strictEqual(editBtn.disabled,true,`${file}: Edit disabled until a camera is picked`);
  ctx.editSelectedCctvCamera();assert.match(toasts.pop()[0],/Choose a single camera/);assert.ok(pickerEl.focused);
  ctx.onCalibrateRoiClicked();assert.match(toasts.pop()[0],/Choose a single camera/);assert.strictEqual(calls.length,0);

  ctx.onCctvCameraPicked('CCTV_02');
  assert.deepStrictEqual(calls.pop(),['bind','rtsp://b','CCTV_02']);
  ctx.syncCctvCameraPicker();assert.strictEqual(pickerEl.value,'CCTV_02');assert.strictEqual(editBtn.disabled,false);
  ctx.editSelectedCctvCamera();assert.deepStrictEqual(calls.pop(),['edit','CCTV_02']);
  ctx.onCalibrateRoiClicked();assert.deepStrictEqual(calls.pop(),['calibrate','CCTV_02','rtsp://b']);
  ctx.isCalibratingRoi=true;ctx.onCalibrateRoiClicked();assert.deepStrictEqual(calls.pop(),['exit']);ctx.isCalibratingRoi=false;

  // A failed binding keeps the pick so its registration can still be edited and fixed.
  bindOk=false;ctx.onCctvCameraPicked('CCTV_01');calls.pop();
  assert.strictEqual(pickerEl.value,'CCTV_01');assert.strictEqual(editBtn.disabled,false);
  ctx.editSelectedCctvCamera();assert.deepStrictEqual(calls.pop(),['edit','CCTV_01']);bindOk=true;

  ctx.onCctvCameraPicked('CCTV_03');assert.match(toasts.pop()[0],/not registered/);assert.strictEqual(calls.length,0);
  ctx.cctvCamerasList=[];ctx.cctvLoadError=true;ctx.syncCctvCameraPicker();
  assert.match(opts[0].textContent,/unavailable/);assert.strictEqual(pickerEl.value,'');assert.strictEqual(editBtn.disabled,true);
  ctx.cctvLoadError=false;ctx.syncCctvCameraPicker();assert.match(opts[0].textContent,/No CCTV cameras registered/);
  // All 3 cameras: diagnostic-only runner, single view hidden, Edit/Calibrate unavailable; picking one camera ends it.
  ctx.cctvCamerasList=[{cctv_id:'CCTV_01',rtsp_url:'rtsp://a',camera_role:'GRID_6_ROI'},{cctv_id:'CCTV_02',rtsp_url:'rtsp://b',camera_role:'GRID_6_ROI'},{cctv_id:'CCTV_03',rtsp_url:'rtsp://c',camera_role:'SIDE_PRESENCE'}];
  ctx.cctvLoadError=false;ctx.syncCctvCameraPicker();
  assert.strictEqual(opts[4].textContent,'All 3 cameras · consensus test');assert.strictEqual(opts[4].disabled,false);
  ctx.activeCctvSourceUrl='rtsp://a';ctx.activeCctvId='CCTV_01';ctx.isPoseRunning=true;
  await ctx.onCctvCameraPicked('ALL');
  assert.ok(runs.length===1&&runs[0].diagnosticOnly===true,'consensus test must use the diagnostic-only runner');
  assert.deepStrictEqual(calls.pop(),['stopPose']);
  assert.strictEqual(single.hidden,true);assert.strictEqual(testView.hidden,false);assert.strictEqual(poseBtn.style.display,'none');assert.strictEqual(calBtn.style.display,'none');
  assert.strictEqual(pickerEl.value,'ALL');assert.strictEqual(editBtn.disabled,true);assert.ok(intervals>0&&renders>0,'status refreshes while running');
  assert.match(toasts.pop()[0],/default regions/);
  ctx.editSelectedCctvCamera();assert.match(toasts.pop()[0],/single camera/);ctx.onCalibrateRoiClicked();assert.match(toasts.pop()[0],/single camera/);
  ctx.isPoseRunning=false;ctx.onCctvCameraPicked('CCTV_02');
  assert.ok(dashStops>=1);assert.strictEqual(single.hidden,false);assert.strictEqual(testView.hidden,true);assert.strictEqual(poseBtn.style.display,'');
  assert.deepStrictEqual(calls.pop(),['bind','rtsp://b','CCTV_02']);
  ctx.threeCameraDashboard.start=async()=>{throw Error('Configure and enable CCTV_03 first');};
  await ctx.onCctvCameraPicked('ALL');assert.match(toasts.pop()[0],/could not start: Configure and enable CCTV_03/);
  ctx.stopConsensusTest();assert.strictEqual(single.hidden,false);assert.notStrictEqual(pickerEl.value,'ALL');
  console.log(`PASS ${file} Camera list holds CCTV_01-03 plus the all-cameras consensus test (diagnostic-only); toolbar Edit/Calibrate act on the picked camera; cards and replay removed`);
}})().catch(error=>{console.error(error);process.exitCode=1;});
