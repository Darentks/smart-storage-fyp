/* Three live feeds share the existing serial ONNX runner and Python consensus bridge. */
class ThreeCameraDashboard {
  constructor(options) { Object.assign(this, options); this.contexts = new Map(); this.generation = 0; this.cursor = 0; this.running = false; this.startupGeneration = 0; this.validations = new Set(); this.votes = Promise.resolve(); }
  valid(c, epoch) { return this.running && c.epoch === epoch && c.generation === this.generation; }
  reset(c) { c.epoch++; c.frame = null; c.results = null; c.processed = 0; c.detectionStatus = 'Idle'; c.detectionAt = null; c.trackers = Object.fromEntries(Object.keys(c.rois).map(id => [id, this.resetTracker()])); }
  healthy(c, now = performance.now()) { return c.received > 0 && now - c.received < 1500 && c.processed > 0 && now - c.processed < 1500; }
  async post(path, body, signal) {
    const response = await fetch(this.bridge() + path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body), signal});
    const data = await response.json(); if (!response.ok) throw Error(data.error || response.status); return data;
  }
  stop() {
    this.running = false; this.generation++; this.startupGeneration++; clearTimeout(this.timer); clearTimeout(this.telemetryTimer);
    this.controller?.abort(); for (const c of this.contexts.values()) { clearTimeout(c.timer); this.reset(c); }
    this.status('Stopped');
  }
  async start(cameras, defaults) {
    this.stop(); const generation = this.generation; this.controller = new AbortController();
    await this.post('/cctv_invalidate_votes', {}, this.controller.signal);
    if (generation !== this.generation) return;
    const host = document.getElementById('threeCameraCards'); host.replaceChildren(); this.contexts.clear();
    for (const [index,id] of ['CCTV_01','CCTV_02','CCTV_03'].entries()) {
      const config = cameras.find(c => c.cctv_id === id);
      if (!config || config.is_active === false || !config.rtsp_url) throw Error(`Configure and enable ${id} first`);
      const role = index === 2 ? 'SIDE_PRESENCE' : 'GRID_6_ROI';
      if (config.camera_role !== role) throw Error(`Set ${id} role to ${role}`);
      let rois = structuredClone(defaults);
      const key = `smart_storage_cctv_rois_${id}`;
      const binding = JSON.parse(localStorage.getItem(key+'_binding') || 'null');
      // Use only calibration bound to this source and role.
      const sourceBinding = this.sourceBinding(config.rtsp_url);
      if (binding && binding.camera_role === role && binding.source === sourceBinding) Object.assign(rois, JSON.parse(localStorage.getItem(key) || '{}'));
      const card = document.createElement('article'), label = document.createElement('div'), canvas = document.createElement('canvas');
      canvas.width=640; canvas.height=480; canvas.style.width='100%';
      canvas.onclick=()=>{canvas.style.maxHeight=canvas.style.maxHeight ? '' : '75vh'; card.style.gridColumn=card.style.gridColumn ? '' : '1 / -1';};
      card.append(label,canvas);host.append(card);
      const c={id,config,source:config.rtsp_url,role,rois,canvas,label,generation,epoch:0,seq:null,received:0,processed:0,fps:0,detectionStatus:'Idle',lastVote:'none'};
      this.reset(c); this.contexts.set(id,c);
    }
    this.running=true;this.cursor=0;
    for (const c of this.contexts.values()) this.poll(c);
    this.schedule(); this.telemetry();
  }
  async invalidate(c) {
    if (!this.running || (!c.received && !c.processed && !c.frame)) return;
    c.received=0; this.reset(c);
    // Clear all local dwell evidence as well as bridge votes after a feed gap.
    for (const other of this.contexts.values()) if(other!==c) this.reset(other);
    try { await this.post('/cctv_invalidate_votes',{},this.controller.signal); } catch (_) {}
  }
  async poll(c) {
    const epoch=c.epoch, generation=this.generation;
    try {
      const response=await fetch(`${this.bridge()}/test_feed?source=${encodeURIComponent(c.source)}&cctv_id=${c.id}`,{cache:'no-store',signal:AbortSignal.any([this.controller.signal,AbortSignal.timeout(3000)])});
      const data=await response.json();
      if(!this.valid(c,epoch)) return;
      if(!response.ok || !data.image || data.seq===undefined) throw Error('No fresh frame sequence');
      if(data.seq!==c.seq) {
        const image=new Image(); image.src=data.image; await image.decode();
        if(!this.valid(c,epoch)) return;
        c.seq=data.seq; c.frame=image; c.received=performance.now();
      } else if(performance.now()-c.received>1500) await this.invalidate(c);
    } catch(error) { if(this.valid(c,epoch)) await this.invalidate(c); }
    finally { if(this.running && generation===this.generation) c.timer=setTimeout(()=>this.poll(c),100); }
  }
  async schedule() {
    const generation=this.generation;
    if(!this.running)return;
    const cameras=[...this.contexts.values()];
    const c=cameras[this.cursor++ % cameras.length];
    let ran=false;
    try {
      if(c.frame && performance.now()-c.received<1500) {
        ran=true;
        const image=c.frame, timestamp=c.received, epoch=c.epoch;c.frame=null;
        if(c.canvas.width!==image.naturalWidth)c.canvas.width=image.naturalWidth;
        if(c.canvas.height!==image.naturalHeight)c.canvas.height=image.naturalHeight;
        const accepted = await this.infer(image,c,timestamp);
        if(accepted === true && this.valid(c,epoch) && performance.now()-timestamp<1500) {
          const now=performance.now();c.fps=c.processed ? 1000/(now-c.processed) : 0;c.processed=now;
        }
      }
      const missing=cameras.filter(c=>!this.healthy(c));
      this.status(missing.length ? 'Waiting for '+missing.map(c=>c.id.replace('_',' ')).join(', ') : 'All cameras detecting');
      for(const c of cameras)c.label.textContent=`${c.id} (${c.role}) | ${this.healthy(c)?'Connected':'Waiting'} | ${c.detectionStatus||'Idle'} | ${c.fps.toFixed(1)} FPS | processed ${c.processed?Math.round((performance.now()-c.processed))+'ms ago':'never'} | vote ${c.lastVote}`;
    } catch(error) {this.event(`${c.id}: ${error.message}`);await this.invalidate(c);}
    finally {if(this.running && generation===this.generation)this.timer=setTimeout(()=>this.schedule(),ran?0:25);}
  }
  vote(c,shelf,action,dwell,confidence) {
    const epoch=c.epoch;
    const detectedAt = c.detectionAt ?? Date.now()/1000;
    const queuedAt = performance.now();
    const body={camera_id:c.id,camera_role:c.role,trigger_id:crypto.randomUUID(),source:'CCTV_BROWSER_DWELL',detected_at:detectedAt,shelf_id:c.role==='SIDE_PRESENCE'?'ANY_SHELF':shelf,oks_confidence:confidence,dwell_duration_seconds:dwell};
    if(c.role!=='SIDE_PRESENCE')body.expected_action=action;
    this.votes=this.votes.catch(()=>{}).then(async()=>{
      if(!Number.isFinite(detectedAt) || Date.now()/1000-detectedAt > 4 || detectedAt-Date.now()/1000 > 1 || performance.now()-queuedAt > 4000)return;
      if(!this.valid(c,epoch) || ![...this.contexts.values()].every(c=>this.healthy(c)))return;
      const result=await this.post('/esp32_trigger',body,this.controller.signal);
      if(!this.valid(c,epoch))return;
      c.lastVote=`${body.shelf_id} ${body.expected_action || 'presence'}`;this.event(`${c.id} vote: ${c.lastVote}`);
      if(result.consensus_met) {
        this.event(`Consensus confirmed: ${result.shelf_id} (${result.expected_action || 'BORROW'})`);
        this.event(`MQTT status: ${result.mqtt_connected?'connected':'disconnected'}`);
        this.event(`ESP32 capture request: ${result.mqtt_connected?'dispatched':'not delivered'} for ${result.shelf_id}`);
        // ValidationQueue retains ownership, ordering and deduplication; it must not block votes.
        const task = this.validate(result, c, epoch);
        this.validations.add(task);
        task.finally(() => this.validations.delete(task));
      }
    }).catch(error=>this.event('Vote: '+error.message));
  }
  async validate(result, c, epoch) {
    try {
      const validation = await this.consume(result);
      if (!this.valid(c,epoch)) return;
      if (validation == null) this.event(`Inventory validation skipped for ${result.shelf_id}`);
      else if (validation.status === 'success' && validation.comparison) this.event(`Inventory validation completed for ${result.shelf_id}`);
      else this.event(`Inventory validation failed for ${result.shelf_id}`);
      return validation;
    } catch(error) {
      if(this.valid(c,epoch))this.event(`Inventory validation failed for ${result.shelf_id}: ${error.message}`);
    }
  }
  async telemetry() {
    const generation=this.generation;
    try {
      const r=await fetch(this.bridge()+'/esp32_nodes',{signal:this.controller.signal});const data=await r.json();
      if(!this.running || generation!==this.generation)return;
      const readySig=JSON.stringify(data.recent_responses||[]);
      if(readySig!==this.lastReadyTelemetry){this.lastReadyTelemetry=readySig;this.event(`ESP32 readiness: ${readySig}`);}
      const logSig=JSON.stringify(data.comparison_logs||[]);
      if(logSig!==this.lastLogTelemetry){this.lastLogTelemetry=logSig;this.event(`Inventory validation results: ${logSig}`);}
    }catch(_){}
    finally {if(this.running && generation===this.generation)this.telemetryTimer=setTimeout(()=>this.telemetry(),2000);}
  }
}
if(typeof module!=='undefined')module.exports=ThreeCameraDashboard;
