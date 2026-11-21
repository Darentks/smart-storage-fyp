const assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm');
(async()=>{
for(const file of ['index.html','VisionValidation_Module/camera_manager.html']){
 const html=fs.readFileSync(file,'utf8');
 const labels={Front:['000','010','011','100','110','111'].map(x=>'Front_'+x),Back:['000','100','X01','11X'].map(x=>'Back_'+x)};
 let mode='good';const urls=[],messages=[];const back={value:''},button={},front={value:''};
 const context={window:{},tmModelUrlInput:front,loadTmModelBtn:button,URL,location:{href:'http://localhost/'},
  document:{getElementById:()=>back},getBridgeUrl:()=>'/bridge',localStorage:{setItem(){}},showToast:(...v)=>messages.push(v)};
 context.window.tmImage={load:async url=>{
  urls.push(url);const role=url.includes('40mk-G4Oq')?'Back':'Front';
  if(mode==='offline' && role==='Back')throw Error('Back unavailable');
  return {getClassLabels:()=>mode==='combined'?[...labels.Front,...labels.Back]:labels[role]};
 }};
 context.tmImage=context.window.tmImage;
 vm.createContext(context);
 vm.runInContext(html.slice(html.indexOf('    const DEFAULT_TM_MODEL_URL'),html.indexOf('    // Low-light canvas enhancement')),context);
 await context.loadTeachableMachineModel();
 assert.ok(context.window.tmModel,JSON.stringify(messages));assert.notEqual(context.window.tmFrontModel,context.window.tmBackModel);
 assert.equal(urls.length,2);assert.ok(urls[0].includes('0cQO4TjOm'));assert.ok(urls[1].includes('40mk-G4Oq'));
 // A combined export or failed Back export never becomes a usable fallback.
 mode='combined';await context.loadTeachableMachineModel();assert.equal(context.window.tmModel,null);
 assert.match(messages.at(-1)[0],/combined models are rejected/);
 mode='offline';await context.loadTeachableMachineModel();assert.equal(context.window.tmModel,null);
 assert.equal(context.window.tmBackModel,null);
 // Offline tester is exercised with all I/O replaced by explicit forbidden-write traps.
 class Image {constructor(){this.style={};this.naturalWidth=640;}set src(v){this._src=v;this.onload?.();}get src(){return this._src;}}
 const node=()=>({style:{}}),toasts=[];
 Object.assign(context,{Image,homeTestBackDataUrl:'back-photo',homeTestFrontDataUrl:'front-photo',
  testBackFileInput:{files:[]},testFrontFileInput:{files:[]},backPreviewImg:new Image(),frontPreviewImg:new Image(),
  livePreviewImg:node(),backPlaceholder:node(),frontPlaceholder:node(),previewPlaceholder:node(),previewOverlayText:node(),
  homeTestModeBadge:node(),tmLivePredictionBanner:node(),backPredBadge:node(),frontPredBadge:node(),refShelfSelect:{value:'s'},
  stitchTwoImages:()=> 'stitched',showToast:(...v)=>toasts.push(v),
  fetch:()=>{throw Error('offline tester must not request a write');},bridgePost:()=>{throw Error('offline tester must not validate');}});
 context.document.getElementById=()=>({value:'off'});context.window.tmModel={};
 let predictions=0;
 context.predictDualCamerasWithTM=async(b,f)=>{
  predictions++;assert.equal(b.src,'back-photo');assert.equal(f.src,'front-photo');
  return {totalBoxes:3,backCount:1,frontCount:2,confidence:95,scenarioLabel:'candidate',degraded:false};
 };
 vm.runInContext(html.slice(html.indexOf('    async function runOfflinePhotoTestLocked('),html.indexOf('    const validatingShelves')),context);
 await context.runOfflinePhotoTestLocked('s','lease');assert.equal(predictions,1);assert.match(toasts.at(-1)[0],/Photo test only/);
 context.homeTestFrontDataUrl=null;await context.runOfflinePhotoTestLocked('s','lease');assert.equal(predictions,1);
 assert.match(toasts.at(-1)[0],/both/i);
 // Diagnostics must not label a tentative range as confirmed inventory.
 const element=()=>({style:{},children:[],append(...children){this.children.push(...children);},replaceChildren(){this.children=[];}});
 const host=element();context.document={getElementById:()=>host,createElement:element};
 context.compartmentsData=[{shelf_id:'s',current_quantity:2}];
 vm.runInContext(html.slice(html.indexOf('    function renderTMDiagnostics('),html.indexOf('    async function predictDualCamerasWithTM(')),context);
 context.renderTMDiagnostics(null,null,[{className:'Back_X01',probability:0.95}],
   [{className:'Front_011',probability:0.96}],'B1 unknown',{countRange:{min:2,max:3},m2Check:'PASS'});
 const allText=node=>[node.textContent||'',...node.children.map(allText)].join(' ');
 const text=allText(host);
 assert.match(text,/B1=unknown/);assert.match(text,/Candidate total \(unverified\): 2\.\.3/);
 assert.match(text,/Confirmed inventory at prediction: 2/);assert.match(text,/does not authorize a database write/);
 assert.match(text,/not measured accuracy/);assert.ok(!text.includes('NaN'));
 console.log('PASS: '+file+' independent defaults/labels, no cross-model fallback, offline no-write safety and distinct camera photos');
}
})().catch(e=>{console.error(e);process.exitCode=1;});
