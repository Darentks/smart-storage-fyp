'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const {createCanvas}=require('@napi-rs/canvas');
const {Inference,LABELS,checkLabels,pair,rgb,tmFrame,poseFrame,poseLandmarks,verifiedFile,sha}=require('./inference.cjs');
const {difference,compare}=require('./parity.cjs');
const probabilities=(role,chosen,confidence=.99)=>LABELS[role].map(className=>({className,probability:className===chosen?confidence:(1-confidence)/(LABELS[role].length-1)}));
async function main(){
  assert.throws(()=>checkLabels('Front',[...LABELS.Front,...LABELS.Back]),/MISMATCH/);
  assert.throws(()=>checkLabels('Back',Array(4).fill('Back_000')),/MISMATCH/);
  let exact=0,unknown=0,conflict=0;
  for(const f of LABELS.Front)for(const b of LABELS.Back){
    const result=pair(probabilities('Front',f),probabilities('Back',b));
    assert.equal(result.authorizes_write,false);
    if(result.hold==='UNKNOWN_B1'){unknown++;assert.equal(result.slots[3],null);assert.equal(result.count_range[1]-result.count_range[0],1);}
    else if(result.hold==='M2_CONFLICT')conflict++;
    else {exact++;assert.equal(result.slots.reduce((a,b)=>a+b,0),result.total);}
  }
  assert.deepEqual([exact,unknown,conflict],[14,2,8]);
  assert.equal(pair(probabilities('Front','Front_111',.79999),probabilities('Back','Back_11X')).hold,'LOW_CONFIDENCE');
  assert.equal(pair(probabilities('Front','Front_111',.8),probabilities('Back','Back_11X')).hold,null);
  // Compare the production browser algorithm with the native port using one canvas
  // implementation. This checks algorithm translation, NOT Chrome/Skia parity.
  const html=fs.readFileSync('index.html','utf8');const sample=createCanvas(640,480),ctx=sample.getContext('2d');
  const gradient=ctx.createLinearGradient(0,0,640,480);gradient.addColorStop(0,'#050a14');gradient.addColorStop(1,'#dd9752');ctx.fillStyle=gradient;ctx.fillRect(0,0,640,480);
  const context={document:{createElement:()=>createCanvas(1,1)},HTMLCanvasElement:sample.constructor,console};
  vm.createContext(context);vm.runInContext(html.slice(html.indexOf('    function enhanceCanvasLowLight('),html.indexOf('    function renderTMDiagnostics(')),context);
  for(const boost of ['off','auto','high'])assert.deepEqual(rgb(context.preprocessForTM(sample,boost)),rgb(tmFrame(sample,boost)));
  const frame=poseFrame(sample);assert.deepEqual([frame.newW,frame.newH,frame.padX,frame.padY],[320,240,0,40]);
  assert.equal(frame.input[0],Math.fround(114/255));
}
// Run numeric checks separately so expected float32 precision is explicit.
async function run(){
  await main();
  assert.throws(()=>poseLandmarks(new Float32Array(1),[1,6,1],{}),/MISMATCH/);
  const detections=new Float32Array(56*3);
  for(let a=0;a<3;a++){
    detections[a]=160;detections[3+a]=160;detections[6+a]=80;detections[9+a]=80;detections[12+a]=[.9,.8,.45][a];
    for(let k=0;k<17;k++){detections[(5+k*3)*3+a]=160;detections[(6+k*3)*3+a]=160;detections[(7+k*3)*3+a]=.75;}
  }
  const persons=poseLandmarks(detections,[1,56,3],{newW:320,newH:240,padX:0,padY:40});
  assert.equal(persons.length,1);assert.equal(persons[0].landmarks.length,33);
  assert.deepEqual(persons[0].landmarks[15],{x:.5,y:.5,visibility:.75});
  assert.equal(persons[0].landmarks[1].visibility,0);
  const temp=fs.mkdtempSync(require('node:path').join(require('node:os').tmpdir(),'edge-hash-'));
  try{
    fs.writeFileSync(require('node:path').join(temp,'asset'),Buffer.from('trusted'));
    assert.equal(verifiedFile(temp,{path:'asset',sha256:sha(Buffer.from('trusted'))}).toString(),'trusted');
    assert.throws(()=>verifiedFile(temp,{path:'asset',sha256:'0'.repeat(64)}),/HASH_MISMATCH/);
    assert.throws(()=>verifiedFile(temp,{path:'../outside',sha256:'0'.repeat(64)}),/MANIFEST/);
  }finally{fs.unlinkSync(require('node:path').join(temp,'asset'));fs.rmdirSync(temp);}
  assert.deepEqual(difference([1,2],[1,2]),{max:0,mean:0});
  assert.throws(()=>difference([1],[NaN]),/NONFINITE/);
  if(process.argv[2]){
    const runtime=new Inference();await runtime.init(process.argv[2]);
    const canvas=createCanvas(640,480);canvas.getContext('2d').fillRect(0,0,640,480);
    const image=canvas.toDataURL('image/png');
    const before=(await runtime.infer({kind:'pair',image_back:image,image_front:image,boost:'off'})).metrics.tf_tensors;
    for(let i=0;i<3;i++){
      const result=await runtime.infer({kind:'pair',image_back:image,image_front:image,boost:'auto'});
      assert.equal(result.metrics.tf_tensors,before);assert.equal(result.authorizes_write,false);
    }
    const pose=await runtime.infer({kind:'pose',image,include_pixels:true});assert.equal(pose.dims[1],56);
    const pairResult=await runtime.infer({kind:'pair',image_back:image,image_front:image,boost:'off',include_pixels:true});
    const fixture={version:1,models:runtime.versions,samples:[{kind:'pair',image_back:image,image_front:image,boost:'off',front:pairResult.front,back:pairResult.back}]};
    assert.equal((await compare(runtime,fixture)).pass,true);
    fixture.samples[0].front.predictions[0].probability+=.01;
    assert.equal((await compare(runtime,fixture)).pass,false);
    fixture.models={...fixture.models,Front:{weights_sha256:'wrong'}};
    await assert.rejects(compare(runtime,fixture),/MODEL_MISMATCH/);
    const normalization=new Inference();let observed;
    normalization.models={Front:{labels:LABELS.Front,model:{predict:t=>{observed=Array.from(t.dataSync()).slice(0,3);return require('@tensorflow/tfjs').tensor2d([[1,0,0,0,0,0]]);}}}};
    const white=createCanvas(224,224);white.getContext('2d').fillStyle='#ffffff';white.getContext('2d').fillRect(0,0,224,224);
    await normalization.tm('Front',white.toDataURL('image/png'));
    assert.equal(observed[0],Math.fround(Math.fround(255/127)-1));
  }
  console.log('PASS native labels, all 24 pairs, raw 80% floor, gamma translation, geometry, parity mismatch rejection'+(process.argv[2]?', real exported models and tensor stability':''));
}
run().catch(error=>{console.error(error);process.exitCode=1;});
