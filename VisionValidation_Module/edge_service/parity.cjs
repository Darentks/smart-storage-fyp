'use strict';
const fs=require('node:fs');
const {Inference}=require('./inference.cjs');
function difference(expected,actual) {
  if(expected.length!==actual.length || !expected.length) throw Error('PARITY_SHAPE_MISMATCH');
  let max=0,total=0;
  for(let i=0;i<expected.length;i++) {
    if(!Number.isFinite(expected[i]) || !Number.isFinite(actual[i])) throw Error('PARITY_NONFINITE');
    const delta=Math.abs(expected[i]-actual[i]);max=Math.max(max,delta);total+=delta;
  }
  return {max,mean:total/expected.length};
}
async function compare(runtime,fixture) {
  if(fixture.version!==1 || !Array.isArray(fixture.samples) || !fixture.samples.length || fixture.samples.length>100)
    throw Error('INVALID_PARITY_FIXTURE');
  const cases=[];
  for(const sample of fixture.samples) {
    if(sample.kind==='pair') {
      for(const role of ['Front','Back']) if(fixture.models[role].weights_sha256!==runtime.versions[role].weights_sha256)
        throw Error('PARITY_MODEL_MISMATCH');
      const actual=await runtime.infer({...sample,include_pixels:true});let pass=true;const details={};
      for(const role of ['front','back']) {
        const golden=sample[role];const result=actual[role];
        const pixels=difference(Buffer.from(golden.rgb_base64,'base64'),Buffer.from(result.rgb_base64,'base64'));
        const expected=golden.predictions,observed=result.predictions;
        if(String(expected.map(x=>x.className))!==String(observed.map(x=>x.className))) throw Error('PARITY_LABEL_ORDER_MISMATCH');
        const probabilities=difference(expected.map(x=>x.probability),observed.map(x=>x.probability));
        const top=items=>items.reduce((a,b)=>a.probability>b.probability?a:b);
        const a=top(expected),b=top(observed);
        const decision=a.className===b.className && (a.probability>=.8)===(b.probability>=.8);
        pass=pass && pixels.max===0 && probabilities.max<=.001 && decision;
        details[role]={pixels,probabilities,decision};
      }
      cases.push({kind:'pair',boost:sample.boost,pass,details,metrics:actual.metrics});
    } else if(sample.kind==='pose') {
      if(fixture.models.pose.asset_sha256!==runtime.versions.pose.asset_sha256) throw Error('PARITY_MODEL_MISMATCH');
      const actual=await runtime.infer({...sample,include_pixels:true});
      const pixels=difference(Buffer.from(sample.rgb_base64,'base64'),Buffer.from(actual.rgb_base64,'base64'));
      const raw=Buffer.from(sample.output_base64,'base64');
      if(raw.length%4)throw Error('PARITY_SHAPE_MISMATCH');
      const probabilities=difference(new Float32Array(raw.buffer.slice(raw.byteOffset,raw.byteOffset+raw.byteLength)),actual.output);
      const dimensions=String(sample.dims)===String(actual.dims);
      cases.push({kind:'pose',pass:pixels.max===0 && probabilities.max<=.001 && dimensions,pixels,probabilities,metrics:actual.metrics});
    } else throw Error('INVALID_PARITY_KIND');
  }
  return {version:1,created_at:new Date().toISOString(),pass:cases.every(c=>c.pass),
    scope:'inference only; does not certify action tracking or authorize service cutover',models:runtime.versions,cases};
}
async function main() {
  const runtime=new Inference();await runtime.init(process.argv[2]);
  const report=await compare(runtime,JSON.parse(fs.readFileSync(process.argv[3],'utf8')));
  fs.writeFileSync(process.argv[4]||'.runtime/edge-parity-report.json',JSON.stringify(report,null,2));
  console.log(JSON.stringify({pass:report.pass,cases:report.cases.length}));
  if(!report.pass)process.exitCode=1;
}
if(require.main===module)main().catch(error=>{console.error(error.message);process.exitCode=1;});
module.exports={difference,compare};
