const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const turn = () => new Promise(resolve => setImmediate(resolve));
function deferred() { let resolve; const promise = new Promise(r => {resolve=r;}); return {promise,resolve}; }

async function tests(file) {
  const html = fs.readFileSync(file,'utf8');
  const context = {processedTriggerIds:new Set(), console};
  vm.createContext(context);
  vm.runInContext(html.slice(html.indexOf('    class ValidationQueue'),html.indexOf('    const validationWorker'))
    +'\nglobalThis.Queue=ValidationQueue;globalThis.remember=rememberProcessedTrigger;',context);
  const base = overrides => ({reserved:new Set(), acquire:async()=> 'token', cancel:async()=>{},
    claim:async()=>true,execute:async()=>{},release:async()=>{},processed:()=>{},pause:turn,onError:()=>{},...overrides});
  const job = (shelfId,source,age=0) => ({shelfId,source,createdAt:Date.now()-age});
  // Priority and FIFO, independent of insertion order.
  let order=[];
  let queue=new context.Queue(base({execute:async item=>order.push(item.shelfId)}));
  await Promise.all([queue.submit(job('periodic','PERIODIC_RECHECK',3000)),
    queue.submit(job('cctv','CCTV',1000)),queue.submit(job('manual','MANUAL',2000))]);
  assert.deepEqual(order,['manual','cctv','periodic']);
  // Every waiting job is registered before acquisition, including queued jobs in other windows.
  const registered=[];
  queue=new context.Queue(base({register:async item=>registered.push(item.shelfId),
    acquire:async item=>{assert.ok(registered.includes(item.shelfId));return 'token';}}));
  const registeredJobs=[queue.submit(job('one','MANUAL')),queue.submit(job('two','PERIODIC_RECHECK'))];
  assert.deepEqual(registered,['one','two']);await Promise.all(registeredJobs);
  // One metadata-only entry per shelf, upgrading backup to live priority.
  order=[];
  queue=new context.Queue(base({execute:async item=>order.push(item.source)}));
  const first=queue.submit(job('s','PERIODIC_RECHECK'));
  assert.equal(queue.submit(job('s','PERIODIC_RECHECK')),first);
  assert.equal(queue.submit(job('s','CCTV')),first);
  assert.equal(queue.items.length,1);
  await first;
  assert.deepEqual(order,['CCTV']);
  // A manual upgrade must not retain the backup trigger's expiry/claim requirement.
  queue=new context.Queue(base({claim:async()=>{throw Error('manual must not claim old backup');},
    execute:async item=>assert.equal(item.trigger,undefined)}));
  const upgraded=queue.submit({...job('upgrade','PERIODIC_RECHECK'),trigger:{trigger_id:'old'}});
  queue.submit(job('upgrade','MANUAL'));await upgraded;
  // Active work is never interrupted; queued live jobs run before backup.
  const gate=deferred(); let active=0,peak=0;
  order=[];
  queue=new context.Queue(base({execute:async item=>{
    active++;peak=Math.max(peak,active);order.push(item.shelfId);
    if(item.shelfId==='active') await gate.promise;
    active--;
  }}));
  const activeJob=queue.submit(job('active','PERIODIC_RECHECK'));
  await turn();
  const backup=queue.submit(job('backup','PERIODIC_RECHECK'));
  const live=queue.submit(job('live','CCTV'));
  assert.equal(queue.submit(job('active','PERIODIC_RECHECK')),activeJob);
  gate.resolve(); await Promise.all([activeJob,backup,live]);
  assert.equal(peak,1);assert.deepEqual(order,['active','live','backup']);
  // Re-sort while waiting on another window's bridge lease.
  const pauseGate=deferred(); let available=false; const cancelled=[];
  order=[];
  queue=new context.Queue(base({acquire:async()=> available?'token':null,
    pause:()=>pauseGate.promise,cancel:async item=>cancelled.push(item.shelfId),
    execute:async item=>order.push(item.shelfId)}));
  const waiting=queue.submit(job('waiting-backup','PERIODIC_RECHECK'));
  await turn();
  const priority=queue.submit(job('new-live','CCTV'));
  available=true;pauseGate.resolve();await Promise.all([waiting,priority]);
  assert.deepEqual(order,['new-live','waiting-backup']);assert.ok(cancelled.includes('waiting-backup'));
  // Acquisition/claim/execution errors cannot strand reservations or stop later work.
  let released=0,errors=0;
  queue=new context.Queue(base({acquire:async item=>{if(item.shelfId==='network')throw Error('network');return 't';},
    execute:async item=>{if(item.shelfId==='failed')throw Error('inference');},
    release:async()=>{released++;},onError:()=>errors++}));
  await Promise.all(['network','failed','okay'].map(s=>queue.submit(job(s,'MANUAL'))));
  assert.equal(queue.reserved.size,0);assert.equal(released,2);assert.equal(errors,2);
  // Export is never coalesced with or upgraded into same-shelf validation.
  const exportHold=deferred(); order=[];
  queue=new context.Queue(base({execute:async item=>{
    order.push(item.kind || 'VALIDATION');
    if(item.kind==='EXPORT') await exportHold.promise;
  }}));
  const exportJob=queue.submit({...job('same','MANUAL'),kind:'EXPORT'});
  await turn();
  const validateJob=queue.submit(job('same','MANUAL'));
  assert.notEqual(exportJob,validateJob);
  assert.equal(queue.items.length,1);
  assert.equal(queue.reserved.has('same'),true);
  exportHold.resolve();await Promise.all([exportJob,validateJob]);
  assert.deepEqual(order,['EXPORT','VALIDATION']);assert.equal(queue.reserved.size,0);
  // The lease acquired by the worker is passed to the event claim.
  queue=new context.Queue(base({claim:async(item,token)=>{
    assert.equal(token,'token');assert.equal(item.trigger.trigger_id,'bound-event');return true;
  }}));
  await queue.submit({...job('bound','CCTV'),trigger:{trigger_id:'bound-event'}});
  // Queue and processed IDs stay bounded under repeated input.
  const hold=deferred();
  queue=new context.Queue(base({execute:async()=>hold.promise}));
  const many=Array.from({length:25},(_,i)=>queue.submit(job('s'+i,'MANUAL')));
  assert.equal(queue.items.length,20);hold.resolve();await Promise.all(many);
  for(let i=0;i<1000;i++) context.remember('trigger-'+i);
  assert.equal(context.processedTriggerIds.size,200);
  assert.equal(context.processedTriggerIds.has('trigger-0'),false);
  assert.equal(context.processedTriggerIds.has('trigger-999'),true);
  console.log('PASS: '+file+' priority/FIFO, single-flight, dedup/upgrade, busy-window reprioritization, error recovery, queue/ID bounds');
}
(async()=>{for(const file of ['index.html','VisionValidation_Module/camera_manager.html'])await tests(file);})()
.catch(error=>{console.error(error);process.exitCode=1;});
