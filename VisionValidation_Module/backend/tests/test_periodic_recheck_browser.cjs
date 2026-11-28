const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
for(const file of ['index.html','VisionValidation_Module/camera_manager.html']){
 const html=fs.readFileSync(file,'utf8');
 assert.ok(!html.includes('function runPeriodicRecheckValidation('));
 assert.ok(!html.includes('function executePeriodicRecheckValidation('));
 assert.ok(!html.includes('30s Baseline'));
 for(const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)){
  if(!/\bsrc\s*=|type\s*=\s*["'](?:module|application\/)/i.test(match[1])&&match[2].trim())new Function(match[2]);
 }
 const ctx={};vm.createContext(ctx);
 vm.runInContext(html.slice(html.indexOf('    function nativeShelfView('),html.indexOf('    function renderCompartments(')),ctx);
 const render=(runtime,exact=true,range=false)=>ctx.nativeShelfView({details:'slots'}, {max_capacity:5},
  {native:{scan:{state:'Count ready'},actions_ready:false}},runtime,exact,range);
 for(const status of ['Count unchanged','Count changed','Check failed','Hidden box not checked']){
  const card=render({current_quantity:3,observed_at:100,cctv_enabled:false,
   count_check:{status,checked_at:120,message:'Previous count retained; hidden B1 was not checked.'}});
  assert.ok(card.display.startsWith('3 / 5 boxes'));assert.ok(card.display.includes(status));
  assert.match(card.details,/Last count check:/);assert.match(card.details,/hidden B1 was not checked/);
  assert.doesNotMatch(card.display,/Borrow|Return/);
 }
 const ranged=render({count_range:[2,3],count_check:{status:'Count changed',checked_at:100,message:'Showing a range.'}},false,true);
 assert.ok(ranged.display.includes('2\u20133'));assert.ok(ranged.display.includes('Count changed'));
 console.log('PASS '+file+' native count-check status/range/failure display; old browser timer and labels removed');
}
