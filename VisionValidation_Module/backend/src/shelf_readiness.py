"""Per-process operating state. Persisted inventory remains the only baseline."""
import time
import threading
import uuid
from typing import TYPE_CHECKING, Optional
if TYPE_CHECKING:
    from .local_operations import LocalFailures
from collections import deque
from .validation_diagnostics import failure_code
from .bridge_runtime import event

class ShelfReadiness:
    def __init__(self):
        self.run_id=str(uuid.uuid4())
        self.states={}
        self.observations={}
        self.diagnostics=deque(maxlen=200)
        self.lock=threading.RLock()
        self.failures: Optional[LocalFailures] = None
        self.recovery_retries: dict[str, float] = {}

    def get(self,shelf):
        with self.lock:
            return dict(self.states.get(shelf,dict(state='Starting',reason='Awaiting fresh startup verification',revision=None,ready_at=0,code=None,retryable=False)))

    def set(self,shelf,state,reason='',revision=None):
        if state not in {'Starting','Checking Shelf','Ready','Validating Action','Held','Offline','Needs Reconciliation'}:
            raise ValueError('Invalid shelf state')
        with self.lock:
            old=self.get(shelf)
            if state == 'Validating Action' and old['state'] not in {'Ready','Validating Action'}:
                raise ValueError('Only Ready shelves may validate actions')
            self.states[shelf]=dict(state=state,reason=reason,revision=revision if revision is not None else old['revision'],
                ready_at=time.time() if state=='Ready' else old.get('ready_at',0),
                code=failure_code(reason) if state in {'Held','Offline','Needs Reconciliation'} else None,
                retryable=False)
            if self.failures is not None:self.failures.set(shelf,state,self.states[shelf]['code'])
            event(self.run_id,'health_transition',shelf_id=shelf,state=state,code=self.states[shelf]['code'])

    def record(self,shelf,token,trigger_id=None,**metadata):
        event(self.run_id,'validation',shelf_id=shelf,lease=token,trigger_id=trigger_id,**{k:v for k,v in metadata.items() if k in {'code','attempt','capture_number'}})
        # Explicit whitelist: no images, URLs, keys or arbitrary client payloads.
        allowed={'attempt','capture_number','max_attempts','stage','boost','duty',
                 'front_confidence','back_confidence','capture_completed_at','code','retryable'}
        with self.lock:
            while self.diagnostics and time.time()-self.diagnostics[0]['timestamp']>300:self.diagnostics.popleft()
            self.diagnostics.append(dict(run_id=self.run_id,shelf_id=shelf,lease=token,
                trigger_id=trigger_id,timestamp=time.time(),**{k:(v[:300] if isinstance(v,str) else v) for k,v in metadata.items() if k in allowed and type(v) in (str,int,float,bool,type(None))}))

    def ready(self,shelf):
        return self.get(shelf)['state']=='Ready'

    def active(self,shelf):
        return self.get(shelf)['state'] in {'Ready','Validating Action'}

    def verify(self,shelf,saved,slots):
        self.set(shelf,'Checking Shelf','Comparing fresh observations with persisted slots')
        if saved['confirmed_occupancy'] is None:
            self.set(shelf,'Needs Reconciliation','No independently verified baseline; operator commissioning required')
            raise ValueError(self.get(shelf)['reason'])
        if list(slots)!=saved['confirmed_occupancy']:
            self.set(shelf,'Needs Reconciliation','Startup occupancy differs from confirmed inventory; no update applied')
            raise ValueError(self.get(shelf)['reason'])
        self.set(shelf,'Ready','Fresh startup verification passed',saved['revision'])
