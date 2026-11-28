const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
for(const file of ['index.html','VisionValidation_Module/camera_manager.html']){
 const html=fs.readFileSync(file,'utf8'),elements={};
 assert.ok(html.includes('href="smartstorage://update-models"'));
 const element=()=>({red:false});
 const ctx={document:{getElementById:id=>elements[id] ||= element()},markStatusError(el,on){el.red=!!on;},Date:{now:()=>ctx.now*1000},now:1000,
  fetch(){throw Error('Model update must use the local launcher, not a web writer');}};
 vm.createContext(ctx);
 vm.runInContext(html.slice(html.indexOf('    let tmUpdateRequestedAt'),html.indexOf('    async function requestNativeScan(')),ctx);
 const status=()=>ctx.document.getElementById('tmModelUpdateStatus');
 const roles={Front:{old:'111111111111',new:'222222222222',changed:true},Back:{old:'333333333333',new:'333333333333',changed:false}};
 const data=update=>({model_versions:{Front:{weights_sha256:'a'.repeat(64)},Back:{weights_sha256:'b'.repeat(64)}},
  model_sources:{file:'deploy/tm-model-sources.json',Front:'0cQO4TjOm',Back:'40mk-G4Oq',error:null},model_update:update});

 // Before any click, an old result is not presented as the outcome of this page's request.
 status().textContent='Model updates run on this PC through the Windows launcher.';
 ctx.renderLoadedTmVersions(data({at:900,outcome:'failed',failure:{stage:'download',message:'old'}}));
 assert.equal(status().textContent,'Model updates run on this PC through the Windows launcher.');
 assert.match(elements.tmLoadedModelVersions.textContent,/Front a{12} · Back b{12}/);
 assert.match(elements.tmLoadedModelVersions.textContent,/Update links \(deploy\/tm-model-sources\.json\): Front 0cQO4TjOm · Back 40mk-G4Oq/);

 ctx.beginLocalTmUpdate();
 assert.equal(status().textContent.startsWith('Launcher opened — waiting for result'),true);
 assert.doesNotMatch(status().textContent,/success|complete|updated/i,'a click is never reported as success');
 ctx.renderLoadedTmVersions(data({at:900,outcome:'unchanged',roles}));
 assert.match(status().textContent,/waiting for result/,'results older than the click are ignored');

 ctx.renderLoadedTmVersions(data({at:1001,outcome:'prepared',roles}));assert.match(status().textContent,/installing them and restarting/);
 ctx.renderLoadedTmVersions(data({at:1002,outcome:'installed',roles,loaded:'pending'}));assert.match(status().textContent,/waiting for the bridge to load/);
 ctx.renderLoadedTmVersions(data({at:1002,outcome:'installed',roles,loaded:'verified'}));
 assert.equal(status().textContent,'Models updated and verified: Front 111111111111 → 222222222222 · Back 333333333333 unchanged.');assert.equal(status().red,false);
 ctx.renderLoadedTmVersions(data({at:1002,outcome:'installed',roles,loaded:'mismatch'}));assert.match(status().textContent,/loaded different versions/);assert.equal(status().red,true);
 ctx.renderLoadedTmVersions(data({at:1002,outcome:'unchanged',roles:{Front:{new:'222222222222'},Back:{new:'333333333333'}},loaded:'verified'}));
 assert.match(status().textContent,/Already up to date: Front 222222222222 unchanged · Back 333333333333 unchanged\. Bridge not restarted/);assert.equal(status().red,false);
 ctx.renderLoadedTmVersions(data({at:1003,outcome:'failed',failure:{stage:'download',role:'Back',message:'HTTP 404 for model.json'}}));
 assert.equal(status().textContent,'Model update failed (Back download): HTTP 404 for model.json. Current models kept.');assert.equal(status().red,true);

 ctx.renderLoadedTmVersions(null);assert.match(elements.tmLoadedModelVersions.textContent,/not confirmed/);
 ctx.renderLoadedTmVersions({model_versions:{},model_sources:{file:'deploy/tm-model-sources.json',error:'Please input the correct Teachable Machine link for the Back model'}});
 assert.match(elements.tmLoadedModelVersions.textContent,/Front not loaded · Back not loaded · Update links \(deploy\/tm-model-sources\.json\): Please input the correct Teachable Machine link for the Back model/);
 ctx.beginLocalTmUpdate();ctx.now=2000;ctx.renderLoadedTmVersions(data(null));assert.match(status().textContent,/No result from the launcher yet/);
 console.log('PASS '+file+' launcher request is not success; verified, unchanged, failed and mismatch results from bridge status; update links shown');
}
