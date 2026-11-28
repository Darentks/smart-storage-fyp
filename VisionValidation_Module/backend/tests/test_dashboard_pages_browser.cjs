const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('index.html','utf8'),script=fs.readFileSync('dashboard_pages.js','utf8');
assert.equal(html,fs.readFileSync('VisionValidation_Module/camera_manager.html','utf8'));
for(const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g))new vm.Script(match[1]);
// Model and inference libraries are pinned (same approach as onnxruntime-web 1.19.0) so Front/Back predictions stay stable.
assert.doesNotMatch(html,/@latest/,'no unpinned @latest CDN scripts');
for(const pinned of ['@tensorflow/tfjs@4.22.0/dist/tf.min.js','@teachablemachine/image@0.8.5/dist/teachablemachine-image.min.js','onnxruntime-web@1.19.0/dist/ort.min.js'])
 assert.ok(html.includes('https://cdn.jsdelivr.net/npm/'+pinned),pinned);
const rewrites=JSON.parse(fs.readFileSync('vercel.json','utf8')).rewrites;
assert.equal(JSON.parse(fs.readFileSync('vercel.json','utf8')).cleanUrls,true);
assert.ok(rewrites.every(r=>!r.destination.endsWith('.html')),'cleanUrls rewrite destinations must be extensionless');
// /esp32-cams keeps its rewrite so old links still load the page, which then redirects to #all-cameras.
for(const route of ['/cctv-configuration','/esp32-cams'])assert.ok(rewrites.some(r=>r.source===route&&r.destination==='/'));
assert.doesNotMatch(html,/data-dashboard-link="esp32"|data-dashboard-view="esp32"|data-dashboard-page="esp32"/,'ESP32 CAMs page and tab removed');
// [path, page, title, redirected address or null]
for(const [path,page,title,redirect] of [['/','dashboard','Inventory',null],['/cctv-configuration','cctv','CCTV setup',null],
 ['/esp32-cams','dashboard','Inventory','/?bridge=http%3A%2F%2F192.0.2.1%3A8008#all-cameras'],
 ['/local-dashboard','dashboard','Inventory',null],['/local-dashboard/cctv-configuration','cctv','CCTV setup',null],
 ['/local-dashboard/esp32-cams/','dashboard','Inventory','/local-dashboard?bridge=http%3A%2F%2F192.0.2.1%3A8008#all-cameras']]){
 const links=['dashboard','cctv'].map(value=>({dataset:{dashboardLink:value},hasAttribute:()=>false,setAttribute(k,v){this[k]=v;}}));
 const views=['dashboard','cctv'].map(value=>({dataset:{dashboardView:value},hidden:true}));
 const heading={},listeners={},assigned=[],frames=[],replaced=[];let calibrations=0,scrolledWhileVisible=null;
 const section={open:false,closest:()=>views[0],scrollIntoView(){scrolledWhileVisible=!views[0].hidden;}};
 const location={pathname:path,search:'?bridge=http%3A%2F%2F192.0.2.1%3A8008',hash:'',assign:value=>assigned.push(value)};
 const ctx={URLSearchParams,location,requestAnimationFrame:fn=>frames.push(fn),
  history:{replaceState(_,__,url){replaced.push(url);const u=new URL(url,'http://x');location.pathname=u.pathname;location.search=u.search;location.hash=u.hash;}},
  toggleRoiCalibrationMode:()=>calibrations++,document:{documentElement:{dataset:{}},
   getElementById:id=>id==='dashboardPageTitle'?heading:id==='all-cameras'?section:id==='connectionModels'?null:{scrollIntoView(){}},addEventListener:(name,fn)=>listeners[name]=fn,
   querySelectorAll:selector=>selector==='[data-dashboard-link]'?links:views}};
 vm.createContext(ctx);vm.runInContext(script,ctx);
 assert.deepStrictEqual(replaced,redirect?[redirect]:[],`${path}: redirect`);
 assert.equal(section.open,false,'section stays closed until the tab has switched');
 listeners.DOMContentLoaded();
 assert.equal(ctx.document.documentElement.dataset.dashboardPage,page);assert.equal(heading.textContent,title);
 assert.equal(ctx.document.title,`${title} · Smart Storage`);
 assert.equal(links.find(l=>l.dataset.dashboardLink===page)['aria-current'],'page');
 assert.equal(views.filter(v=>!v.hidden).length,1);assert.equal(views.find(v=>!v.hidden).dataset.dashboardView,page);
 // Old links open the All 6 cameras section and scroll to it on a later frame, after the dashboard view is shown.
 assert.equal(section.open,!!redirect,`${path}: section opened only for #all-cameras`);
 assert.equal(frames.length,redirect?1:0);frames.forEach(fn=>fn());
 assert.equal(scrolledWhileVisible,redirect?true:null);
 const prefix=path.startsWith('/local-dashboard')?'/local-dashboard':'';
 assert.ok(links[1].href.startsWith(prefix+'/cctv-configuration?bridge='));
 assert.equal(typeof ctx.navigateToDashboardPage,'undefined','unused navigation helper removed');assert.equal(calibrations,0);assert.equal(assigned.length,0);
 assert.throws(()=>ctx.dashboardPageUrl('esp32'),/Unknown dashboard page/);
 assert.equal(ctx.isLocalDashboardPath('/local-dashboard/unknown'),false);
 assert.equal(ctx.isLocalDashboardPath('/local-dashboard.evil'),false);
}
assert.doesNotMatch(html,/wiredReplacementButton|replacementDialog/);
console.log('PASS two-tab hosted/local page routes, titles, exclusive views, navigation, bridge query, calibration links, /esp32-cams -> #all-cameras after the tab switch, HTML parity');

// Opening/closing settings survives full-page navigation in this tab, including a fast click.
const preference=new Map();
function settingsPage(path,blocked=false) {
 const events={},section={open:false,addEventListener:(name,fn)=>events[name]=fn};
 const ctx={URLSearchParams,location:{pathname:path,search:'',hash:''},
  document:{documentElement:{dataset:{}},getElementById:()=>section,addEventListener(){}},
  window:{addEventListener:(name,fn)=>events[name]=fn},
  sessionStorage:{getItem(key){if(blocked)throw Error('blocked');return preference.get(key);},setItem(key,value){if(blocked)throw Error('blocked');preference.set(key,value);}}};
 vm.createContext(ctx);vm.runInContext(script,ctx);ctx.rememberConnectionModels();return {section,events};
}
const first=settingsPage('/');assert.equal(first.section.open,false);
first.section.open=true;first.events.pagehide();
const second=settingsPage('/cctv-configuration');assert.equal(second.section.open,true);
second.section.open=false;second.events.toggle();
assert.equal(settingsPage('/').section.open,false);
const blocked=settingsPage('/',true);blocked.section.open=true;assert.doesNotThrow(()=>blocked.events.toggle());
assert.match(html,/<div class="model-settings"/);
assert.doesNotMatch(html,/<div class="model-settings"[^>]*data-dashboard-view/);
console.log('PASS shared model controls and tab-local connection disclosure persistence, close, fast navigation and blocked storage');

// Camera addresses remain available to preview/training, with no reassignment controls.
for(const id of ['refEspIpInput','refEspIp2Input']) assert.match(html,new RegExp('<input[^>]*id="'+id+'"[^>]*\\breadonly\\b'));
for(const id of ['cam1IpSelect','cam2IpSelect','saveCamIpsBtn','scanCamIpsBtn']) assert.doesNotMatch(html,new RegExp('<(?:select|button)[^>]*id="'+id+'"'));
assert.doesNotMatch(html,/oninput="markIpInputDirty|onclick="saveAssignedShelfIps|onclick="discoverOnlineEsp32Cameras/);
console.log('PASS read-only camera addresses and removed reassignment UI');
