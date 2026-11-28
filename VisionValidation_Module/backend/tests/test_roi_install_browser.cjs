const assert=require('assert'),fs=require('fs'),vm=require('vm');
const grab=(src,head)=>{const i=src.indexOf(head);assert(i>=0,head);let d=0;for(let k=src.indexOf('{',i);;k++){if(src[k]==='{')d++;if(src[k]==='}'&&--d===0)return src.slice(i,k+1);}};

(async()=>{
  for(const file of ['index.html','VisionValidation_Module/camera_manager.html']){
    const src=fs.readFileSync(file,'utf8');
    assert(src.includes('onclick="installEdgeCalibrations()"'),`${file}: Install to Bridge button missing`);
    const store=new Map(),toasts=[],posts=[];let reply;
    const ctx={window:{},URL,JSON,Error,AbortSignal:{timeout:()=>undefined},ALL_SHELF_IDS:[1,2,3,4,5,6].map(i=>`SHELF_R1_C${i}`),
      localStorage:{getItem:k=>store.has(k)?store.get(k):null},showToast:(m,t)=>toasts.push([t,m]),getBridgeUrl:()=>'http://localhost:8008',
      fetch:async(url,options)=>{posts.push([url,options]);return reply;},edgeDownload:()=>{throw Error('no download on install');}};
    vm.createContext(ctx);
    vm.runInContext(['function cctvSourceBinding(','function cameraRole(','function edgeCalibrationPayload(','async function installEdgeCalibrations(','async function uploadCalibrationFile(','async function postLocalRois('].map(h=>grab(src,h)).join('\n')
      +'\nlet installingEdgeCalibrations=false;',ctx);
    ctx.cctvCamerasList=[1,2,3].map(i=>({cctv_id:`CCTV_0${i}`,rtsp_url:`rtsp://u:p@10.0.0.${i}/s`,is_active:true,camera_role:i===3?'SIDE_PRESENCE':'GRID_6_ROI'}));
    // Missing saved ROIs: no upload, error shown.
    await ctx.installEdgeCalibrations();
    assert.equal(posts.length,0);assert.equal(toasts.pop()[0],'error');
    for(const [i,cam] of ctx.cctvCamerasList.entries()){
      const role=i===2?'SIDE_PRESENCE':'GRID_6_ROI',ids=i===2?['RACK_ENTRY_ZONE']:ctx.ALL_SHELF_IDS;
      store.set(`smart_storage_cctv_rois_${cam.cctv_id}`,JSON.stringify(Object.fromEntries(ids.map(s=>[s,{x_min:0,y_min:0,x_max:.1,y_max:.1}]))));
      store.set(`smart_storage_cctv_rois_${cam.cctv_id}_binding`,JSON.stringify({source:ctx.cctvSourceBinding(cam.rtsp_url),camera_role:role}));
    }
    reply={ok:true,status:200,json:async()=>({status:'installed',native:{status:'verifying'}})};
    await ctx.installEdgeCalibrations();
    const [url,options]=posts.pop();
    assert.equal(url,'http://localhost:8008/local_rois');assert.equal(options.method,'POST');
    const body=JSON.parse(options.body);assert.equal(body.version,1);assert.equal(body.cameras.length,3);
    assert(!options.body.includes('u:p@'),'credentials must not be uploaded');
    const [kind,message]=toasts.pop();assert.equal(kind,'success');assert(/hands clear of the rack/.test(message)&&/Panel 1/.test(message));
    ctx.window.smartStorageDeviceAdminSession={access_token:'user-session'};
    await ctx.installEdgeCalibrations();assert.equal(posts.pop()[1].headers.Authorization,'Bearer user-session');
    reply={ok:false,status:422,json:async()=>({error:'ROI import rejected',output:'Traceback\nValueError: Overlapping shelf ROIs'})};
    await ctx.installEdgeCalibrations();
    const failure=toasts.pop();assert.equal(failure[0],'error');assert(/Overlapping shelf ROIs/.test(failure[1]));
    reply={ok:true,status:200,json:async()=>({native:{status:'blocked',message:'SERVICE_CUTOVER_BLOCKED'}})};
    await ctx.installEdgeCalibrations();assert.equal(toasts.pop()[0],'error');
    // Upload calibration file: same checked route; rejects non-exports and files for other camera addresses.
    assert(src.includes('id="cctvCalibrationFileInput"')&&src.includes('onchange="uploadCalibrationFile(this)"'),`${file}: upload control missing`);
    posts.length=0;
    const exported=JSON.stringify(ctx.edgeCalibrationPayload());
    const pick=text=>({files:[{size:text.length,text:async()=>text}],value:'x'});
    reply={ok:true,status:200,json:async()=>({status:'installed',native:{status:'verifying'}})};
    let input=pick(exported);await ctx.uploadCalibrationFile(input);
    assert.equal(input.value,'','file input cleared so the same file can be chosen again');
    let [upUrl,upOptions]=posts.pop();assert.equal(upUrl,'http://localhost:8008/local_rois');assert.equal(upOptions.method,'POST');
    assert.deepStrictEqual(JSON.parse(upOptions.body),JSON.parse(exported));assert.equal(upOptions.headers.Authorization,'Bearer user-session');
    assert.equal(toasts.pop()[0],'success');
    for(const bad of ['not json',JSON.stringify({version:1,cameras:[]}),JSON.stringify({...JSON.parse(exported),cameras:JSON.parse(exported).cameras.map(c=>c.cctv_id==='CCTV_02'?{...c,binding:{...c.binding,source:'10.9.9.9/s'}}:c)})]){
      await ctx.uploadCalibrationFile(pick(bad));
      assert.equal(posts.length,0,'invalid or mismatched file must not be uploaded');assert.equal(toasts.pop()[0],'error');
    }
    await ctx.uploadCalibrationFile({files:[{size:2000001,text:async()=>exported}],value:'x'});assert.equal(posts.length,0);assert.match(toasts.pop()[1],/too large/);
    reply={ok:false,status:422,json:async()=>({error:'ROI import rejected',output:'ValueError: Overlapping shelf ROIs'})};
    await ctx.uploadCalibrationFile(pick(exported));posts.pop();assert.match(toasts.pop()[1],/Overlapping shelf ROIs/);
    console.log(`PASS ${file} Install to Bridge and Upload calibration file share the checked route, upload without credentials, report verify/blocked/422, reject mismatched files`);
  }
})().catch(error=>{console.error(error);process.exit(1);});
