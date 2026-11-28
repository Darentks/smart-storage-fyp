"""Browser-independent orchestration; all inventory writes still use bridge guards.

One native coordinator counts shelves from ESP32 pairs and optionally watches the
three CCTVs, using separate box and pose workers. CCTV starts off. Counting needs
saved ESP32 records and staged Front/Back models; movement also needs saved ROIs.
A field-acceptance record is optional
and, when supplied, is fully verified. Normal operation is labelled NOT_FIELD_ACCEPTED.
No credentials or acquisition URLs are passed to the Node worker.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
import urllib.parse
import uuid

from .edge_shadow import EdgeShadow
from .runtime_inventory import RuntimeInventory, runtime_enabled
from .preview_panel import NativeObservations, InferenceBusy, LIVE_CAPTURE_BOOST, LIVE_CAPTURE_DUTY
from .validation_diagnostics import failure_code
from .shelves import SHELF_IDS, SHELF_SET
from .dual_cam_inventory import REQUIRED_MATCHING_PAIRS, MAX_PAIRS_PER_ATTEMPT, MAX_CAPTURE_DURATION_MS


CHECKS = ('same_frame_tm', 'same_frame_pose', 'action_consensus_parity',
          'browser_closed_borrow_return', 'duplicate_clients', 'camera_outage',
          'mqtt_outage', 'database_outage', 'reboot_reverification', 'load_budget',
          'cutover_rollback', 'runtime_three_pair_rebaseline', 'unknown_b1_hold', 'no_cloud_inventory',
          'bridge_crash_mid_action','roi_source_swap','range_2_3_and_3_4','stale_duplicate_events','runtime_exact_baseline')


# One bright fixed capture setting (flash on, duty 140, raw image) shared with the quick diagnostic preview; no lighting sweep.
CAPTURE_BOOST=LIVE_CAPTURE_BOOST
CAPTURE_DUTY=LIVE_CAPTURE_DUTY
MAX_SCAN_ATTEMPTS=3                # automatic count attempts per shelf before "Retry scan" is required
SCAN_BACKOFF_SECONDS=(5,15,30)     # wait before the next automatic attempt
SCAN_DEADLINE_SECONDS=90
COUNT_CHECK_INTERVAL_SECONDS=30

SCAN_MESSAGES={
    'COUNT_CHECK_SKIPPED':'Count check deferred while CCTV or another camera check is active.',
    'BACK_CAMERA_OFFLINE':'Count unavailable — back camera offline.',
    'FRONT_CAMERA_OFFLINE':'Count unavailable — front camera offline.',
    'BACK_CAMERA_NOT_CONFIGURED':'Count unavailable — back camera is not assigned to this shelf.',
    'FRONT_CAMERA_NOT_CONFIGURED':'Count unavailable — front camera is not assigned to this shelf.',
    'CAPTURE_FAILED':'Count unavailable — could not take the camera pictures; check the ESP32 connection.',
    'MISSING_CAMERA':'Count unavailable — a shelf camera did not respond.',
    'LOW_CONFIDENCE':'Low confidence — the cameras could not read the boxes clearly.',
    'M2_CONFLICT':'Unclear — recheck. Front and back cameras disagree about the shared slot.',
    'OCCUPANCY_CHANGED':'The boxes changed between pictures — keep hands clear.',
    'STALE_CAPTURE':'Camera pictures were too slow or out of date.',
    'QUIET_WINDOW':'Waiting for the rack to be clear — keep hands away.',
    'MODEL_FAILURE':'Count unavailable — the box-reading models are not responding.',
    'VALIDATION_TIMEOUT':'The scan took too long.',
    'LEASE_EXPIRED':'The scan was interrupted.',
    'MQTT_UNAVAILABLE':'MQTT is unavailable.',
}


def scan_message(code):
    return SCAN_MESSAGES.get(code) or 'Count unavailable — the pictures did not pass the safety checks.'


class ScanFailure(ValueError):
    """A scan/confirmation attempt that stopped for a specific, user-explainable reason."""
    def __init__(self, code, detail=''):
        super().__init__(detail or scan_message(code))
        self.code=code


def reason_code(exc):
    return getattr(exc,'code',None) or failure_code(exc)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def acceptance_code_paths(root):
    root=Path(root)
    return ([root/'VisionValidation_Module/camera_service.py',root/'index.html',root/'VisionValidation_Module/camera_manager.html']
        + sorted((root/'VisionValidation_Module/backend/src').glob('*.py'))
        + [root/'VisionValidation_Module/edge_service'/name for name in ('inference.cjs','worker.cjs','package-lock.json')])


def source_binding(source):
    if str(source).isdigit():
        return 'device:'+str(int(source))
    url = urllib.parse.urlsplit(source)
    if url.scheme not in {'rtsp', 'rtsps'} or not url.hostname:
        raise ValueError('Invalid camera source')
    # Credentials belong in the protected source environment, never ROI bindings.
    query='&'.join(part for part in url.query.split('&') if part and not any(
        word in urllib.parse.unquote_plus(part.split('=',1)[0]).lower()
        for word in ('user','pass','token','secret','auth','credential','key')))
    return f'{url.scheme}://{url.hostname.lower()}:{url.port or 554}{url.path}'+('?' + query if query else '')


def polygon(roi):
    if isinstance(roi, list):
        points = roi
    else:
        if roi.get('coordinate_space') == 'pixels':
            raise ValueError('Re-export normalized camera calibrations')
        points = roi.get('vertices') or [[roi['x_min'],roi['y_min']],[roi['x_max'],roi['y_min']],
                                        [roi['x_max'],roi['y_max']],[roi['x_min'],roi['y_max']]]
    if len(points)!=4 or any(len(p)!=2 or any(type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=1 for v in p) for p in points):
        raise ValueError('Invalid normalized ROI')
    turns=[]
    for i in range(4):
        a,b,c=points[i],points[(i+1)%4],points[(i+2)%4]
        turns.append((b[0]-a[0])*(c[1]-b[1])-(b[1]-a[1])*(c[0]-b[0]))
    if not (all(t>1e-9 for t in turns) or all(t< -1e-9 for t in turns)):
        raise ValueError('ROI must be a convex quadrilateral')
    return points


def inside(point, points):
    crosses=[(b[0]-a[0])*(point['y']-a[1])-(b[1]-a[1])*(point['x']-a[0])
             for a,b in zip(points, points[1:]+points[:1])]
    return all(x>=-1e-9 for x in crosses) or all(x<=1e-9 for x in crosses)


class ActionTracker:
    """Port of browser ROI membership, landmark ordering and dwell/withdrawal rules."""
    def __init__(self, role, rois):
        self.role=role;self.rois={s:polygon(r) for s,r in rois.items()}
        self.reset()

    def reset(self):
        self.last_frame=None
        self.states={s:dict(state='IDLE',start=None,seen=0,duration=0,fired=False,
            cooldown=0,history=[],presence=0,action='RECHECK',confidence=0) for s in self.rois}

    def update(self, persons, now):
        if self.last_frame is not None and (now<=self.last_frame or now-self.last_frame>1.5):
            self.reset()  # Never synthesize withdrawal from missing/stale frames.
        self.last_frame=now
        points=[]
        for person in persons:
            landmarks=person['landmarks']
            if self.role=='GRID_6_ROI':
                points += [landmarks[k] for k in (15,16) if landmarks[k]['visibility']>=.45]
            else:
                torso=[landmarks[k] for k in (11,12,23,24) if landmarks[k]['visibility']>=.25]
                if len(torso)>=2:points.append(dict(x=sum(p['x'] for p in torso)/len(torso),y=sum(p['y'] for p in torso)/len(torso),visibility=max(p['visibility'] for p in torso)))
                for k,threshold in ((15,.30),(16,.30),(13,.25),(14,.25),(11,.25),(12,.25),(23,.30),(24,.30),(0,.30),(7,.30),(8,.30)):
                    if landmarks[k]['visibility']>=threshold:points.append(landmarks[k])
                if person.get('personBox',{}).get('visibility',0)>=.45:points.append(person['personBox'])
        memberships={s:[] for s in self.rois};ambiguous=set();votes=[]
        for point in points:
            hits=[s for s,roi in self.rois.items() if inside(point,roi)]
            if len(hits)>1:ambiguous.update(hits)
            elif hits:memberships[hits[0]].append(point)
        for shelf,t in self.states.items():
            if shelf in ambiguous:
                t.update(state='IDLE',start=None,seen=0,duration=0,fired=False,history=[])
                continue
            point=next(iter(memberships[shelf]),None)
            emit=False
            if point:
                t['seen']=now;t['history']=(t['history']+[point])[-30:]
                if t['state']=='IDLE' and now>=t['cooldown']:
                    t.update(state='DWELLING',start=now,duration=0,fired=False)
                elif t['state']=='DWELLING':
                    t['duration']=now-t['start']
                    if t['duration']>=1.2:
                        t.update(state='INTENT_CONFIRMED',confidence=point['visibility'] or .88,action='RECHECK')
                        if self.role=='SIDE_PRESENCE':t.update(fired=True,presence=now);emit=True
                elif t['state']=='INTENT_CONFIRMED':
                    t['duration']=now-t['start']
                    if self.role=='SIDE_PRESENCE' and now-t['presence']>=2.5:
                        t.update(presence=now,confidence=point['visibility'] or t['confidence'] or .88);emit=True
            elif t['state'] in ('DWELLING','INTENT_CONFIRMED') and now-t['seen']>.3:
                if t['state']=='INTENT_CONFIRMED':
                    emit=not t['fired'];t['cooldown']=now+1.5
                t['state']='IDLE';t['start']=None
                # Preserve duration until vote copied below.
            if emit:votes.append(dict(shelf_id='ANY_SHELF' if self.role=='SIDE_PRESENCE' else shelf,
                expected_action=t['action'],dwell_duration_seconds=t['duration'],oks_confidence=t['confidence']))
            if not point and t['state']=='IDLE':t.update(duration=0,fired=False)
        return votes


def load_cameras(calibration_path, source_for, roi_path=None):
    """Validate the installed ROIs against the real camera sources; errors name the affected camera."""
    calibration_path=Path(calibration_path).resolve()
    if roi_path is not None and calibration_path!=Path(roi_path).resolve():raise ValueError('Native calibration must use the deployed local ROI store')
    try:raw=json.loads(calibration_path.read_text(encoding='utf8'))
    except FileNotFoundError:raise ValueError('No camera calibration is installed yet; calibrate the cameras and use Install to Bridge') from None
    from .local_operations import validate_rois
    calibration=validate_rois(raw)
    if len(calibration.get('cameras',[]))!=3 or calibration.get('version')!=1:raise ValueError('Exactly three camera calibrations required')
    cameras=[]
    for i,cid in enumerate(('CCTV_01','CCTV_02','CCTV_03')):
        rows=[c for c in calibration['cameras'] if c['cctv_id']==cid]
        if len(rows)!=1:raise ValueError('Three unique camera calibrations required')
        cam=rows[0];role='SIDE_PRESENCE' if i==2 else 'GRID_6_ROI';source=source_for(cid)
        if not source:raise ValueError(f'{cid}: no saved camera source; add it under camera administration')
        if cam['camera_role']!=role or cam['binding']!={'source':source_binding(source),'camera_role':role}:
            raise ValueError(f'{cid}: Camera source/role binding changed since calibration; recalibrate and use Install to Bridge')
        shelves=['RACK_ENTRY_ZONE'] if i==2 else list(SHELF_IDS)
        cameras.append(dict(id=cid,role=role,source=source,tracker=ActionTracker(role,{s:cam['rois'][s] for s in shelves})))
    if len({source_binding(c['source']) for c in cameras})!=3:raise ValueError('Camera sources must be distinct')
    return cameras


def load_service_config(config_path, manifest_path, acceptance_path, code_paths,roi_path=None):
    """Explicit-config loader. A supplied acceptance record is fully verified; none is required for normal operation."""
    runtime_enabled()  # Legacy release selection cannot restore cloud inventory authority.
    config_path=Path(config_path).resolve();base=config_path.parent
    config=json.loads(config_path.read_text());calibration_path=(base/config['calibration_file']).resolve()
    if config.get('version')!=1:raise ValueError('Unsupported service configuration version')
    cameras=load_cameras(calibration_path,lambda cid:os.environ.get(config['source_environment'][cid]),roi_path)
    reports=_accepted_reports(acceptance_path,manifest_path,calibration_path,code_paths) if acceptance_path is not None else []
    return cameras,reports


def _accepted_reports(acceptance_path,manifest_path,calibration_path,code_paths):
    acceptance=json.loads(Path(acceptance_path).read_text())
    if acceptance.get('inventory_contract')!='runtime-v1':raise ValueError('Runtime contract needs separate explicit acceptance')
    code_hash=hashlib.sha256(b''.join(Path(p).read_bytes() for p in code_paths)).hexdigest()
    if (acceptance.get('version')!=1
        or acceptance.get('operator_accepted') is not True or not acceptance.get('operator')
        or acceptance.get('code_sha256')!=code_hash or acceptance.get('manifest_sha256')!=digest(manifest_path)
        or acceptance.get('calibration_sha256')!=digest(calibration_path)
        or not 0<=time.time()-acceptance.get('accepted_at',0)<=7*86400
        or any(acceptance.get('checks',{}).get(k) is not True for k in CHECKS)):
        raise ValueError('SERVICE_CUTOVER_BLOCKED: acceptance missing, expired or mismatched')
    reports=[]
    for entry in acceptance.get('reports',[]):
        report_path=(Path(acceptance_path).resolve().parent/entry['path']).resolve()
        if digest(report_path)!=entry['sha256']:raise ValueError('Parity report changed')
        report=json.loads(report_path.read_text())
        if report.get('pass') is not True or not report.get('cases') or any(c.get('pass') is not True for c in report['cases']):raise ValueError('Parity has not passed')
        reports.append(report)
    if not all(any(any(c['kind']==kind for c in r['cases']) for r in reports) for kind in ('pair','pose')):
        raise ValueError('Both TM and pose parity reports required')
    if not {'Front','Back','pose'} <= {role for report in reports for role in report.get('models',{})}:
        raise ValueError('Parity reports must bind all three loaded models')
    return reports


PREVIEW_INFERENCE_TTL=5  # seconds a diagnostic preview may hold the model (one pair takes about 2.4 s)


class NativeClient:
    def __init__(self, root, run_id, manifest, node='node', kind='pair'):
        self.owner=EdgeShadow(root,run_id,'shadow',manifest=manifest,node=node)
        self.owner.worker_kind=kind
        self.lock=threading.Lock();self.replies=None;self.versions={};self.failures=0;self.accepted_versions=None
        self.restart_count=0;self.retry_at=0
        self.holder=None  # 'scan' or 'preview' while the model is in use

    def _load(self):
        if time.monotonic()<self.retry_at:raise RuntimeError('Native worker restart cooling down')
        self.replies=self.owner._spawn();ready=self.owner._reply(self.replies,60)
        if not ready.get('ready'):raise RuntimeError('Native models unavailable')
        versions=ready['versions']
        if self.accepted_versions is not None and versions!=self.accepted_versions:
            raise ValueError('Model/runtime changed after worker restart; restart the bridge to load the new models')
        self.versions=versions

    def _failed(self):
        self.failures+=1;self.restart_count+=1
        self.owner._stop_child();self.replies=None
        # Retry forever with bounded pacing instead of permanently exhausting a lifetime counter.
        if self.failures>=3:self.retry_at=time.monotonic()+min(30,2**min(self.failures-2,5))

    def warm(self):
        with self.lock:
            if self.replies is not None:return
            try:self._load()
            except Exception:
                if time.monotonic()>=self.retry_at:self._failed()
                raise

    def infer(self, job, ttl=15, preview=False):
        """Scans have priority: a diagnostic preview never waits (InferenceBusy), and a scan that finds a preview
        using the model waits for that one bounded preview instead of timing out."""
        started=time.monotonic()
        if preview:
            if not self.lock.acquire(blocking=False):raise InferenceBusy('Box-reading model in use by an automatic check')
            ttl=min(ttl,PREVIEW_INFERENCE_TTL)
        elif not self.lock.acquire(timeout=min(ttl,2)):
            if self.holder!='preview' or not self.lock.acquire(timeout=PREVIEW_INFERENCE_TTL+1):raise TimeoutError('Inference busy')
        self.holder='preview' if preview else 'scan'
        attempted=False
        try:
            if self.replies is None:
                if time.monotonic()<self.retry_at:raise RuntimeError('Native worker restart cooling down')
                attempted=True;self._load()
            if time.monotonic()-started>=ttl:raise TimeoutError('Late inference input')
            request=dict(job,id=str(uuid.uuid4()),startup_id=self.owner.startup_id,expires_at=time.time()+ttl-(time.monotonic()-started))
            attempted=True;self.owner.proc.stdin.write(json.dumps(request)+'\n');self.owner.proc.stdin.flush()
            reply=self.owner._reply(self.replies,max(.01,ttl-(time.monotonic()-started)))
            if reply.get('id')!=request['id'] or reply.get('startup_id')!=request['startup_id'] or 'error' in reply or time.time()>request['expires_at']:
                raise ValueError('Late, failed or mismatched native evidence')
            self.failures=0;self.retry_at=0
            return reply['result']
        except Exception:
            if attempted:self._failed()
            raise
        finally:
            self.holder=None
            self.lock.release()

    def close(self):
        self.owner.stop_event.set()
        with self.lock:self.owner._stop_child()


class AutonomousProcessor:
    """One shared native coordinator: counts each shelf, watches the three CCTVs, confirms actions with fresh ESP32 pairs."""

    def __init__(self, run_id, native, cameras, reports, request, capture, shelves, pending, invalidate, log, pose_native=None, cctv_busy=None,training_paused=None,training_capturing=None,preview_shelf=None):
        self.run_id=run_id;self.native=native;self.cameras=cameras;self.reports=reports
        self.request=request;self.capture=capture;self.shelves=shelves;self.pending=pending
        self.cctv_busy=cctv_busy if cctv_busy is not None else lambda:bool(self.pending())
        self.invalidate=invalidate;self.log=log;self.stop=threading.Event();self.threads=[]
        self.state='Starting';self.last_frames={};self.health={};self.retry_at={};self.metrics={}
        # Honest label: normal operation is not field-accepted unless a verified acceptance record was supplied.
        self.acceptance='NOT_FIELD_ACCEPTED'
        self.runtime: RuntimeInventory | None=None
        self.panel_observations=NativeObservations()
        self.models_loaded=False;self.model_issue=None;self.calibration_issue=None
        self.mqtt_ok=True
        self.pose_native=pose_native if pose_native is not None else native
        self.pose_loaded=False;self.pose_issue=None
        self.cctv_enabled=False;self.cctv_generation=0;self.cctv_lock=threading.RLock()
        self.cctv_recounts=set()
        self.scan={};self.attempts={};self.exhausted=set();self.scan_lock=threading.Lock()
        self.active_shelf=None
        self.count_check_due={}
        self.replacement_recovery=set();self.replacement_finished=None
        # training_paused(): shelf reserved for training capture (or None); only that shelf stops automatic checks.
        # training_capturing(): shelf whose capture has started; its CCTV votes are staged handling, not actions.
        self.training_paused=training_paused or (lambda:None);self.training_shelf=None
        self.training_capturing=training_capturing or (lambda:None)
        self.preview_shelf=preview_shelf or (lambda:None)  # shelf briefly reserved by a diagnostic preview

    # ---- setup / lifecycle -------------------------------------------------
    @property
    def setup_issue(self):
        return self.model_issue or self.calibration_issue

    def start(self,cctv=False):
        """Never raises for setup problems: they are reported through status while counting stays available.

        cctv=True switches CCTV on at boot through the same control boundary as the dashboard's Resume:
        previous action evidence is discarded and every shelf is recounted once all three cameras are live.
        """
        if self.runtime is not None:self.runtime.set_cctv_enabled(False)
        if cctv:self.set_cctv_enabled(True)
        for target in (self._boot,self._poses,self._validations,self._recover_workers):
            thread=threading.Thread(target=target,daemon=True,name='AutonomousEdge');self.threads.append(thread);thread.start()

    def _boot(self):
        while not self.stop.is_set():
            try:
                self.native.warm()
                for report in self.reports:
                    for role,version in report['models'].items():
                        if role in ('Front','Back') and version!=self.native.versions[role]:raise ValueError('Loaded model differs from accepted parity report')
                self.native.accepted_versions=json.loads(json.dumps(self.native.versions))
                self.model_issue=None;self.models_loaded=True
                if self.runtime is not None:self.runtime.models_ready=True
                return
            except Exception as exc:
                try:self.native.owner._stop_child()  # never leave a half-started worker behind between retries
                except Exception:pass
                self.model_issue='Box-reading models unavailable: '+(str(exc)[:160] or 'check the staged model files')
                self.log(self.run_id,'native_models_unavailable',code='MODEL_FAILURE')
                if self.stop.wait(10):return

    def _recover_workers(self):
        """Recreate crashed count workers even when the shelf's scan retry budget is exhausted."""
        while not self.stop.wait(1):
            if not self.models_loaded or self.native.replies is not None:continue
            try:self.native.warm()
            except Exception:self.log(self.run_id,'native_worker_restarting',code='WORKER_UNAVAILABLE')

    def set_calibration_issue(self,message):
        self.calibration_issue=message;self.cameras=[];self.last_frames.clear();self.health.clear()

    def reload_calibration(self,cameras):
        """Swap in newly validated camera ROIs; caller has already invalidated runtime evidence."""
        for camera in cameras:camera['tracker'].reset()
        self.cameras=cameras;self.last_frames.clear();self.health.clear();self.calibration_issue=None
        self.panel_observations.clear()
        self.reset_scan_budget()

    def set_cctv_enabled(self,enabled):
        """An explicit control boundary: retain displayed counts, discard all previous action evidence."""
        with self.cctv_lock:
            if self.cctv_enabled==enabled:return
            self.cctv_generation+=1
            self.cctv_enabled=enabled
            self.cctv_recounts=set(SHELF_IDS) if enabled else set()
            self.last_frames.clear();self.health.clear()
            for cam in self.cameras:cam['tracker'].reset()
            if self.runtime is not None:self.runtime.set_cctv_enabled(enabled)
            self.invalidate('CCTV started' if enabled else 'CCTV stopped',preserve_counts=True,disable_actions=True)
            self.reset_scan_budget()
            if not enabled:
                # Stop movement inference; the independent box-count process stays running.
                self.pose_native.close()
                self.pose_native.owner.stop_event.clear()
                self.pose_loaded=False;self.pose_issue=None

    # ---- status ------------------------------------------------------------
    def live_camera_ids(self):
        """Calibrated CCTVs with a frame in the last 1.5 s (the freshness rule actions use)."""
        now=time.time()
        return [c['id'] for c in self.cameras if self.health.get(c['id'],{}).get('state')=='Online' and
            0<=now-self.health[c['id']]['at']<1.5]

    def cameras_ready(self):
        return self.cctv_enabled and bool(self.cameras) and len(self.live_camera_ids())==len(self.cameras)

    def actions_blocker(self):
        """Why BORROW/RETURN confirmation is paused right now, or None. Counting is independent of this."""
        if self.model_issue:return self.model_issue
        if not self.models_loaded:return 'Loading box-reading models'
        if not self.cctv_enabled:return 'CCTV stopped; last counts retained'
        if self.pose_issue:return self.pose_issue
        if self.calibration_issue:return self.calibration_issue
        if not self.cameras:return 'Install camera calibration'
        if not self.mqtt_ok:return 'MQTT unavailable'
        live=self.live_camera_ids();down=[c['id'] for c in self.cameras if c['id'] not in live]
        return ('Waiting for live video from '+', '.join(down)) if down else None

    def shelf_status(self,sid,runtime=None):
        """Per-shelf view: count scan progress and, separately, whether actions can be confirmed."""
        with self.scan_lock:
            scan=dict(self.scan.get(sid) or dict(state='Waiting',message='Waiting to scan this shelf',reason=None,attempt=0))
            active=self.active_shelf
        if scan['state']=='Waiting' and not scan.get('reason'):
            if not self.models_loaded:scan['message']='Waiting for the box-reading models to load'
            elif active and active!=sid:scan['message']=f'Waiting for {active} to finish its camera check'
        paused=self.actions_blocker()
        if isinstance(self.runtime,RuntimeInventory) and sid in self.runtime.maintenance:
            scan.update(state='Maintenance',message='Held by an unfinished wired-replacement record; recover it with the previous bridge version')
            paused='Replacement maintenance'
        if sid==self.training_shelf:
            scan.update(state='Waiting',message='Training capture on this shelf; it is counted fresh afterwards')
            paused='Training capture on this shelf'
        if paused is None and runtime is not None and not runtime.get('action_capable'):
            paused='A fresh complete count is required after the last count check' if runtime.get('count_check_unverified') else (
                'Waiting for a fresh count with all cameras live' if runtime.get('state')!='Ready' else 'Recounting now that all cameras are live')
        return dict(scan=scan,actions_ready=paused is None,actions_paused_reason=paused,max_attempts=MAX_SCAN_ATTEMPTS)

    def snapshot(self):
        blocker=self.actions_blocker()
        state='Setup needed' if self.model_issue else 'Starting' if not self.models_loaded else 'Running' if blocker is None else 'Counting; actions paused'
        if self.state!='Stopped':self.state=state
        return dict(mode='service',authoritative_processor='service',service_cutover_available=True,
            inventory_mode='runtime' if self.runtime is not None else 'persistent',
            startup_id=self.run_id,state=self.state,setup_issue=self.setup_issue,actions_paused_reason=blocker,
            cameras=dict(self.health),camera_ids=[c['id'] for c in self.cameras],model_versions=self.native.versions,
            metrics=dict(self.metrics),restart_count=self.native.restart_count,cctv_enabled=self.cctv_enabled,
            cctv_live=self.live_camera_ids() if self.cctv_enabled else [],
            movement_models_ready=self.pose_loaded,movement_issue=self.pose_issue,authorizes_write=False,
            acceptance=self.acceptance,release_blockers=[] if self.acceptance=='ACCEPTED' else ['REAL_CAMERA_ACCEPTANCE'])

    def _set_scan(self,sid,state,code=None,message=None,attempt=None):
        with self.scan_lock:
            previous=self.scan.get(sid,{})
            self.scan[sid]=dict(state=state,reason=code,attempt=previous.get('attempt',0) if attempt is None else attempt,
                message=message or scan_message(code) if code else (message or ''),updated=time.time())

    def reset_scan_budget(self,sid=None):
        """Fresh evidence boundary (retry request, camera/MQTT/calibration change): count scans may try again."""
        for shelf in ([sid] if sid else list(SHELF_IDS)):
            if sid is None and isinstance(self.runtime,RuntimeInventory) and shelf in self.runtime.maintenance:continue
            self.attempts.pop(shelf,None);self.exhausted.discard(shelf);self.retry_at.pop(shelf,None)
            with self.scan_lock:
                if self.scan.get(shelf,{}).get('state') in ('Needs retry','Retrying'):self.scan.pop(shelf,None)

    # ---- CCTV tracking -----------------------------------------------------
    def _frame_failed(self,cam,reason,now):
        previous=self.health.get(cam['id'],{})
        down_since=previous.get('down_since',now)
        duration_ms=max(0,int((now-down_since)*1000))
        self.log(self.run_id,'cctv_frame_failure',camera_id=cam['id'],reason=reason,duration_ms=duration_ms)
        invalidated=self.runtime.camera(cam['id'],now,False) if self.runtime is not None else False
        cam['tracker'].reset()  # Other cameras' trajectories and evidence remain untouched.
        if invalidated:self.invalidate('CCTV outage exceeded five seconds')
        self.health[cam['id']]={'state':'Offline','at':now,'down_since':down_since,'reason':reason,'duration_ms':duration_ms}

    def _poses(self):
        while not self.stop.is_set():
            if not self.cctv_enabled:
                self.stop.wait(.2);continue
            if not self.pose_loaded:
                try:
                    with self.cctv_lock:
                        if not self.cctv_enabled:continue
                        self.pose_native.warm()
                        for report in self.reports:
                            version=report['models'].get('pose')
                            if version is not None and version!=self.pose_native.versions.get('pose'):raise ValueError('Pose model differs from parity report')
                        self.pose_native.accepted_versions=json.loads(json.dumps(self.pose_native.versions))
                        self.pose_loaded=True;self.pose_issue=None
                except Exception:
                    self.pose_issue='Movement model unavailable; counting continues'
                    self.stop.wait(2);continue
            generation=self.cctv_generation
            for cam in list(self.cameras):
                if self.stop.is_set():return
                if not self.cctv_enabled or generation!=self.cctv_generation:break
                reason='FRAME_UNAVAILABLE'
                try:
                    frame=self.capture(cam['source']);now=time.time()
                    if frame.get('status')!='success':raise ValueError('Camera unavailable')
                    identity=frame.get('seq')
                    if identity==self.last_frames.get(cam['id']):
                        if now-self.health.get(cam['id'],{}).get('at',0)>1.5:
                            reason='STALE_FRAME';raise ValueError('Stale CCTV frame')
                        continue
                    self.last_frames[cam['id']]=identity
                    reason='POSE_INFERENCE_FAILED'
                    with self.cctv_lock:
                        if not self.cctv_enabled or generation!=self.cctv_generation:break
                        result=self.pose_native.infer({'kind':'pose','image':frame['image']},ttl=1.5)
                        previous_health=self.health.get(cam['id'],{})
                        if previous_health.get('state')=='Online' and now-previous_health['at']>=1.5:
                            self.health[cam['id']]={**previous_health,'down_since':previous_health['at']}
                            self._frame_failed(cam,'FRAME_GAP',now)
                            if now-previous_health['at']>5:self.invalidate('CCTV outage exceeded five seconds')
                        self.metrics['pose']=result['metrics'];self.health[cam['id']]={'state':'Online','at':now}
                        votes=cam['tracker'].update(result['persons'],now)
                        if self.runtime is not None:
                            interacting=any(t['seen']==now for t in cam['tracker'].states.values())
                            self.runtime.camera(cam['id'],now,True,interacting)
                        training=self.training_capturing()
                        for vote in votes:
                            if training is not None and vote.get('shelf_id')==training:continue
                            try:
                                self.request('/esp32_trigger',dict(vote,source='CCTV',camera_id=cam['id'],camera_role=cam['role'],
                                    trigger_id=str(uuid.uuid4()),detected_at=now))
                            except ValueError:
                                self.log(self.run_id,'native_vote_held',code='SHELF_NOT_READY')
                except Exception:
                    with self.cctv_lock:
                        if self.cctv_enabled and generation==self.cctv_generation:self._frame_failed(cam,reason,time.time())
            self.stop.wait(.1)

    # ---- one scan / confirmation attempt ----------------------------------
    def validate(self, shelf, trigger=None, *, count_check=False):
        """Take at most MAX_PAIRS_PER_ATTEMPT fresh pairs; submit once REQUIRED_MATCHING_PAIRS consecutive pairs agree."""
        sid=shelf['shelf_id']
        if isinstance(self.runtime,RuntimeInventory) and not self.runtime.maintenance_allowed(sid):raise ValueError('Replacement maintenance')
        source='COUNT_CHECK' if count_check else trigger.get('source','CCTV') if trigger else 'STARTUP'
        action=trigger['expected_action'] if trigger else 'RECHECK';request_id=str(uuid.uuid4());token=None
        if not shelf.get('ip_address'):raise ScanFailure('BACK_CAMERA_NOT_CONFIGURED')
        if not shelf.get('secondary_ip_address'):raise ScanFailure('FRONT_CAMERA_NOT_CONFIGURED')
        try:
            token=self.request('/validation_begin',dict(shelf_id=sid,source=source,request_id=request_id))['token']
            if not token:
                if count_check:raise ScanFailure('COUNT_CHECK_SKIPPED')
                raise ValueError('Lease unavailable')
            if trigger and not self.request('/cctv_trigger_claim',dict(shelf_id=sid,trigger_id=trigger['trigger_id'],validation_token=token))['claimed']:
                raise ValueError('Trigger not claimable')
            deadline=time.monotonic()+SCAN_DEADLINE_SECONDS
            if trigger is not None and isinstance(trigger.get('timestamp'),(int,float)):
                now=self.runtime.clock() if self.runtime is not None else time.time()
                deadline=min(deadline,time.monotonic()+max(0,trigger['timestamp']+60-now))
            runtime=self.runtime
            runtime_version=runtime.snapshot(sid)['version'] if runtime is not None else None
            if runtime is not None:
                def await_environment():
                    while True:
                        try:runtime.environment_ready(require_cameras=trigger is not None);break
                        except ValueError:
                            if count_check:raise ScanFailure('COUNT_CHECK_SKIPPED') from None
                            if trigger is None:
                                self._set_scan(sid,'Waiting','QUIET_WINDOW',runtime.environment_wait_reason(require_cameras=False))
                            if self.stop.is_set() or time.monotonic()>=deadline:
                                if trigger is None:raise ScanFailure('QUIET_WINDOW') from None
                                raise TimeoutError('Quiet/healthy baseline window unavailable') from None
                            if not self.request('/validation_renew',dict(shelf_id=sid,token=token))['renewed']:raise ValueError('Lease expired')
                            self.stop.wait(.2)
                await_environment()
            trail=[];failure='VALIDATION_REJECTED';last=None
            movement_generation=runtime.movement_generation() if runtime is not None and trigger is not None else None
            for number in range(MAX_PAIRS_PER_ATTEMPT):
                if trigger is not None and runtime is not None:
                    await_environment()
                    current=runtime.movement_generation()
                    if current!=movement_generation:trail=[];movement_generation=current
                if count_check and (self.cctv_busy() or runtime.environment_wait_reason(require_cameras=False)):
                    raise ScanFailure('COUNT_CHECK_SKIPPED')
                if self.stop.is_set() or time.monotonic()>deadline:raise TimeoutError('Validation time limit')
                if not self.request('/validation_renew',dict(shelf_id=sid,token=token))['renewed']:raise ValueError('Lease expired')
                if not count_check:self.request('/validation_progress',dict(shelf_id=sid,validation_token=token,run_id=self.run_id,stage='Native paired capture',capture_number=number+1,boost=CAPTURE_BOOST,duty=CAPTURE_DUTY))
                if trigger is None and not count_check:self._set_scan(sid,'Scanning',message=f'Taking picture pair {number+1} of up to {MAX_PAIRS_PER_ATTEMPT}')
                query=urllib.parse.urlencode(dict(tm_raw=1,ip=shelf['ip_address'],ip2=shelf['secondary_ip_address'],shelf_id=sid,boost=CAPTURE_BOOST,duty=CAPTURE_DUTY))
                try:preview=self.request('/esp32_preview?'+query,None)
                except ValueError:preview=None;failure='CAPTURE_FAILED';trail=[]
                if preview is not None and not preview.get('image_back'):failure='BACK_CAMERA_OFFLINE';trail=[];preview=None
                elif preview is not None and not preview.get('image_front'):failure='FRONT_CAMERA_OFFLINE';trail=[];preview=None
                if preview is not None:
                    result=self.native.infer(dict(kind='pair',image_back=preview['image_back'],image_front=preview['image_front'],boost=CAPTURE_BOOST))
                    self.metrics['pair']=result['metrics'];candidate=result['candidate']
                    if trigger is not None and runtime is not None and runtime.movement_generation()!=movement_generation:
                        trail=[];failure='STALE_CAPTURE'
                        if number<MAX_PAIRS_PER_ATTEMPT-1 and self.stop.wait(2):raise RuntimeError('Stopping')
                        continue
                    # Display only (no inventory effect): the panel shows the latest picture of each shelf.
                    self.panel_observations.put(sid,preview,candidate,source='count_check' if count_check else 'scan')
                    if runtime is not None:runtime.observe(sid,candidate,preview['capture'])
                    valid_range=runtime is not None and candidate.get('hold')=='UNKNOWN_B1'
                    if candidate.get('hold') and not valid_range:
                        failure=candidate['hold'];trail=[]
                    elif preview['capture']['duration_ms']>MAX_CAPTURE_DURATION_MS:
                        failure='STALE_CAPTURE';trail=[]
                    else:
                        sample=dict(front_class=candidate['front_class'],back_class=candidate['back_class'],
                            front_confidence=candidate['front_confidence'],back_confidence=candidate['back_confidence'],capture=preview['capture'])
                        if trail and trail[-1][0]!=candidate['slots']:failure='OCCUPANCY_CHANGED';trail=[]
                        trail.append((candidate['slots'],sample,preview,candidate))
                        if len(trail)>=REQUIRED_MATCHING_PAIRS:break
                if number<MAX_PAIRS_PER_ATTEMPT-1 and self.stop.wait(2):raise RuntimeError('Stopping')
            if len(trail)<REQUIRED_MATCHING_PAIRS:raise ScanFailure(failure)
            samples=[row[1] for row in trail[-REQUIRED_MATCHING_PAIRS:]];preview,candidate=trail[-1][2],trail[-1][3]
            if trigger is not None and runtime is not None:
                await_environment()
                if runtime.movement_generation()!=movement_generation:raise ScanFailure('STALE_CAPTURE')
            if self.stop.is_set() or (trigger is not None and not self.cameras_ready()):raise ValueError('CCTV health lost before validation')
            if count_check and self.cctv_busy():raise ScanFailure('COUNT_CHECK_SKIPPED')
            return self.request('/esp32_validate',dict(shelf_id=sid,validation_token=token,source=source,
                trigger_id=trigger['trigger_id'] if trigger else None,expected_action=action,ip=shelf['ip_address'],ip2=shelf['secondary_ip_address'],
                image=preview['image'],image_back=preview['image_back'],image_front=preview['image_front'],
                predicted_box_count=candidate.get('total'),dual_cam_evidence={'version':'stacked-visibility-v4-x01','samples':samples},
                run_id=self.run_id,expected_version=runtime_version,runtime_event_id='baseline:'+request_id,
                tm_confidence=min(candidate['front_confidence'],candidate['back_confidence']),boost=CAPTURE_BOOST))
        except Exception as exc:
            if token and not count_check:
                try:
                    progress=self.request('/validation_progress',dict(shelf_id=sid,validation_token=token,run_id=self.run_id,trigger_id=trigger.get('trigger_id') if trigger else None,
                        failed=True,failure_code=reason_code(exc),stage=failure_code(exc)+': native validation held; fresh verification required'))
                    if isinstance(progress,dict) and progress.get('status')=='retry_pending':return progress
                except Exception:pass
            raise
        finally:
            if token:self.request('/validation_end',dict(shelf_id=sid,token=token,source=source,trigger_id=trigger['trigger_id'] if trigger else None))
            else:self.request('/validation_cancel',dict(request_id=request_id))

    def _attempt(self,shelf,trigger):
        sid=shelf['shelf_id']
        with self.scan_lock:self.active_shelf=sid
        if trigger is None:
            n=self.attempts[sid]=self.attempts.get(sid,0)+1
            self._set_scan(sid,'Scanning',message='Scanning: keep hands clear of the rack',attempt=n)
        try:
            recovery=sid in self.replacement_recovery and trigger is None
            if recovery:
                with self.runtime.guard:self.runtime.recovery_threads[sid]=threading.get_ident()
            try:result=self.validate(shelf,trigger)
            finally:
                if recovery:
                    with self.runtime.guard:self.runtime.recovery_threads.pop(sid,None)
            if recovery and isinstance(result,dict) and result.get('status')=='success':
                self.replacement_finished(sid)
                self.replacement_recovery.discard(sid)
            if isinstance(result,dict) and result.get('status')=='retry_pending':
                self._set_scan(sid,'Waiting',message='Nonterminal validation; waiting for one fresh retry')
                return
            if isinstance(result,dict) and result.get('code')=='SIDE_PRESENCE_DEFERRED':
                self.attempts[sid]=max(0,self.attempts.get(sid,1)-1)
                self.retry_at[sid]=time.monotonic()+2
                self._set_scan(sid,'Waiting','SIDE_PRESENCE_DEFERRED','Changed count deferred until the side zone is clear for 2 s, then two new matching pairs are taken.',attempt=self.attempts[sid])
                return
            self.count_check_due[sid]=time.monotonic()+COUNT_CHECK_INTERVAL_SECONDS
            if trigger is None and self.cameras_ready():self.cctv_recounts.discard(sid)
            self.attempts.pop(sid,None);self.exhausted.discard(sid);self.retry_at.pop(sid,None)
            self._set_scan(sid,'Count ready',message='Count ready',attempt=0)
        except Exception as exc:
            if isinstance(self.runtime,RuntimeInventory) and sid in self.runtime.maintenance and not recovery:
                self._set_scan(sid,'Maintenance',message='Replacement maintenance; stale attempt discarded')
                return
            code=reason_code(exc)
            self.log(self.run_id,'native_validation_held',shelf_id=sid,code='VALIDATION_PENDING')
            if trigger is not None:
                # A held action is never retried as that action; the shelf is recounted through the normal scan path.
                self.attempts.pop(sid,None);self.exhausted.discard(sid);self.retry_at.pop(sid,None)
                self._set_scan(sid,'Waiting',code,'Action held: '+scan_message(code)+' The shelf will be recounted when the rack is quiet.',attempt=0)
                return
            n=self.attempts.get(sid,1)
            if n>=MAX_SCAN_ATTEMPTS:
                self.exhausted.add(sid)
                self._set_scan(sid,'Needs retry',code,scan_message(code)+' Use Retry scan.',attempt=n)
            else:
                self.retry_at[sid]=time.monotonic()+SCAN_BACKOFF_SECONDS[min(n-1,len(SCAN_BACKOFF_SECONDS)-1)]
                self._set_scan(sid,'Retrying',code,scan_message(code)+f' Retrying (attempt {n} of {MAX_SCAN_ATTEMPTS}).',attempt=n)
        finally:
            with self.scan_lock:self.active_shelf=None

    def _count_check(self,shelf):
        sid=shelf['shelf_id'];version=self.runtime.snapshot(sid)['version']
        self.count_check_due[sid]=time.monotonic()+COUNT_CHECK_INTERVAL_SECONDS
        with self.scan_lock:self.active_shelf=sid
        try:
            result=self.validate(shelf,count_check=True)
            if isinstance(result,dict) and result.get('code')=='SIDE_PRESENCE_DEFERRED':
                self.count_check_due[sid]=time.monotonic()+2
        except Exception as exc:
            code=reason_code(exc)
            if code=='COUNT_CHECK_SKIPPED':self.count_check_due[sid]=time.monotonic()+2
            message=(self.runtime.environment_wait_reason(require_cameras=False) if code=='COUNT_CHECK_SKIPPED' else None) or scan_message(code)
            self.runtime.count_check_failed(sid,code,message,version)
            self.log(self.run_id,'count_check_failed',shelf_id=sid,code=code)
        finally:
            with self.scan_lock:self.active_shelf=None

    def _validations(self):
        while not self.stop.is_set():
            try:
                self.training_shelf=self.training_paused()
                if not self.models_loaded:
                    self.stop.wait(1);continue
                info=self.request('/shelf_readiness',None)
                self.mqtt_ok=info.get('mqtt_connected') is not False
                statuses=info['shelves'];shelves={s['shelf_id']:s for s in self.shelves()}
                blocker=self.actions_blocker();now=time.monotonic()
                actions=[];scans=[];checks=[];pending=self.pending()
                for sid,status in statuses.items():
                    shelf=shelves.get(sid)
                    if isinstance(self.runtime,RuntimeInventory) and sid in self.runtime.maintenance:
                        if sid in self.replacement_recovery and shelf and sid not in self.exhausted and now>=self.retry_at.get(sid,0):
                            from .camera_slots import SLOTS
                            pair=[slot for slot in SLOTS if slot['shelf_id']==sid]
                            scans.append((dict(shelf,ip_address=pair[0]['ip_address'],secondary_ip_address=pair[1]['ip_address']),None))
                        continue
                    if shelf is None or sid==self.training_shelf or sid==self.preview_shelf():continue
                    runtime=status.get('runtime') or {}
                    ready=status['state']=='Ready'
                    if ready and self.runtime is not None:
                        due=self.count_check_due.setdefault(sid,now+COUNT_CHECK_INTERVAL_SECONDS)
                        if now>=due and not self.cctv_busy() and self.runtime.count_recheck_ready(sid):checks.append(shelf)
                    if not shelf.get('ip_address') or not shelf.get('secondary_ip_address'):
                        missing='BACK_CAMERA_NOT_CONFIGURED' if not shelf.get('ip_address') else 'FRONT_CAMERA_NOT_CONFIGURED'
                        self._set_scan(sid,'Unavailable',missing);continue
                    if ready and runtime.get('action_capable'):
                        head=next((t for t in self.pending() if t['shelf_id']==sid),None)
                        if blocker is None and head and not head.get('claimed') and time.time()>=head.get('retry_after',0):actions.append((shelf,head))
                        continue
                    if runtime.get('side_count_deferred') and (ready or not self.runtime.count_recheck_ready(sid)):continue
                    if ready and runtime.get('count_check_unverified') and not pending and sid not in self.cctv_recounts:continue
                    # Counts need only models and the ESP32 HTTP pair; a count taken while CCTV/MQTT was down is redone once they are live.
                    if ready and blocker is not None and not (sid in self.cctv_recounts and self.cameras_ready()):continue
                    if status['state'] in ('Starting','Offline','Held') or ready:
                        if sid in self.exhausted or now<self.retry_at.get(sid,0):continue
                        scans.append((shelf,None))
                work=(actions+scans)[:1]
                for shelf,trigger in work:self._attempt(shelf,trigger)
                if not work and checks:
                    shelf=min(checks,key=lambda item:self.count_check_due[item['shelf_id']])
                    self._count_check(shelf);work=[(shelf,None)]
            except Exception:
                work=[];self.log(self.run_id,'native_scheduler_error',code='VALIDATION_PENDING')
            self.stop.wait(.2 if work else 1)  # next shelf starts right after one finishes; idle polling stays at 1 s

    def close(self):
        if self.runtime is not None:self.runtime.models_ready=False;self.runtime.invalidate()
        self.stop.set();self.native.close()
        if self.pose_native is not self.native:self.pose_native.close()
        for thread in self.threads:thread.join(timeout=10)
        self.state='Stopped'
