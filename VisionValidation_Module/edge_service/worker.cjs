'use strict';
const readline=require('node:readline');
const {Inference}=require('./inference.cjs');
const runtime=new Inference();
const send=value=>process.stdout.write(JSON.stringify(value)+'\n');
(async()=>{
  const kind=process.argv[3] || 'all';
  const versions=await runtime.init(process.argv[2],kind);send({ready:true,versions});
  // Serial iteration provides backpressure; the bridge keeps at most one waiting frame.
  for await(const line of readline.createInterface({input:process.stdin,crlfDelay:Infinity})) {
    let job;
    try {
      if(line.length>16_500_000) throw Error('JOB_TOO_LARGE');
      job=JSON.parse(line);
      if(kind!=='all' && job.kind!==kind) throw Error('WRONG_WORKER_KIND');
      if(!Number.isFinite(job.expires_at) || Date.now()/1000>job.expires_at) throw Error('EXPIRED_JOB');
      const result=await runtime.infer(job);
      if(Date.now()/1000>job.expires_at) throw Error('EXPIRED_RESULT');
      send({id:job.id,startup_id:job.startup_id,result});
    } catch (_) {send({id:job?.id,error:'INFERENCE_REJECTED'});}
  }
})().catch(()=>{send({error:'MODEL_INITIALIZATION_FAILED'});process.exitCode=1;});
