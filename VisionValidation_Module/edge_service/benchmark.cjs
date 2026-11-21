'use strict';
// Saved frames only. No camera, MQTT or inventory side effects.
const fs=require('node:fs');const os=require('node:os');
const {Inference}=require('./inference.cjs');
(async()=>{
  const runtime=new Inference(),started=performance.now();await runtime.init(process.argv[2]);
  const load_ms=performance.now()-started;
  const fixture=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));
  if(fixture.version!==1 || !Array.isArray(fixture.samples) || !fixture.samples.length || fixture.samples.length>100)throw Error('INVALID_FIXTURE');
  const metrics=[];
  for(let round=0;round<5;round++)for(const sample of fixture.samples){
    const result=await runtime.infer({...sample,include_pixels:false});metrics.push({kind:sample.kind,...result.metrics});
  }
  const report={created_at:new Date().toISOString(),platform:process.platform,node:process.version,
    cpu:os.cpus()[0].model,logical_cpus:os.cpus().length,load_ms,models:runtime.versions,metrics,
    scope:'sequential saved-frame inference; excludes camera I/O and end-to-end action validation'};
  fs.writeFileSync(process.argv[4]||'.runtime/edge-benchmark.json',JSON.stringify(report,null,2));
  console.log(JSON.stringify({samples:metrics.length,load_ms,max_rss_bytes:Math.max(...metrics.map(m=>m.rss_bytes)),
    mean_latency_ms:metrics.reduce((s,m)=>s+m.latency_ms,0)/metrics.length}));
})().catch(error=>{console.error(error.message);process.exitCode=1;});
