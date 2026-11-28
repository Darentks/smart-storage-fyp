// ROI calibration tools: Inset/Outset 5% undo each other, Undo this shelf restores one shelf, Reset all edits
// restores every shelf to how it was when calibration opened, and nothing is deleted from browser storage.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const grab=(src,head)=>{const i=src.indexOf(head);assert.ok(i>=0,head);let d=0;for(let k=src.indexOf(') {',i)+2;;k++){if(src[k]==='{')d++;if(src[k]==='}'&&--d===0)return src.slice(i,k+1);}};
(async()=>{for(const file of ['index.html','VisionValidation_Module/camera_manager.html']){
  const html=fs.readFileSync(file,'utf8');
  for(const [handler,label] of [['insetSelectedRoi()','Inset 5%'],['outsetSelectedRoi()','Outset 5%'],['undoSelectedRoi()','Undo this shelf'],['resetAllRoiEdits()','&#8634; Reset all edits']])
    assert.match(html,new RegExp(`onclick="${handler.replace(/[()]/g,'\\$&')}"[^>]*>\\s*${label}`),`${file}: ${label} button`);
  assert.doesNotMatch(html,/resetDefaultRois|Reset Defaults|Cancel drawing/,'old buttons replaced');
  const toasts=[];let shelf='SHELF_R1_C1',resets=0;
  const ctx={toasts,activeCctvId:'CCTV_01',activeCameraRole:'GRID_6_ROI',DEFAULT_COMPARTMENT_ROIS:{},
    document:{getElementById:id=>id==='cctvCalibShelfSelect'?{value:shelf}:{width:640,height:480}},
    showToast:(...a)=>toasts.push(a),initCompartmentTrackers(){},updateCalibCoordsText(){},resetRoiEditor(){resets++;},
    localStorage:{removeItem(){throw Error('Reset must not delete saved calibration');},setItem(){throw Error('Tools must not save');}}};
  vm.createContext(ctx);
  vm.runInContext(['    function validQuad(','    function roiVertices(','    function quadRoi(','    function selectedRoiId(','    function setEditedRoi(',
    '    function captureRoiSnapshot(','    function roiSnapshotForActiveCamera(','    function scaleSelectedRoi(','    function insetSelectedRoi(',
    '    function outsetSelectedRoi(','    function undoSelectedRoi(','    function resetAllRoiEdits('].map(h=>grab(html,h)).join('\n')+
    '\n    let roiCalibrationSnapshot = null;\n    var activeCctvRois;',ctx);
  const quad=(x1,y1,x2,y2)=>({vertices:[[x1,y1],[x2,y1],[x2,y2],[x1,y2]],coordinate_space:'normalized',x_min:x1,y_min:y1,x_max:x2,y_max:y2});
  const near=(a,b)=>a.every((p,i)=>p.every((v,j)=>Math.abs(v-b[i][j])<1e-9));
  vm.runInContext('activeCctvRois='+JSON.stringify({SHELF_R1_C1:quad(.2,.2,.4,.6),SHELF_R1_C2:quad(.5,.2,.7,.6),SHELF_R1_C3:quad(.6,.1,.99,.9)}),ctx);
  const rois=()=>JSON.parse(JSON.stringify(vm.runInContext('activeCctvRois',ctx)));  // plain copies compare across realms
  // Nothing to restore before calibration opens for this camera.
  ctx.undoSelectedRoi();assert.match(toasts.pop()[0],/Nothing to restore/);
  vm.runInContext('captureRoiSnapshot()',ctx);
  const original=JSON.parse(JSON.stringify(rois()));
  // Inset then Outset returns the same corners; each step changes the shape.
  ctx.insetSelectedRoi();assert.ok(!near(rois().SHELF_R1_C1.vertices,original.SHELF_R1_C1.vertices));
  ctx.outsetSelectedRoi();assert.ok(near(rois().SHELF_R1_C1.vertices,original.SHELF_R1_C1.vertices),'Outset undoes Inset');
  ctx.outsetSelectedRoi();const grown=rois().SHELF_R1_C1.vertices;
  assert.ok(grown[0][0]<.2&&grown[2][0]>.4,'Outset grows every side');
  // Outset near the frame edge clamps inside [0,1] and stays a valid region.
  shelf='SHELF_R1_C3';ctx.outsetSelectedRoi();
  assert.ok(rois().SHELF_R1_C3.vertices.flat().every(v=>v>=0&&v<=1));assert.equal(rois().SHELF_R1_C3.x_max,1);
  // Undo this shelf restores only the selected shelf.
  shelf='SHELF_R1_C2';ctx.insetSelectedRoi();
  shelf='SHELF_R1_C1';ctx.undoSelectedRoi();
  assert.deepEqual(rois().SHELF_R1_C1,original.SHELF_R1_C1);
  assert.notDeepEqual(rois().SHELF_R1_C2,original.SHELF_R1_C2,'other shelves keep their edits');
  assert.notDeepEqual(rois().SHELF_R1_C3,original.SHELF_R1_C3);
  // Reset all edits restores every shelf (without touching browser storage).
  ctx.resetAllRoiEdits();assert.deepEqual(rois(),original);
  assert.match(toasts.pop()[0],/Restored every ROI on CCTV_01/);
  // Undo and Reset also discard a half-drawn polygon.
  assert.ok(resets>=4);
  // A snapshot from another camera is never applied.
  ctx.activeCctvId='CCTV_02';const before=JSON.stringify(rois());
  ctx.resetAllRoiEdits();assert.equal(JSON.stringify(rois()),before);assert.match(toasts.pop()[0],/Nothing to restore/);
  // Opening calibration takes the snapshot; closing clears it.
  const toggle=grab(html,'    function toggleRoiCalibrationMode(');
  assert.match(toggle,/if \(isCalibratingRoi\) \{\s*captureRoiSnapshot\(\);/);assert.match(toggle,/else roiCalibrationSnapshot = null;/);
  assert.match(html,/activeCctvRois = loadStoredRois\(\);\s*if \(isCalibratingRoi\) captureRoiSnapshot\(\);/,'camera switch while calibrating re-snapshots');
  // Exit Calibration with unsaved edits asks "Are you sure you don't want to save?" with Yes / No.
  assert.match(html,/<dialog id="roiConfirmDialog"[\s\S]*?Are you sure you don't want to save\?[\s\S]*?value="no"[^>]*>No<\/button>[\s\S]*?value="yes">Yes<\/button>/);
  assert.match(grab(html,'    function onCalibrateRoiClicked('),/if \(isCalibratingRoi\) \{ exitRoiCalibration\(\); return; \}/);
  const dialog={open:false,returnValue:'',answer:'',listeners:[],shown:0,
    addEventListener(type,fn){this.listeners.push(fn);},showModal(){this.shown++;this.open=true;this.returnValue=this.answer;this.open=false;this.listeners.splice(0).forEach(fn=>fn());}};
  const exit={closed:0,isCalibratingRoi:true,roiDraft:[]};
  Object.assign(ctx,{toggleRoiCalibrationMode:on=>{exit.closed++;ctx.isCalibratingRoi=on;}});
  ctx.document.getElementById=id=>id==='roiConfirmDialog'?dialog:id==='cctvCalibShelfSelect'?{value:shelf}:{width:640,height:480};
  vm.runInContext(['    function stableRoiJson(','    function roiEditsPending(','    function confirmRoiDialog(','    function confirmDiscardRoiEdits(','    async function exitRoiCalibration('].map(h=>grab(html,h)).join('\n')+
    '\n    var roiDraft = [];',ctx);
  const reopen=()=>{ctx.activeCctvId='CCTV_01';ctx.isCalibratingRoi=true;vm.runInContext('roiDraft=[];captureRoiSnapshot()',ctx);};
  // No changes: exits without asking.
  reopen();await ctx.exitRoiCalibration();assert.equal(dialog.shown,0);assert.equal(ctx.isCalibratingRoi,false);
  // Edits + No (or Esc): stays in calibration, edits kept.
  for(const answer of ['no','']){
    reopen();shelf='SHELF_R1_C1';ctx.insetSelectedRoi();const edited=rois();dialog.answer=answer;const shown=dialog.shown;
    await ctx.exitRoiCalibration();assert.equal(dialog.shown,shown+1);assert.equal(ctx.isCalibratingRoi,true,`answer "${answer}" keeps calibrating`);
    assert.deepEqual(rois(),edited);
  }
  // Edits + Yes: saved regions restored, calibration closed.
  reopen();const saved=rois();ctx.outsetSelectedRoi();dialog.answer='yes';
  await ctx.exitRoiCalibration();assert.equal(ctx.isCalibratingRoi,false);assert.deepEqual(rois(),saved);
  // An unfinished outline also counts as unsaved; reordered keys alone do not.
  reopen();vm.runInContext('roiDraft=[[0.1,0.1]]',ctx);dialog.answer='no';const shownDraft=dialog.shown;
  await ctx.exitRoiCalibration();assert.equal(dialog.shown,shownDraft+1);assert.equal(ctx.isCalibratingRoi,true);
  reopen();vm.runInContext("activeCctvRois.SHELF_R1_C1=Object.fromEntries(Object.entries(activeCctvRois.SHELF_R1_C1).reverse())",ctx);
  const shownKeys=dialog.shown;await ctx.exitRoiCalibration();assert.equal(dialog.shown,shownKeys,'same regions in a different key order are not unsaved edits');
  // Delete region (one shelf) and Clear all regions (Yes/No popup) leave regions empty for redrawing.
  assert.match(html,/onclick="deleteSelectedRoi\(\)"[^>]*>Delete region<\/button>/);
  assert.match(html,/onclick="clearAllRois\(\)"[^>]*>\s*Clear all regions\s*<\/button>/);
  const tool={value:'edit'},title={textContent:''},detail={textContent:''};
  ctx.document.getElementById=id=>({roiConfirmDialog:dialog,cctvCalibShelfSelect:{value:shelf},cctvRoiTool:tool,roiConfirmTitle:title,roiConfirmDetail:detail})[id]||{width:640,height:480};
  vm.runInContext(['    function setRoiTool(','    function clearedRoi(','    function roiCleared(','    function chooseRoiRedrawTool(','    function deleteSelectedRoi(','    async function clearAllRois(','    function pointInRoi('].map(h=>grab(html,h)).join('\n')+
    "\n    var ALL_SHELF_IDS = ['SHELF_R1_C1','SHELF_R1_C2','SHELF_R1_C3'];",ctx);
  reopen();ctx.activeCameraRole='GRID_6_ROI';const kept=rois();shelf='SHELF_R1_C2';
  ctx.deleteSelectedRoi();
  assert.deepEqual(rois().SHELF_R1_C2,{vertices:[],coordinate_space:'normalized'},'deleted region is empty, not the default');
  assert.deepEqual(rois().SHELF_R1_C1,kept.SHELF_R1_C1);assert.deepEqual(rois().SHELF_R1_C3,kept.SHELF_R1_C3);
  assert.equal(tool.value,'rectangle','switches to Draw rectangle so the shelf can be redrawn');
  assert.equal(vm.runInContext("roiVertices(activeCctvRois.SHELF_R1_C2,640,480).length",ctx),0,'no points: never hit or drawn, Save stays blocked');
  assert.equal(vm.runInContext("pointInRoi({x:.6,y:.4},roiVertices(activeCctvRois.SHELF_R1_C2))",ctx),false);
  vm.runInContext("setEditedRoi([[.5,.2],[.7,.2],[.7,.6],[.5,.6]])",ctx);  // redraw
  assert.deepEqual(rois().SHELF_R1_C2.vertices,[[.5,.2],[.7,.2],[.7,.6],[.5,.6]]);
  ctx.deleteSelectedRoi();ctx.undoSelectedRoi();assert.deepEqual(rois().SHELF_R1_C2,kept.SHELF_R1_C2,'Undo this shelf brings a deleted region back');
  // Clear all: popup text; No/Esc change nothing; Yes clears every shelf on this camera.
  for(const answer of ['no','']){
    dialog.answer=answer;const before=rois();await ctx.clearAllRois();
    assert.equal(title.textContent,'Are you sure you want to clear all?');assert.match(detail.textContent,/Every region on CCTV_01 will be removed/);
    assert.deepEqual(rois(),before,`answer "${answer}" keeps every region`);
  }
  dialog.answer='yes';tool.value='edit';await ctx.clearAllRois();
  for(const id of ['SHELF_R1_C1','SHELF_R1_C2','SHELF_R1_C3'])assert.deepEqual(rois()[id],{vertices:[],coordinate_space:'normalized'});
  assert.equal(tool.value,'rectangle');
  ctx.resetAllRoiEdits();assert.deepEqual(rois(),kept,'Reset all edits restores the saved regions after Clear all');
  // Draw 4 corners is gone: the tool offers Edit / move and Draw rectangle only; a finished rectangle switches to Edit.
  const toolSelect=html.match(/<select id="cctvRoiTool"[\s\S]*?<\/select>/)[0];
  assert.deepEqual([...toolSelect.matchAll(/<option value="([^"]+)"/g)].map(m=>m[1]),['edit','rectangle']);
  assert.doesNotMatch(html,/'polygon'|Draw 4 corners|click 4 corners/);
  assert.match(grab(html,'    function initCanvasRoiEventListeners('),/if \(roiPointerEdit\?\.rectangle && roiPointerEdit\.drew\) setRoiTool\('edit', \{reset: false\}\);/);
  // Grouped toolbar: Tool / Resize / Swap with / Undo / Remove, one primary Save, tool toggles keep their state in sync.
  const bar=html.slice(html.indexOf('<div id="cctvCalibrationToolbar"'),html.indexOf('<div id="cctvConsensusResult"'));
  assert.deepEqual([...bar.matchAll(/class="roi-group-label"[^>]*>([^<]+)</g)].map(m=>m[1]),['Tool','Resize','Swap with','Undo','Remove']);
  assert.match(bar,/class="btn-action roi-save" onclick="saveCalibratedRois\(\)"/);
  for(const id of ['cctvCalibShelfSelect','cctvRoiTool','cctvRoiSwapControls','cctvRoiSwapSelect','cctvRoiSwapBtn','cctvCalibCoordsText'])assert.ok(bar.includes(`id="${id}"`),id);
  const buttons=['edit','rectangle'].map(roiTool=>({dataset:{roiTool},classes:new Set(),attrs:{},classList:{toggle(c,on){on?this.owner.classes.add(c):this.owner.classes.delete(c);}},setAttribute(k,v){this.attrs[k]=v;}}));
  buttons.forEach(b=>b.classList.owner=b);
  ctx.document.querySelectorAll=sel=>sel==='[data-roi-tool]'?buttons:[];
  ctx.setRoiTool('rectangle');assert.equal(tool.value,'rectangle');
  assert.deepEqual(buttons.map(b=>[b.attrs['aria-pressed'],b.classes.has('is-active')]),[['false',false],['true',true]]);
  const resetsBefore=resets;ctx.setRoiTool('edit',{reset:false});assert.equal(resets,resetsBefore,'programmatic switch keeps the edit in progress');
  assert.deepEqual(buttons.map(b=>b.attrs['aria-pressed']),['true','false']);
  ctx.setRoiTool('rectangle');assert.equal(resets,resetsBefore+1,'clicking a different tool discards an unfinished drag');
  console.log(`PASS ${file} Inset/Outset 5% undo each other and clamp to the frame; Undo this shelf and Reset all edits restore the opening snapshot without deleting saved calibration; Exit asks Yes/No only when edits are unsaved; Delete region and Clear all (Yes/No) leave regions empty for redrawing`);
}})().catch(error=>{console.error(error);process.exitCode=1;});
