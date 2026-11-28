const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
for(const file of ['index.html','VisionValidation_Module/camera_manager.html']){
 const source=fs.readFileSync(file,'utf8'),elements={},saved=[],toasts=[];let blocked=false,resets=0;
 const ctx={isCalibratingRoi:true,isPoseRunning:true,bridgeStatus:{authoritative_processor:'none'},
  document:{getElementById:id=>elements[id] ||= {style:{},width:640,height:480}},
  resetRoiEditor(){},captureRoiSnapshot(){},roiCalibrationSnapshot:null,initCompartmentTrackers(){resets++;},updateCalibCoordsText(){},showToast:(...x)=>toasts.push(x),
  roiDraft:[],activeCameraRole:'GRID_6_ROI',ALL_SHELF_IDS:['SHELF_R1_C1'],activeCctvRois:{SHELF_R1_C1:{}},
  validQuad:()=>true,roiVertices:()=>[],activeCctvId:'CCTV_01',activeCctvSourceUrl:'source',
  cctvCamerasList:[{cctv_id:'CCTV_01',rtsp_url:'source',ip_address:'192.0.2.1'}],
  validateCctvBinding:()=> 'binding',cctvSourceBinding:()=> 'binding',cameraRole:()=> 'GRID_6_ROI',roiStorageKey:()=> 'roi',
  localStorage:{setItem(k,v){if(blocked)throw Error('storage blocked');saved.push([k,v]);}}};
 vm.createContext(ctx);
 vm.runInContext(source.slice(source.indexOf('    function updateCctvTrackingHelp()'),source.indexOf('    function onPoseResults(')),ctx);
 vm.runInContext(source.slice(source.indexOf('    function toggleRoiCalibrationMode('),source.indexOf('    function onCalibShelfChanged()')),ctx);
 vm.runInContext(source.slice(source.indexOf('    function saveCalibratedRois()'),source.indexOf('    function resetAllRoiEdits()')),ctx);
 ctx.updateCctvTrackingHelp();assert.match(elements.cctvTrackingHelp.textContent,/Calibration active/);
 ctx.saveCalibratedRois();assert.equal(saved.length,2);assert.equal(ctx.isCalibratingRoi,false);assert.equal(resets,1);assert.equal(elements.cctvCalibrationToolbar.style.display,'none');assert.match(elements.cctvTrackingHelp.textContent,/Diagnostic preview only/);
 ctx.toggleRoiCalibrationMode(true);blocked=true;ctx.saveCalibratedRois();assert.equal(ctx.isCalibratingRoi,true);assert.match(toasts.at(-1)[0],/Failed to save/);
 blocked=false;ctx.roiDraft=[1];ctx.saveCalibratedRois();assert.equal(ctx.isCalibratingRoi,true);assert.match(toasts.at(-1)[0],/Finish or cancel/);
 ctx.roiDraft=[];ctx.validQuad=()=>false;ctx.saveCalibratedRois();assert.equal(ctx.isCalibratingRoi,true);assert.match(toasts.at(-1)[0],/Invalid ROI/);
 assert.match(source,/tracker\.ambiguous \|\| isCalibratingRoi/,'calibration must continue to cancel dwell');
 console.log('PASS '+file+' successful save exits calibration/reset dwell; failed save preserves editor; explicit diagnostic-only help');
}
