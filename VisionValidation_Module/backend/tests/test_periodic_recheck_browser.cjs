const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

async function scenario(file, predictions) {
  const html = fs.readFileSync(file, 'utf8');
  const start = html.indexOf('    const PERIODIC_CONFIRM_DELAY_MS');
  const end = html.indexOf('    // Trigger Test Capture', start);
  const requests = [], toasts = [], logs = [];
  class Image {
    set src(value) { this.onload(); }
  }
  let index = 0;
  const context = {
    window:{dualCamSettings:{consistentCaptures:3}}, dualCamEvidence:samples=>({samples}), Image, console: {warn() {}}, Set, Number, Promise, Date,
    validatingShelves: new Set(), processedTriggerIds: new Set(),
    compartmentsData: [{shelf_id:'s',ip_address:'back',secondary_ip_address:'front',current_quantity:4}],
    getBridgeUrl: () => 'http://bridge',
    clearTimeout,setTimeout: (fn, delay) => { if(delay===2000){fn();return;}return setTimeout(fn,delay); },
    predictDualCamerasWithTM: async () => predictions[index++],
    fetch: async (url, options) => {
      const payload = options?.body ? JSON.parse(options.body) : null;
      requests.push({url,payload});
      let data;
      if (url.endsWith('/validation_begin')) data = {token:'lease'};
      else if (url.endsWith('/validation_renew')) data = {renewed:true};
      else if (url.endsWith('/cctv_trigger_claim')) data = {claimed:true};
      else if (url.includes('/esp32_preview')) data = {image:'combined',image_back:'back',image_front:'front',capture:{id:String(requests.length),completed_at:Date.now()/1000,duration_ms:500}};
      else if (url.endsWith('/esp32_validate')) data = {comparison:{delta:-1,state_delta:'ITEM_REMOVED'},log:{source:'PERIODIC_RECHECK'}};
      else data = {released:true};
      return {ok:true,json:async()=>data};
    },
    showToast: (...args) => toasts.push(args),
    prependLatestAction: log => logs.push(log), refreshAllData: async()=>{}
  };
  vm.createContext(context);
  vm.runInContext(html.slice(start,end) + '\nglobalThis.run = runPeriodicRecheckValidation;',context);
  await context.run('s',{trigger_id:'trigger'});
  assert.equal(context.validatingShelves.size,0);
  assert.equal(requests.filter(r=>r.url.endsWith('/validation_end')).length,1);
  return {requests,toasts,logs};
}
(async () => {
  for (const file of ['index.html','VisionValidation_Module/camera_manager.html']) {
    const html=fs.readFileSync(file,'utf8');
    for (const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) {
      if (!/\bsrc\s*=|type\s*=\s*["'](?:module|application\/)/i.test(match[1]) && match[2].trim()) new Function(match[2]);
    }
    const prediction = count => ({totalBoxes:count,confidence:90,degraded:false,scenarioLabel:'count',occupancyKey:String(count)});
    const good=await scenario(file,[prediction(3),prediction(3),prediction(3)]);
    const writes=good.requests.filter(r=>r.url.endsWith('/esp32_validate'));
    assert.equal(writes.length,1);
    assert.equal(writes[0].payload.source,'PERIODIC_RECHECK');
    assert.equal(writes[0].payload.predicted_box_count,3);
    for (const count of [4,5]) {
      const returned=await scenario(file,[prediction(count),prediction(count),prediction(count)]);
      const updates=returned.requests.filter(r=>r.url.endsWith('/esp32_validate'));
      assert.equal(updates.length,1);
      assert.equal(updates[0].payload.predicted_box_count,count);
    }
    assert.match(good.toasts[0][0],/ITEM_REMOVED \(-1\).*periodic recheck/);
    const mismatch=await scenario(file,[prediction(3),prediction(2)]);
    assert.equal(mismatch.requests.filter(r=>r.url.endsWith('/esp32_validate')).length,0);
    assert.equal(mismatch.toasts.length,1);
    assert.match(mismatch.toasts[0][0],/Unresolved/);
    const low=await scenario(file,[prediction(3),{...prediction(3),confidence:64}]);
    assert.equal(low.requests.filter(r=>r.url.endsWith('/esp32_validate')).length,0);
    const degraded=await scenario(file,[{...prediction(3),degraded:true}]);
    assert.equal(degraded.requests.filter(r=>r.url.endsWith('/esp32_validate')).length,0);
    console.log('PASS: '+file+' syntax, agreeing counts, mismatch, low confidence, degraded input, and lease cleanup');
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
