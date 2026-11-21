"""Browser-independent orchestration; all inventory writes still use bridge guards.

Activation requires explicit local configuration, parity reports and operator
acceptance. No credentials or acquisition URLs are passed to the Node worker.
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


CHECKS = ('same_frame_tm', 'same_frame_pose', 'action_consensus_parity',
          'browser_closed_borrow_return', 'duplicate_clients', 'camera_outage',
          'mqtt_outage', 'database_outage', 'reboot_reverification', 'load_budget',
          'cutover_rollback')


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
    return f'{url.scheme}://{url.hostname.lower()}:{url.port or 554}{url.path}'+('?' + url.query if url.query else '')


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


def trajectory_action(history):
    if len(history)<5:return 'BORROW'
    dx=history[-1]['x']-history[0]['x'];dy=history[-1]['y']-history[0]['y'];mid=len(history)//2
    shift=sum(p['y'] for p in history[mid:])/len(history[mid:])-sum(p['y'] for p in history[:mid])/mid
    return 'RETURN' if dy>.03 and shift>.015 and abs(dy)>=abs(dx)*.4 else 'BORROW'


class ActionTracker:
    """Port of browser ROI membership, landmark ordering and dwell/withdrawal rules."""
    def __init__(self, role, rois):
        self.role=role;self.rois={s:polygon(r) for s,r in rois.items()}
        self.reset()

    def reset(self):
        self.last_frame=None
        self.states={s:dict(state='IDLE',start=None,seen=0,duration=0,fired=False,
            cooldown=0,history=[],presence=0,action='BORROW',confidence=0) for s in self.rois}

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
                        t.update(state='INTENT_CONFIRMED',confidence=point['visibility'] or .88,action=trajectory_action(t['history']))
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


def load_service_config(config_path, manifest_path, acceptance_path, code_paths):
    """Local-only activation gate, bound to code, models, calibrations and reports."""
    config_path=Path(config_path).resolve();base=config_path.parent
    config=json.loads(config_path.read_text());calibration_path=(base/config['calibration_file']).resolve()
    calibration=json.loads(calibration_path.read_text())
    if len(calibration.get('cameras',[]))!=3:raise ValueError('Exactly three camera calibrations required')
    acceptance=json.loads(Path(acceptance_path).read_text())
    code_hash=hashlib.sha256(b''.join(Path(p).read_bytes() for p in code_paths)).hexdigest()
    if (config.get('version')!=1 or calibration.get('version')!=1 or acceptance.get('version')!=1
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
    cameras=[]
    for i,cid in enumerate(('CCTV_01','CCTV_02','CCTV_03')):
        rows=[c for c in calibration['cameras'] if c['cctv_id']==cid]
        if len(rows)!=1:raise ValueError('Three unique camera calibrations required')
        cam=rows[0];role='SIDE_PRESENCE' if i==2 else 'GRID_6_ROI';source=os.environ[config['source_environment'][cid]]
        if cam['camera_role']!=role or cam['binding']!={'source':source_binding(source),'camera_role':role}:
            raise ValueError('Camera source/role binding changed')
        shelves=['RACK_ENTRY_ZONE'] if i==2 else [f'SHELF_R1_C{j}' for j in range(1,7)]
        tracker=ActionTracker(role,{s:cam['rois'][s] for s in shelves})
        cameras.append(dict(id=cid,role=role,source=source,tracker=tracker))
    if len({source_binding(c['source']) for c in cameras})!=3:raise ValueError('Camera sources must be distinct')
    return cameras,reports


class NativeClient:
    def __init__(self, root, run_id, manifest, node='node'):
        self.owner=EdgeShadow(root,run_id,'shadow',manifest=manifest,node=node)
        self.lock=threading.Lock();self.replies=None;self.versions={};self.failures=0;self.accepted_versions=None

    def warm(self):
        with self.lock:
            self.replies=self.owner._spawn();ready=self.owner._reply(self.replies,60)
            if not ready.get('ready'):raise RuntimeError('Native models unavailable')
            self.versions=ready['versions']

    def infer(self, job, ttl=15):
        started=time.monotonic()
        if not self.lock.acquire(timeout=min(ttl,2)):raise TimeoutError('Inference busy')
        try:
            if self.replies is None:
                if self.failures>=3:raise RuntimeError('Native worker restart budget exhausted')
                self.replies=self.owner._spawn();ready=self.owner._reply(self.replies,60)
                if not ready.get('ready'):raise RuntimeError('Native models unavailable')
                self.versions=ready['versions']
                if self.accepted_versions is not None and self.versions!=self.accepted_versions:
                    raise ValueError('Model/runtime changed after worker restart; new acceptance required')
            if time.monotonic()-started>=ttl:raise TimeoutError('Late inference input')
            request=dict(job,id=str(uuid.uuid4()),startup_id=self.owner.startup_id,expires_at=time.time()+ttl-(time.monotonic()-started))
            self.owner.proc.stdin.write(json.dumps(request)+'\n');self.owner.proc.stdin.flush()
            reply=self.owner._reply(self.replies,max(.01,ttl-(time.monotonic()-started)))
            if reply.get('id')!=request['id'] or reply.get('startup_id')!=request['startup_id'] or 'error' in reply or time.time()>request['expires_at']:
                raise ValueError('Late, failed or mismatched native evidence')
            return reply['result']
        except Exception:
            self.failures+=1;self.owner._stop_child();self.replies=None;raise
        finally:self.lock.release()

    def close(self):
        self.owner.stop_event.set()
        with self.lock:self.owner._stop_child()


class AutonomousProcessor:
    def __init__(self, run_id, native, cameras, reports, request, capture, shelves, pending, invalidate, log):
        self.run_id=run_id;self.native=native;self.cameras=cameras;self.reports=reports
        self.request=request;self.capture=capture;self.shelves=shelves;self.pending=pending
        self.invalidate=invalidate;self.log=log;self.stop=threading.Event();self.threads=[]
        self.state='Starting';self.last_frames={};self.health={};self.retry_at={};self.metrics={}

    def start(self):
        # Warm up and verify parity was produced by these exact loaded model weights.
        self.native.warm()
        for report in self.reports:
            for role,version in report['models'].items():
                if role in ('Front','Back','pose') and version!=self.native.versions[role]:raise ValueError('Loaded model differs from accepted parity report')
        self.native.accepted_versions=json.loads(json.dumps(self.native.versions))
        for target in (self._poses,self._validations):
            thread=threading.Thread(target=target,daemon=True,name='AutonomousEdge');self.threads.append(thread);thread.start()

    def snapshot(self):
        return dict(mode='service',authoritative_processor='service',service_cutover_available=True,
            startup_id=self.run_id,state=self.state,cameras=dict(self.health),model_versions=self.native.versions,
            metrics=dict(self.metrics),restart_count=self.native.failures,authorizes_write=False)

    def cameras_ready(self):
        return all(self.health.get(c['id'],{}).get('state')=='Online' and
            0<=time.time()-self.health[c['id']]['at']<1.5 for c in self.cameras)

    def _poses(self):
        while not self.stop.is_set():
            for cam in self.cameras:
                if self.stop.is_set():return
                try:
                    frame=self.capture(cam['source']);now=time.time()
                    if frame.get('status')!='success':raise ValueError('Camera unavailable')
                    identity=frame.get('seq')
                    if identity==self.last_frames.get(cam['id']):
                        if now-self.health.get(cam['id'],{}).get('at',0)>1.5:raise ValueError('Stale CCTV frame')
                        continue
                    self.last_frames[cam['id']]=identity
                    result=self.native.infer({'kind':'pose','image':frame['image']},ttl=1.5)
                    self.metrics['pose']=result['metrics'];self.health[cam['id']]={'state':'Online','at':now}
                    for vote in cam['tracker'].update(result['persons'],now):
                        try:
                            self.request('/esp32_trigger',dict(vote,source='CCTV',camera_id=cam['id'],camera_role=cam['role'],
                                trigger_id=str(uuid.uuid4()),detected_at=now))
                        except ValueError:
                            self.log(self.run_id,'native_vote_held',code='SHELF_NOT_READY')
                except Exception:
                    cam['tracker'].reset();self.invalidate();self.health[cam['id']]={'state':'Offline','at':time.time()}
            self.stop.wait(.1)

    def validate(self, shelf, trigger=None):
        sid=shelf['shelf_id'];source=trigger.get('source','CCTV') if trigger else 'STARTUP'
        action=trigger['expected_action'] if trigger else 'RECHECK';request_id=str(uuid.uuid4());token=None
        try:
            token=self.request('/validation_begin',dict(shelf_id=sid,source=source,request_id=request_id))['token']
            if not token:raise ValueError('Lease unavailable')
            if trigger and not self.request('/cctv_trigger_claim',dict(shelf_id=sid,trigger_id=trigger['trigger_id'],validation_token=token))['claimed']:
                raise ValueError('Trigger not claimable')
            deadline=time.monotonic()+90;accepted=None
            for boost,duty in (('off',90),('auto',135),('high',185)):
                samples=[];slots=None
                for number in range(3):
                    if self.stop.is_set() or time.monotonic()>deadline:raise TimeoutError('Validation time limit')
                    if not self.request('/validation_renew',dict(shelf_id=sid,token=token))['renewed']:raise ValueError('Lease expired')
                    self.request('/validation_progress',dict(shelf_id=sid,validation_token=token,run_id=self.run_id,stage='Native paired capture',capture_number=number+1,boost=boost,duty=duty))
                    query=urllib.parse.urlencode(dict(tm_raw=1,ip=shelf['ip_address'],ip2=shelf['secondary_ip_address'],shelf_id=sid,boost=boost,duty=duty))
                    preview=self.request('/esp32_preview?'+query,None)
                    result=self.native.infer(dict(kind='pair',image_back=preview['image_back'],image_front=preview['image_front'],boost=boost))
                    self.metrics['pair']=result['metrics'];candidate=result['candidate']
                    if candidate.get('hold') or preview['capture']['duration_ms']>2000 or (slots is not None and slots!=candidate['slots']):break
                    slots=candidate['slots'];samples.append(dict(front_class=candidate['front_class'],back_class=candidate['back_class'],
                        front_confidence=candidate['front_confidence'],back_confidence=candidate['back_confidence'],capture=preview['capture']))
                    if number<2 and self.stop.wait(2):raise RuntimeError('Stopping')
                if len(samples)==3:accepted=(preview,candidate,samples,boost);break
                if self.stop.wait(2):raise RuntimeError('Stopping')
            if accepted is None:raise ValueError('No three stable trusted pairs; inventory held')
            preview,candidate,samples,boost=accepted
            if self.stop.is_set() or not self.cameras_ready():raise ValueError('CCTV health lost before validation')
            return self.request('/esp32_validate',dict(shelf_id=sid,validation_token=token,source=source,
                trigger_id=trigger['trigger_id'] if trigger else None,expected_action=action,ip=shelf['ip_address'],ip2=shelf['secondary_ip_address'],
                image=preview['image'],image_back=preview['image_back'],image_front=preview['image_front'],
                predicted_box_count=candidate['total'],dual_cam_evidence={'version':'stacked-visibility-v4-x01','samples':samples},
                tm_confidence=min(candidate['front_confidence'],candidate['back_confidence']),boost=boost))
        except Exception:
            if token:
                try:self.request('/validation_progress',dict(shelf_id=sid,validation_token=token,run_id=self.run_id,failed=True,stage='Native validation failed; fresh verification required'))
                except Exception:pass
            raise
        finally:
            if token:self.request('/validation_end',dict(shelf_id=sid,token=token,trigger_id=trigger['trigger_id'] if trigger else None))
            else:self.request('/validation_cancel',dict(request_id=request_id))

    def _validations(self):
        while not self.stop.is_set():
            try:
                statuses=self.request('/shelf_readiness',None)['shelves'];shelves={s['shelf_id']:s for s in self.shelves()}
                if not self.cameras_ready():
                    self.state='Held: waiting for all three fresh cameras'
                    self.stop.wait(1)
                    continue
                candidates=[(shelves[sid],None) for sid,status in statuses.items() if sid in shelves and status['state'] in ('Starting','Offline','Held') and time.monotonic()>=self.retry_at.get(sid,0)]
                candidates += [(shelves[t['shelf_id']],t) for t in self.pending() if t['shelf_id'] in shelves and statuses.get(t['shelf_id'],{}).get('state')=='Ready']
                self.state='Running'
                for shelf,trigger in candidates[:1]:
                    sid=shelf['shelf_id'];self.retry_at[sid]=time.monotonic()+30
                    try:self.validate(shelf,trigger)
                    except Exception:self.log(self.run_id,'native_validation_held',shelf_id=sid,code='VALIDATION_PENDING')
            except Exception:self.state='Held: bridge/database unavailable'
            self.stop.wait(1)

    def close(self):
        self.stop.set();self.native.close()
        for thread in self.threads:thread.join(timeout=10)
        self.state='Stopped'
