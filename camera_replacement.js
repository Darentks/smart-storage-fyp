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
      </article>`;
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
// Fixed camera monitoring only: registration checks, probing and snapshots.
renderEsp32CameraManager();
