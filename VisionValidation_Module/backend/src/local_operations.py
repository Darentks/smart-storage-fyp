"""Small local operational stores; no cloud I/O, images, archives or credentials."""
import json
import math
import os
import re
from pathlib import Path
import tempfile
import threading
import time

from .autonomous_edge import polygon, source_binding
from .validation_diagnostics import FAILURE_CODES
from .shelves import SHELF_IDS, SHELF_SET


def atomic_json(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    descriptor,temp=tempfile.mkstemp(prefix='.'+path.name+'-',dir=path.parent)
    try:
        with os.fdopen(descriptor,'w',encoding='utf8') as stream:
            json.dump(value,stream,separators=(',',':'),allow_nan=False);stream.flush();os.fsync(stream.fileno())
        os.chmod(temp,0o600);os.replace(temp,path)
        if os.name!='nt':
            fd=os.open(path.parent,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
    finally:
        if os.path.exists(temp):os.unlink(temp)


class LatestIntents:
    """One persistent JSON file, global five records; no backups or append log."""
    def __init__(self,path):
        self.path=Path(path);self.lock=threading.RLock();self.records=[]
        if self.path.exists():
            value=json.loads(self.path.read_text(encoding='utf8'))
            if value.get('version')!=1 or not isinstance(value.get('records'),list):raise ValueError('Local intents corrupt; operator recovery required')
            self.records=[self.clean(r) for r in value['records']][-5:]
            self.save()  # Enforce retention on startup as well as insertion.

    @staticmethod
    def clean(record):
        result={key:record[key] for key in ('id','timestamp','camera_id','shelf_id','action','status','reason') if key in record}
        if not isinstance(result.get('id'),str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}',result['id']):raise ValueError('Invalid intent identity')
        if type(result.get('timestamp')) not in (int,float) or not math.isfinite(result['timestamp']):raise ValueError('Invalid intent timestamp')
        if result.get('camera_id') not in (None,'CCTV_01','CCTV_02','CCTV_03'):raise ValueError('Invalid camera identity')
        if result.get('shelf_id') not in (None,'ANY_SHELF',*SHELF_IDS):raise ValueError('Invalid shelf identity')
        if result.get('action') not in (None,'BORROW','RETURN','RECHECK'):raise ValueError('Invalid candidate action')
        if result.get('status') not in ('PENDING','CONSENSUS','HELD','ACCEPTED','RECONCILED'):raise ValueError('Invalid intent status')
        # Reasons are codes only, never arbitrary client text/PII/URLs.
        if result.get('reason') is not None and result['reason'] not in FAILURE_CODES:
            result['reason']='VALIDATION_REJECTED'
        return result

    def save(self):atomic_json(self.path,dict(version=1,records=self.records))

    def add(self,record):
        record=self.clean(record)
        with self.lock:
            records=[r for r in self.records if r['id']!=record['id']]+[record]
            atomic_json(self.path,dict(version=1,records=records[-5:]));self.records=records[-5:]

    def update(self,identity,status,reason=None):
        with self.lock:
            records=[self.clean(dict(r,status=status,reason=reason)) if r['id']==identity and not
                (status=='HELD' and r['status'] in ('ACCEPTED','RECONCILED')) else dict(r) for r in self.records]
            atomic_json(self.path,dict(version=1,records=records));self.records=records

    def snapshot(self):
        with self.lock:return [dict(r) for r in self.records]


def overlaps(a,b):
    for points in (a,b):
        for i,p in enumerate(points):
            q=points[(i+1)%4];axis=(-(q[1]-p[1]),q[0]-p[0])
            first=[x*axis[0]+y*axis[1] for x,y in a];second=[x*axis[0]+y*axis[1] for x,y in b]
            if max(first)<=min(second)+1e-9 or max(second)<=min(first)+1e-9:return False
    return True


def validate_rois(value):
    if value.get('version')!=1 or not isinstance(value.get('cameras'),list) or len(value['cameras'])!=3:raise ValueError('Three cameras required')
    cameras={};sources=set()
    for row in value['cameras']:
        cid=row.get('cctv_id');role='SIDE_PRESENCE' if cid=='CCTV_03' else 'GRID_6_ROI'
        if cid not in ('CCTV_01','CCTV_02','CCTV_03') or cid in cameras or row.get('camera_role')!=role:raise ValueError('Camera identity/role mismatch')
        binding=row['binding'];source=binding['source']
        raw=source[7:] if source.startswith('device:') else source
        if source_binding(raw)!=source or '@' in source or binding.get('camera_role')!=role or source in sources:raise ValueError('Invalid or shared source binding')
        sources.add(source)
        required=['RACK_ENTRY_ZONE'] if role=='SIDE_PRESENCE' else list(SHELF_IDS)
        rois={s:{'vertices':polygon(row['rois'][s]),'coordinate_space':'normalized'} for s in required}
        polygons=[r['vertices'] for r in rois.values()]
        for i,a in enumerate(polygons):
            for j in range(i+1,len(polygons)):
                if overlaps(a,polygons[j]):
                    raise ValueError(f'Overlapping shelf ROIs: {cid}: {required[i]} overlaps {required[j]}; redraw these regions without shared area')
        cameras[cid]=dict(cctv_id=cid,camera_role=role,binding=dict(source=source,camera_role=role),rois=rois)
    return dict(version=1,cameras=[cameras[k] for k in sorted(cameras)])


class RoiStore:
    def __init__(self,path):self.path=Path(path);self.lock=threading.RLock()
    def load(self):
        with self.lock:return validate_rois(json.loads(self.path.read_text(encoding='utf8')))
    def import_config(self,value):
        validated=validate_rois(value)
        with self.lock:
            if self.path.exists():atomic_json(str(self.path)+'.previous',self.load())
            atomic_json(self.path,validated)
        return validated


class CloudMetrics:
    def __init__(self):self.lock=threading.Lock();self.data=dict(calls=0,reads=0,writes=0,request_bytes_estimate=0,response_bytes=0,failures=0,cache_hits=0)
    def record(self,method,request_bytes,response_bytes,ok):
        with self.lock:
            self.data['calls']+=1;self.data['reads' if method.upper()=='GET' else 'writes']+=1
            self.data['request_bytes_estimate']+=request_bytes;self.data['response_bytes']+=response_bytes
            if not ok:self.data['failures']+=1
    def snapshot(self):
        with self.lock:return dict(self.data)
    def hit(self):
        with self.lock:self.data['cache_hits']+=1

CLOUD_METRICS=CloudMetrics()


class LocalFailures:
    """Only unresolved shelf failures, replaced/cleared instead of archived."""
    def __init__(self,path):
        self.path=Path(path);self.lock=threading.RLock()
        self.values=json.loads(self.path.read_text()) if self.path.exists() else {}
        if not isinstance(self.values,dict) or len(self.values)>len(SHELF_IDS):raise ValueError('Invalid local failure state')
        for shelf,row in self.values.items():
            if (shelf not in SHELF_SET or not isinstance(row,dict)
                or set(row)!={'state','code','at'} or row['state'] not in {'Held','Offline','Needs Reconciliation'}
                or row['code'] not in FAILURE_CODES | {None} or type(row['at']) not in (int,float) or not math.isfinite(row['at'])):
                raise ValueError('Local failures may contain only bounded technical metadata')
    def set(self,shelf,state,code):
        if shelf not in SHELF_SET:return
        if code is not None and code not in FAILURE_CODES:code='VALIDATION_REJECTED'
        with self.lock:
            if state=='Ready':self.values.pop(shelf,None)
            elif state in {'Held','Offline','Needs Reconciliation'}:
                self.values[shelf]=dict(state=state,code=code,at=time.time())
            else:return  # Starting/checking does not hide the unresolved failure.
            atomic_json(self.path,self.values)
    def snapshot(self):
        with self.lock:return json.loads(json.dumps(self.values))
