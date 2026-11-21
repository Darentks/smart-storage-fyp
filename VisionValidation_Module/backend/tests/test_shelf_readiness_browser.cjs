const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
(async()=>{for(const file of ['index.html','VisionValidation_Module/camera_manager.html']){
 const html=fs.readFileSync(file,'utf8');let run='one',state='Starting',online=true;const jobs=[],toasts=[];
 const ctx={window:{tmModel:{}},compartmentsData:[],getBridgeUrl:()=>'',renderCompartments(){},showToast:(...x)=>toasts.push(x),
 validationWorker:{submit:job=>{jobs.push(job);return Promise.resolve();}},fetch:async()=>{if(!online)throw Error('offline');return {ok:true,json:async()=>({run_id:run,shelves:{s:{state,reason:''}}})};}};
 vm.createContext(ctx);vm.runInContext(html.slice(html.indexOf('    function readinessText('),html.indexOf('    const MAX_PROCESSED_TRIGGER_IDS')),ctx);
 await ctx.pollShelfReadiness();await ctx.pollShelfReadiness();assert.equal(jobs.length,1);assert.equal(jobs[0].source,'STARTUP');
 state='Held';await ctx.pollShelfReadiness();assert.equal(jobs.length,1);
 run='two';state='Starting';await ctx.pollShelfReadiness();assert.equal(jobs.length,2);assert.match(toasts.at(-1)[0],/Bridge restarted/);
 online=false;await ctx.pollShelfReadiness();online=true;await ctx.pollShelfReadiness();assert.equal(jobs.length,3);
 console.log('PASS: '+file+' automatic startup, bounded retry scheduling, bridge restart and reconnect scheduling');
}})().catch(e=>{console.error(e);process.exitCode=1;});
