/* Fixed-slot UI. Registration fields never come from live probes. */
const FIXED_CAMERA_SLOTS = ALL_SHELF_IDS.flatMap((shelf_id,i)=>[1,2].map(angle_index=>({
  label:`CAM${i*2+angle_index}`,shelf_id,angle_index,role:angle_index===1?'Back':'Front',
  device_id:`ESP32_R1_C${i+1}_CAM${angle_index}`,ip_address:`192.168.0.${201+i*2+angle_index-1}`,
  environment:`cam${i*2+angle_index}_usb`
})));
function fixedCameraRegistrations(rows){
  return FIXED_CAMERA_SLOTS.map(slot=>{
    const matches=rows.filter(r=>r.device_id===slot.device_id);
    const conflict=rows.some(r=>r.shelf_id===slot.shelf_id && Number(r.angle_index)===slot.angle_index && r.device_id!==slot.device_id);
    const row=matches.length===1?matches[0]:null,errors=[];
    if(matches.length>1 || conflict)errors.push('Duplicate or conflicting registration');
    if(row){for(const key of ['shelf_id','angle_index','ip_address'])if(String(key==='ip_address'?cleanCameraHost(row[key]):row[key])!==String(slot[key]))errors.push(`${key} mismatch`);
      if(row.role && String(row.role).toLowerCase()!==slot.role.toLowerCase())errors.push('role mismatch');}
    return {...slot,row,errors,status:errors.length?'Configuration error':row?'Registered':'Not registered'};
  });
}
// One display-only picture per fixed camera, in this browser tab's RAM. No image archive.
const esp32CachedPictures=new Map();
const esp32PendingPictures=new Map();
function cachedShelfPicture(bridge,shelf){
  const key=bridge+'|'+shelf;
  if(!esp32PendingPictures.has(key)){
    const request=fetch(`${bridge}/native_observation?shelf_id=${encodeURIComponent(shelf)}`,{
      cache:'no-store',signal:AbortSignal.timeout(5000)
    }).then(async response=>{
      if(!response.ok)throw Error('Native picture cache unavailable');
      const payload=await response.json(),observation=payload.observation;
      if(!observation || observation.shelf_id!==shelf)throw Error('No cached picture yet. Wait for a native scan, then refresh.');
      return observation;
    }).finally(()=>esp32PendingPictures.delete(key));
    esp32PendingPictures.set(key,request);
  }
  return esp32PendingPictures.get(key);
}
async function loadCachedCameraPicture(slot){
  const bridge=getBridgeUrl(),key=bridge+'|'+slot.device_id;
  const previous=esp32CachedPictures.get(slot.device_id);
  if(previous?.key===key && previous.loading)return;
  const minimumLoading=new Promise(resolve=>setTimeout(resolve,1000));
  const record={key,loading:true,image:previous?.key===key?previous.image:'',timestamp:previous?.key===key?previous.timestamp:null,message:'Loading cached picture…'};
  esp32CachedPictures.set(slot.device_id,record);
  try{
    const observation=await cachedShelfPicture(bridge,slot.shelf_id);
    const image=slot.angle_index===1?observation.image_back:observation.image_front;
    if(typeof image!=='string' || !/^data:image\/(jpeg|png);base64,/.test(image) || image.length>8*1024*1024)throw Error('No usable cached picture for this camera.');
    record.image=image;record.timestamp=observation.timestamp;record.message='Cached picture';
  }catch(error){record.message=error.message;}
  finally{
    await minimumLoading;
    record.loading=false;
    if(esp32CachedPictures.get(slot.device_id)===record && getBridgeUrl()===bridge)renderEsp32CameraManager();
  }
}
async function refreshCachedCameraPicture(deviceId){
  const slot=fixedCameraRegistrations(esp32CameraNodesList).find(s=>s.device_id===deviceId);
  if(!slot || slot.status!=='Registered')return;
  const pending=loadCachedCameraPicture(slot);
  renderEsp32CameraManager();await pending;
}
// Probe results live here, never in the shared esp32CameraNodesList rows that Panel 2 and status polling use.
const esp32ProbeResults=new Map();
let esp32Probing=false,esp32OverviewLoaded=false;
function esp32IpSaveRunning(){return typeof esp32IpSaveInProgress!=='undefined' && esp32IpSaveInProgress===true;}
// First opening of the All 6 cameras section loads each registered camera's cached picture once.
function loadEsp32OverviewPictures(){
  if(esp32OverviewLoaded || !document.getElementById('all-cameras')?.open)return;
  const slots=fixedCameraRegistrations(esp32CameraNodesList).filter(s=>s.status==='Registered');
  if(!slots.length)return;
  esp32OverviewLoaded=true;
  for(const slot of slots)loadCachedCameraPicture(slot);
  renderEsp32CameraManager();
}
function onEsp32OverviewToggle(details){if(details?.open)loadEsp32OverviewPictures();}
function openEsp32Picture(deviceId){
  const record=esp32CachedPictures.get(deviceId),slot=FIXED_CAMERA_SLOTS.find(s=>s.device_id===deviceId);
  const dialog=document.getElementById('esp32PictureDialog'),full=document.getElementById('esp32PictureFull');
  if(!record?.image || !slot || !dialog || !full)return;
  full.src=record.image;full.alt=`${slot.label} ${slot.role} cached picture, full size`;
  document.getElementById('esp32PictureCaption').textContent=`${slot.label} · ${slot.shelf_id} ${slot.role}`+
    (Number.isFinite(record.timestamp)?' · '+new Date(record.timestamp*1000).toLocaleString():'');
  if(!dialog.open)dialog.showModal();
}
function renderEsp32CameraManager(){
  const grid=document.getElementById('esp32CameraManagerGrid');if(!grid)return;
  const probeButton=document.getElementById('probeAllNodesBtn');
  if(probeButton){
    probeButton.disabled=esp32Probing || esp32IpSaveRunning();
    probeButton.textContent=esp32Probing?'Probing…':'Probe all 6 cameras';
    probeButton.title=esp32IpSaveRunning()?'Wait for Save Assigned IPs to finish':'';
  }
  const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  document.getElementById('ignoredCameraRows').textContent=`${esp32CameraNodesList.filter(r=>!FIXED_CAMERA_SLOTS.some(s=>s.device_id===r.device_id)).length} retired/unrecognised rows ignored`;
  const slots=fixedCameraRegistrations(esp32CameraNodesList),bridge=getBridgeUrl();
  const pictures=new Map(Array.from(grid.querySelectorAll?.('img[data-camera-id]')||[]).map(image=>[image.dataset.cameraId,image]));
  const markup=slots.map(slot=>{
    const valid=slot.status==='Registered',record=esp32CachedPictures.get(slot.device_id);
    const cached=valid && record?.key===bridge+'|'+slot.device_id?record:null;
    const image=cached?.loading?'':cached?.image||'';
    const timestamp=!cached?.loading && Number.isFinite(cached?.timestamp)?new Date(cached.timestamp*1000).toLocaleString():'';
    const probe=esp32ProbeResults.get(slot.device_id),live=valid && probe?.ip===slot.ip_address?probe:null;
    return `<article style="padding:10px;border:1px solid #475569;border-radius:8px"><h3>${slot.label} · ${slot.shelf_id} ${slot.role}</h3>
      <p>${slot.device_id}<br>${slot.ip_address}</p><p>${slot.status} ${escape(slot.errors.join('; '))}</p>
      <p>${escape(live?live.status:slot.row?.status||'OFFLINE')} ${escape(live?.issue||'')}${live?.rssi_dbm!=null?' · '+escape(live.rssi_dbm)+' dBm':''}</p>
      ${image?`<button type="button" class="esp32-thumb" onclick="openEsp32Picture('${slot.device_id}')" aria-label="Open ${slot.label} ${slot.role} picture full size"><img data-camera-id="${slot.device_id}" src="${escape(image)}" alt="${slot.label} ${slot.role} cached picture"></button>`:''}
      ${cached?.loading?'<div role="status" aria-busy="true" style="height:110px;display:grid;place-items:center;color:var(--text-muted)">Loading cached picture…</div>':''}
      <p role="status">${escape(cached?.loading?'Loading…':cached?.message||'Click Show cached picture to view this camera.')}${timestamp?' · '+escape(timestamp):''}</p>
      <button class="btn-action" ${valid && !cached?.loading?'':'disabled'} onclick="refreshCachedCameraPicture('${slot.device_id}')">${cached?.loading?'Loading…':cached?.image?'Refresh cached picture':'Show cached picture'}</button>
      </article>`;
  }).join('');
  if(grid.innerHTML!==markup){
    grid.innerHTML=markup;
    // Preserve the existing image elements when only telemetry changes, avoiding visual reloads.
    for(const image of Array.from(grid.querySelectorAll?.('img[data-camera-id]')||[])){
      const previous=pictures.get(image.dataset.cameraId);
      if(previous && previous.getAttribute('src')===image.getAttribute('src'))image.replaceWith(previous);
    }
  }
  for(const slot of slots)if(slot.status!=='Registered')esp32CachedPictures.delete(slot.device_id);
  loadEsp32OverviewPictures();
}
// Button-only probe. Blocked while Save Assigned IPs runs; results never overwrite the shared rows.
async function probeAllEsp32CameraNodes(){
  if(esp32Probing || esp32IpSaveRunning())return;
  esp32Probing=true;renderEsp32CameraManager();
  try{
    await Promise.all(fixedCameraRegistrations(esp32CameraNodesList).filter(s=>s.status==='Registered').map(async slot=>{
      let result;
      try{const live=await probeCameraStatus(slot.ip_address);
        const mismatch=['device_id','shelf_id'].filter(k=>live[k]!==slot[k]);
        if(cleanCameraHost(live.ip_address)!==slot.ip_address)mismatch.push('IP');
        if(live.camera_role && live.camera_role!==slot.role)mismatch.push('role');
        if(live.angle_index && Number(live.angle_index)!==slot.angle_index)mismatch.push('angle');
        result={status:mismatch.length?'MISMATCH':'ONLINE',issue:mismatch.length?mismatch.join(', ')+' mismatch':'',rssi_dbm:typeof live.rssi_dbm==='number'?live.rssi_dbm:null};
      }catch(error){result={status:'OFFLINE',issue:'',rssi_dbm:null};}
      esp32ProbeResults.set(slot.device_id,{...result,ip:slot.ip_address});
    }));
  }finally{esp32Probing=false;renderEsp32CameraManager();}
}
// Fixed camera monitoring only: registration checks, probing and snapshots.
renderEsp32CameraManager();
