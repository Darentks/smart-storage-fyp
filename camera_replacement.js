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
function renderEsp32CameraManager(){
  const grid=document.getElementById('esp32CameraManagerGrid');if(!grid)return;
  const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  document.getElementById('ignoredCameraRows').textContent=`${esp32CameraNodesList.filter(r=>!FIXED_CAMERA_SLOTS.some(s=>s.device_id===r.device_id)).length} retired/unrecognised rows ignored`;
  grid.innerHTML=fixedCameraRegistrations(esp32CameraNodesList).map(slot=>{
    const mode=esp32NodeSnapshotModeMap[slot.device_id]||'live',valid=slot.status==='Registered';
    const image=valid && esp32NodeCacheBustMap[slot.device_id]?captureLiveSnapshotUrl(slot.ip_address,mode,0):'';
    return `<article style="padding:14px;border:1px solid #475569;border-radius:8px"><h3>${slot.label} · ${slot.shelf_id} ${slot.role}</h3>
      <p>${slot.device_id}<br>${slot.ip_address}</p><p>${slot.status} ${escape(slot.errors.join('; '))}</p>
      <p>${escape(slot.row?.status||'OFFLINE')} ${escape(slot.row?.probe_issue||'')}</p>
      ${image?`<img src="${escape(image)}" alt="${slot.label} ${slot.role}" style="width:100%;height:140px;object-fit:contain">`:''}
      <button class="btn-action" ${valid?'':'disabled'} onclick="triggerSingleCameraNode('${slot.device_id}','${slot.ip_address}','${slot.shelf_id}')">Test Trigger</button>
      <button class="btn-action" ${valid?'':'disabled'} onclick="toggleNodeSnapshotMode('${slot.device_id}')">Baseline/Live: ${mode}</button>
      <label class="btn-action">Wireless OTA (.bin)<input type="file" accept=".bin" ${valid?'':'disabled'} onchange="uploadNodeOtaBin(event,'${slot.device_id}','${slot.ip_address}')"></label>
      <small>OTA requires firmware compiled for ${slot.device_id}.</small></article>`;
  }).join('');
}
async function probeAllEsp32CameraNodes(){
  await Promise.all(fixedCameraRegistrations(esp32CameraNodesList).filter(s=>s.status==='Registered').map(async slot=>{
    try{const live=await probeCameraStatus(slot.ip_address);
      const mismatch=['device_id','shelf_id'].filter(k=>live[k]!==slot[k]);
      if(cleanCameraHost(live.ip_address)!==slot.ip_address)mismatch.push('IP');
      if(live.camera_role && live.camera_role!==slot.role)mismatch.push('role');
      if(live.angle_index && Number(live.angle_index)!==slot.angle_index)mismatch.push('angle');
      Object.assign(slot.row,{status:mismatch.length?'MISMATCH':'ONLINE',probe_issue:mismatch.length?mismatch.join(', ')+' mismatch':'',rssi_dbm:live.rssi_dbm});
    }catch(error){slot.row.status='OFFLINE';}
  }));renderEsp32CameraManager();
}
let replacementTimer=null;
const replacementLocal=()=>location.hostname==='127.0.0.1' && /^\/local-dashboard(?:\/esp32-cams)?\/?$/.test(location.pathname);
async function replacementFetch(operation,body){
  if(!replacementLocal())throw Error('Open http://127.0.0.1:8008/local-dashboard/esp32-cams on the bridge laptop (use its configured port).');
  const response=await fetch(`${location.origin}/replacement/${operation}`,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{cache:'no-store'});
  const data=await response.json();if(!response.ok)throw Error(data.error||'Replacement request failed');return data;
}
function replacementPreview(){
  const slot=FIXED_CAMERA_SLOTS.find(s=>s.label===document.getElementById('replacementSlot').value);
  document.getElementById('replacementPreview').textContent=slot?`${slot.device_id}\n${slot.shelf_id} · ${slot.role} · ${slot.ip_address}\n${slot.environment}`:'';
  document.getElementById('replacementDisconnected').checked=false;
}
async function refreshReplacementPorts(){
  try{const data=await replacementFetch('ports');const select=document.getElementById('replacementPort');select.replaceChildren();
    for(const row of data.ports){const option=document.createElement('option');option.value=row.port;option.textContent=`${row.port} ${row.description}`;select.append(option);}
    if(!data.ports.length)document.getElementById('replacementProgress').textContent='No USB serial port found. Install the programmer driver and reconnect.';
  }catch(error){document.getElementById('replacementProgress').textContent=error.message;}
}
async function replacementStatus(){
  try{const data=await replacementFetch('status');const job=data.job;
    document.getElementById('replacementProgress').textContent=`Firmware repository: ${data.repository_available?'available':'missing'} · PlatformIO: ${data.platformio_available?'available':'missing'}\n${data.blocker||''}\n${job?`${job.camera_slot}: ${job.stage}\n${job.error||''}`:'Select a slot and begin replacement.'}\n${data.native?.scan?.message||''}\nMaintenance: ${data.maintenance.map(r=>r.camera_slot).join(', ')||'none'}`;
    document.getElementById('replacementSlot').disabled=Boolean(job?.busy);
  }catch(error){document.getElementById('replacementProgress').textContent=error.message;}
}
async function openReplacement(){
  if(!replacementLocal()){showToast('Open http://127.0.0.1:8008/local-dashboard/esp32-cams on the bridge laptop (or its configured port) for wired replacement.','info');return;}
  const dialog=document.getElementById('replacementDialog'),select=document.getElementById('replacementSlot');select.replaceChildren();
  for(const slot of FIXED_CAMERA_SLOTS){const option=document.createElement('option');option.value=slot.label;option.textContent=`${slot.label} ${slot.shelf_id} ${slot.role}`;select.append(option);}
  replacementPreview();dialog.showModal();await replacementStatus();await refreshReplacementPorts();
  clearInterval(replacementTimer);replacementTimer=setInterval(()=>{if(dialog.open)replacementStatus();else{clearInterval(replacementTimer);replacementTimer=null;}},1500);
}
async function replacementAction(operation){
  if(operation==='repair' && !confirm('Repair only the selected camera identity through USB? The physical ESP32 MAC must match this replacement job. Wi-Fi and unrelated NVS settings are retained.'))return;
  try{await replacementFetch(operation,{camera_slot:document.getElementById('replacementSlot').value,port:document.getElementById('replacementPort').value,disconnected:document.getElementById('replacementDisconnected').checked});await replacementStatus();}
  catch(error){document.getElementById('replacementProgress').textContent=error.message;}
}
if(!replacementLocal()){
  const button=document.getElementById('wiredReplacementButton');
  if(button){button.disabled=true;button.insertAdjacentText('afterend',' Wired replacement: open http://127.0.0.1:8008/local-dashboard/esp32-cams on the bridge laptop (use its configured port).');}
}
renderEsp32CameraManager();
