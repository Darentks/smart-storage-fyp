const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
(async () => {
  for (const file of ['index.html', 'VisionValidation_Module/camera_manager.html']) {
    const html = fs.readFileSync(file, 'utf8');
    const labels = {Front: ['000','010','011','100','110','111'].map(s=>'Front_'+s), Back: ['000','100','X01','11X'].map(s=>'Back_'+s)};
    let frontClass, backClass, score=0.95;
    const model = role => ({getClassLabels:()=>labels[role], predict:async image=>{if(image.role)assert.equal(image.role,role);return labels[role].map(className=>({className, probability:className===(role==='Front'?frontClass:backClass)?score:0.01}));}});
    class Canvas {constructor(){this.width=this.height=224;}}
    const context = {window:{tmFrontModel:model('Front'), tmBackModel:model('Back'), dualCamSettings:{confidence:0.80}}, DUAL_CAM_LABELS:labels,
      HTMLCanvasElement:Canvas, HTMLImageElement:class {}, document:{getElementById:()=>null}, preprocessForTM:source=>Object.assign(new Canvas(),{role:source.role}), renderTMDiagnostics:()=>{}, console};
    vm.createContext(context);
    const guard = html.slice(html.indexOf('    function requireDualCamModel('), html.indexOf('    async function loadTeachableMachineModel('));
    const predict = html.slice(html.indexOf('    async function predictDualCamerasWithTM('), html.indexOf('    function dualCamEvidence('));
    vm.runInContext(guard+predict+'\nglobalThis.run = predictDualCamerasWithTM;',context);
    frontClass='Front_111';backClass='Back_11X';
    assert.equal((await context.run({role:'Back'},{role:'Front'})).totalBoxes,5);
    score=0.80;
    assert.equal((await context.run({},{})).status,'VERIFIED_PAIR');
    score=0.7999;
    assert.equal((await context.run({},{})).status,'VALIDATION_PENDING');
    score=0.95;
    let accepted=0, conflicts=0, ambiguous=0;
    for (const front of labels.Front) for (const back of labels.Back) {
      frontClass=front; backClass=back;
      const result = await context.run({},{});
      const frontBits=[...front.slice(6)].map(Number), backBits=[...back.slice(5,7)].map(Number);
      if(back !== 'Back_11X' && Number(back.slice(-1)) !== frontBits[2]) {
        assert.equal(result.status,'VALIDATION_PENDING');
        assert.equal(result.m2Check,'CONFLICT');
        assert.equal(result.totalBoxes,null);
        conflicts++;
      } else if(back === 'Back_X01') {
        assert.equal(result.status,'INDETERMINATE_B1');
        assert.equal(result.slots.B1,null);assert.equal(result.totalBoxes,null);assert.equal(result.backCount,null);
        assert.equal(result.countRange.min,frontBits.reduce((a,b)=>a+b,0));
        assert.equal(result.countRange.max,result.countRange.min+1);assert.equal(result.m2Check,'PASS');
        assert.equal(result.degraded,true);ambiguous++;
      } else {
        assert.equal(result.status,'VERIFIED_PAIR');
        assert.equal(result.totalBoxes,[...frontBits,...backBits].reduce((n,b)=>n+b,0));
        assert.equal(result.frontCount+result.backCount,result.totalBoxes);
        assert.equal(result.m2Check,back === 'Back_11X'?'SKIPPED_OCCLUDED':'PASS');
        assert.equal(result.observedM2,back === 'Back_11X'?null:frontBits[2]);
        accepted++;
      }
    }
    assert.equal(accepted,14); assert.equal(conflicts,8);assert.equal(ambiguous,2);
    context.window.dualCamSettings.confidence=0.99;
    assert.equal((await context.run({},{})).status,'VALIDATION_PENDING');
    context.window.dualCamSettings.confidence=0.80;
    assert.equal((await context.run(null,{})).totalBoxes,null);
    frontClass='Front_011';backClass='Back_X01';
    context.window.dualCamSettings.confidence=0.99;
    const low=await context.run({},{});
    assert.equal(low.status,'VALIDATION_PENDING');assert.equal(low.countRange,undefined);
    context.window.dualCamSettings.confidence=0.80;
    const combined=[...labels.Front,...labels.Back];
    const combinedModel=role=>({getClassLabels:()=>combined,predict:async()=>combined.map(className=>({className,
      probability:className===(role==='Front'?frontClass:backClass)?0.95:0.001}))});
    context.window.tmFrontModel=combinedModel('Front');context.window.tmBackModel=combinedModel('Back');
    assert.equal((await context.run({},{})).status,'VALIDATION_PENDING');
    context.window.tmFrontModel=model('Front');context.window.tmBackModel=model('Back');
    const unknown=await context.run({},{});
    assert.equal(unknown.status,'INDETERMINATE_B1');assert.match(unknown.scenarioLabel,/2\.\.3/);
    frontClass='Front_110';backClass='Back_100';
    assert.equal((await context.run({},{})).totalBoxes,3);
    context.window.tmBackModel.getClassLabels=()=>['Back_000','Back_001','Back_100','Back_101','Back_11X'];
    assert.equal((await context.run({},{})).status,'VALIDATION_PENDING');
    context.window.tmBackModel.getClassLabels=()=>['Back_000','Back_001','Back_100','Back_101','Back_110','Back_111'];
    assert.equal((await context.run({},{})).status,'VALIDATION_PENDING');
    console.log('PASS: '+file+' 14 exact pairs, 2 ambiguous pairs, 8 conflicts, occlusion, confidence, missing camera and legacy-model rejection');
  }
})().catch(error=>{console.error(error); process.exitCode=1;});
