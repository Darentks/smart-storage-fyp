// Execute the production browser action function with drawing/UI replaced by no-ops.
const fs=require('node:fs'),vm=require('node:vm');
const scenarios=JSON.parse(fs.readFileSync(0,'utf8'));
const html=fs.readFileSync('index.html','utf8');
const all=[];
for(const scenario of scenarios){
  let now=0;const votes=[];
  const noop=new Proxy({},{get:(_,key)=>key==='createLinearGradient'?()=>({addColorStop(){}}):()=>{},set:()=>true});
  const context={window:{},performance:{now:()=>now},ALL_SHELF_IDS:Object.keys(scenario.rois),DEFAULT_COMPARTMENT_ROIS:scenario.rois,
    NOOP_CANVAS_CONTEXT:noop,threeCameraDashboard:{background:true,vote:(_,shelf,action,duration,confidence)=>votes.push({at:now/1000,shelf_id:shelf,expected_action:action,dwell_duration_seconds:duration,oks_confidence:confidence})},
    drawBasicSkeleton(){},drawCompartmentRoiBox(){},validQuad:p=>p.length===4,
    roiVertices:r=>Array.isArray(r)?r:r.vertices||[[r.x_min,r.y_min],[r.x_max,r.y_min],[r.x_max,r.y_max],[r.x_min,r.y_max]]};
  vm.createContext(context);
  for(const [start,end] of [['    function classifyTrajectoryAction(', '    let isYoloInferencing'],['    function pointInRoi(', '    function quadRoi('],['    function cancelAmbiguousDwell(', '    const windowUnusedRole'],['    function onPoseResults(', '    function drawCompartmentRoiBox(']])
    vm.runInContext(html.slice(html.indexOf(start),html.indexOf(end)),context);
  const camera={role:scenario.role,rois:scenario.rois,canvas:{width:640,height:480},trackers:{}};
  for(const shelf of Object.keys(scenario.rois))camera.trackers[shelf]={state:'IDLE',dwellStartTime:null,lastSeenTime:null,currentDwellDuration:0,dwellThreshold:1.2,dropGracePeriod:.3,trajectoryHistory:[],triggerFired:false,lastConfirmedAction:'BORROW',lastConfirmedConf:0,cooldownUntil:0};
  for(const frame of scenario.frames){
    now=frame.at*1000;
    const persons=frame.persons.map(p=>{const points=p.landmarks;points.personBox=p.personBox;return points;});
    context.onPoseResults({multiPoseLandmarks:persons},camera);
  }
  all.push(votes);
}
process.stdout.write(JSON.stringify(all));
