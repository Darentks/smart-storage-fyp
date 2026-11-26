"""Single process ownership and bounded structured logs (no raw images/credentials)."""
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import os
import re
import time

from .validation_diagnostics import FAILURE_CODES

LOG=logging.getLogger('smart_storage')
LOG.addHandler(logging.NullHandler())

class InstanceLock:
    def __init__(self,path):self.path=Path(path);self.file=None
    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.file=self.path.open('a+b')
        if os.fstat(self.file.fileno()).st_size==0:self.file.write(b'0');self.file.flush()
        self.file.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.file.close();raise RuntimeError('Another bridge instance owns this deployment')
        return self
    def __exit__(self,*args):
        if self.file:
            if os.name=='nt':
                import msvcrt
                self.file.seek(0);msvcrt.locking(self.file.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_UN)
            self.file.close()

def process_started_at():
    """This process's creation time (Unix seconds). The Windows launcher compares it with the operating system's
    record so a reused process ID is never mistaken for the bridge. None where it cannot be read."""
    if os.name!='nt':return None
    try:
        import ctypes
        from ctypes import wintypes
        # A private handle with explicit 64-bit-safe types (the default int conversion truncates HANDLE).
        kernel=ctypes.WinDLL('kernel32')
        kernel.GetCurrentProcess.restype=wintypes.HANDLE
        kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
        kernel.GetProcessTimes.restype=wintypes.BOOL
        created,exited,kernel_time,user_time=(wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(kernel.GetCurrentProcess(),ctypes.byref(created),ctypes.byref(exited),ctypes.byref(kernel_time),ctypes.byref(user_time)):
            return None
        ticks=(created.dwHighDateTime<<32)|created.dwLowDateTime  # 100 ns since 1601-01-01 UTC
        return ticks/1e7-11644473600
    except (OSError,AttributeError,ValueError):return None


def write_process_record(path,role,**fields):
    """Atomically record which process owns a role (bridge or supervisor). Best effort: a locked file
    (for example OneDrive syncing) must never stop the bridge; the launcher then uses its fallback checks."""
    path=Path(path);record=dict(role=role,pid=os.getpid(),started_at=process_started_at(),written_at=time.time(),**fields)
    try:
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_name(path.name+'.'+str(os.getpid())+'.tmp')
        temporary.write_text(json.dumps(record),encoding='utf-8');os.replace(temporary,path)
        return True
    except OSError:return False


def remove_process_record(path):
    """Remove the record only if it still describes this process."""
    path=Path(path)
    try:
        if json.loads(path.read_text(encoding='utf-8')).get('pid')==os.getpid():path.unlink()
    except (OSError,ValueError,AttributeError):pass


_SECRET_PATTERNS=(
    (re.compile(r'\brtsps?://\S+',re.I),'rtsp://[redacted]'),  # camera source URLs, with or without credentials
    (re.compile(r'([a-z][a-z0-9+.-]*://)[^/\s@:]+(?::[^/\s@]*)?@',re.I),r'\1[redacted]@'),  # user:password@ in any URL
    (re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+'),'Bearer [redacted]'),
    (re.compile(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+'),'[redacted-token]'),  # JWTs, e.g. Supabase keys
    (re.compile(r'(?i)\b([a-z0-9_]*(?:password|passwd|pwd|secret|token|api_?key|service_?role|authorization)[a-z0-9_]*)(["\']?\s*[=:]\s*)["\']?[^\s,;&"\']+'),r'\1\2[redacted]'),
)


def redact(text):
    """Strip credentials, tokens and camera source URLs from free text before it is written to disk."""
    text=str(text)
    for pattern,replacement in _SECRET_PATTERNS:text=pattern.sub(replacement,text)
    return text


def write_startup_error(path,text,limit=4000):
    """Overwrite one small, redacted error report (newest only, bounded size)."""
    body=redact(text)
    if len(body)>limit:body='...\n'+body[-limit:]
    try:
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        Path(path).write_text(time.strftime('%Y-%m-%d %H:%M:%S')+' bridge stopped with an error\n'+body+'\n',encoding='utf-8')
    except OSError:pass


TM_SOURCE_RULE=re.compile(r'^https://teachablemachine\.withgoogle\.com/models/([A-Za-z0-9_-]+)/$')


def read_model_sources(path):
    """The Front/Back links the model updater will use (deploy/tm-model-sources.json), as short model IDs."""
    row=dict(file='deploy/tm-model-sources.json',Front=None,Back=None,error=None)
    try:data=json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError,ValueError):return dict(row,error='Settings file missing or not valid JSON')
    if not isinstance(data,dict) or data.get('version')!=1:return dict(row,error='Settings file needs "version": 1')
    for role in ('Front','Back'):
        match=TM_SOURCE_RULE.fullmatch(data.get(role)) if isinstance(data.get(role),str) else None
        if not match:return dict(row,error='Please input the correct Teachable Machine link for the '+role+' model')
        row[role]=match.group(1)
    if row['Front']==row['Back']:return dict(row,error='Front and Back links are the same model')
    return row


def model_update_status(path,loaded_versions):
    """The latest model-update result (written by deploy/update_tm_models.py) plus whether this bridge has
    actually loaded those models: 'verified', 'mismatch' or 'pending' (models still loading). None if no
    update has run. Only short version codes, stages and messages are returned."""
    try:result=json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError,ValueError):return None
    if not isinstance(result,dict):return None
    roles={}
    for role in ('Front','Back'):
        row=(result.get('roles') or {}).get(role) or {}
        short=lambda value:value[:12] if isinstance(value,str) and re.fullmatch(r'[a-f0-9]{64}',value) else None
        roles[role]=dict(old=short(row.get('old')),new=short(row.get('new')),changed=bool(row.get('changed')),new_full=row.get('new'))
    loaded=None
    if result.get('outcome') in ('installed','unchanged'):
        current={role:((loaded_versions or {}).get(role) or {}).get('weights_sha256') for role in ('Front','Back')}
        if not all(current.values()):loaded='pending'
        else:loaded='verified' if all(current[role]==roles[role]['new_full'] for role in roles) else 'mismatch'
    for row in roles.values():row.pop('new_full')
    failure=result.get('failure') if isinstance(result.get('failure'),dict) else None
    return dict(id=str(result.get('id',''))[:64],at=result.get('at'),outcome=str(result.get('outcome','')),roles=roles,
        failure=dict(stage=str(failure.get('stage') or '')[:20],role=failure.get('role') if failure.get('role') in ('Front','Back') else None,
            message=redact(str(failure.get('message') or ''))[:300]) if failure else None,
        backup=str(result.get('backup') or '')[:80] or None,cleanup_pending=[redact(str(x))[:160] for x in (result.get('cleanup_pending') or [])][:10],
        loaded=loaded)


def configure_logs(directory):
    path=Path(directory);path.mkdir(parents=True,exist_ok=True)
    for old in LOG.handlers:
        old.close()
    handler=RotatingFileHandler(path/'bridge.jsonl',maxBytes=2_000_000,backupCount=5,encoding='utf-8')
    os.chmod(path/'bridge.jsonl',0o600)
    handler.setFormatter(logging.Formatter('%(message)s'))
    LOG.handlers=[handler];LOG.setLevel(logging.INFO);LOG.propagate=False

def event(startup_id,event_type,**fields):
    # Technical health/retry logs must not recreate the intent/event history.
    allowed={'shelf_id','state','code','attempt','capture_number','status','camera_id','reason','duration_ms'}
    if event_type in {'reconciliation','edge_shadow_prediction'}:return
    def technical(value):
        return isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_. -]{1,80}',value) is not None
    if not technical(startup_id) or not technical(event_type):return
    safe={k:v for k,v in fields.items() if k in allowed and
        (v is None or type(v) in (int,bool) or technical(v))}
    if safe.get('code') not in FAILURE_CODES | {'WORKER_UNAVAILABLE','SHELF_NOT_READY','BRIDGE_EXIT','SERVICE_CUTOVER_BLOCKED','VALIDATION_PENDING'}:safe.pop('code',None)
    LOG.info(json.dumps(dict(timestamp=time.time(),startup_id=startup_id,event=event_type,**safe)))
