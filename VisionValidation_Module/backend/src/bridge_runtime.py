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
