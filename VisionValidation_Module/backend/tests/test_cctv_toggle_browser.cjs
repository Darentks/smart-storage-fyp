// CCTV runs automatically with the bridge; the dashboard has no CCTV start/stop/pause control.
// (Pausing for camera maintenance is a bridge API call: POST /cctv_control.)
const fs=require('node:fs'),assert=require('node:assert/strict');
for(const file of ['index.html','VisionValidation_Module/camera_manager.html']){
  const html=fs.readFileSync(file,'utf8');
  assert.doesNotMatch(html,/>Start CCTV<|>Stop CCTV<|Pause CCTV|Resume CCTV|nativeCctvToggle|setNativeCctv|toggleNativeCctv|renderCctvToggle|\/cctv_control/,`${file}: CCTV control removed from the dashboard`);
  assert.match(html,/runtime\.cctv_enabled===false\?'last counted \(CCTV stopped\)'/,`${file}: shelf cards still say when CCTV is not running`);
  console.log(`PASS ${file} no dashboard CCTV control; CCTV runs with the bridge; paused state still shown on shelf cards`);
}
const bridge=fs.readFileSync('VisionValidation_Module/camera_service.py','utf8');
assert.match(bridge,/os\.getenv\('SMART_STORAGE_CCTV_AUTOSTART','1'\)/);assert.match(bridge,/if path == '\/cctv_control':/);
console.log('PASS bridge auto-starts CCTV and keeps /cctv_control for maintenance');
