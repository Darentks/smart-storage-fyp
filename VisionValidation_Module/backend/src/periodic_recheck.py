"""Expiring shared camera-validation leases; the native coordinator schedules count checks."""
import threading
import time
import uuid

class RecheckCoordinator:
    """One expiring lease across this bridge; bounded priority/FIFO waiters."""
    def __init__(self):
        self.global_lock = threading.RLock()
        self.leases = {}
        self.waiting = {}
        self.sequence = 0
        # One training reservation for one shelf. Other shelves keep counting. The reserved shelf loses its
        # count authority when capture starts (see camera_service), so ending/expiry needs no resume step.
        self.training=None

    def _prune(self):
        if self.training and time.monotonic()>=self.training['expires']:
            self.training=None
        now = time.time()
        self.leases = {s: lease for s, lease in self.leases.items() if lease[1] > now}
        self.waiting = {key: job for key, job in self.waiting.items() if job["expires"] > now}

    def busy(self, shelf=None):
        with self.global_lock:
            self._prune()
            return bool(self.leases) if shelf is None else shelf in self.leases

    def enqueue(self, shelf, source, request_id, created_at=None):
        with self.global_lock:
            self._prune()
            if request_id not in self.waiting:
                if len(self.waiting) >= 20:
                    return False
                self.sequence += 1
                self.waiting[request_id] = dict(sequence=self.sequence)
            self.waiting[request_id].update(shelf=shelf, source=source,
                order=created_at if isinstance(created_at, (int, float)) else self.waiting[request_id].get("order", time.time()),
                expires=time.time()+60)
            return True

    def begin(self, shelf, source="MANUAL", request_id=None, created_at=None):
        with self.global_lock:
            self._prune()
            if self.training and self.training['shelf']==shelf:return None
            request_id = request_id or shelf
            if not self.enqueue(shelf, source, request_id, created_at):
                return None
            if self.leases:
                return None
            first = min(self.waiting, key=lambda key: (
                self.waiting[key]["source"] in {"COUNT_CHECK","PERIODIC_RECHECK"},
                self.waiting[key]["order"], self.waiting[key]["sequence"]))
            if first != request_id:
                return None
            self.waiting.pop(request_id)
            token = str(uuid.uuid4())
            self.leases[shelf] = (token, time.time()+120)
            return token

    def request_training(self,shelf,ips,token=None,idle=True):
        """Reserve one shelf's cameras; drain that shelf's active lease without cancelling its validation.

        idle: the native scanner is not working on this shelf. Returns newly_ready=True exactly once,
        when capture may start, so the caller can drop that shelf's count authority first."""
        with self.global_lock:
            self._prune()
            if self.training is None:
                if token:raise ValueError('Training session expired; start a new batch')
                self.training=dict(token=str(uuid.uuid4()),shelf=shelf,ips=tuple(ips),ready=False,
                    deadline=time.monotonic()+600,expires=time.monotonic()+30)
            elif not token or token!=self.training['token']:
                raise ValueError('Another training session owns the cameras')
            if self.training['shelf']!=shelf or self.training['ips']!=tuple(ips):raise ValueError('Training camera assignment changed')
            self.training['expires']=min(time.monotonic()+30,self.training['deadline'])
            newly_ready=not self.training['ready'] and shelf not in self.leases and idle
            self.training['ready']=self.training['ready'] or newly_ready
            return dict(state='ready' if self.training['ready'] else 'waiting',token=self.training['token'],
                shelf_id=shelf,newly_ready=newly_ready)

    def end_training(self,token):
        with self.global_lock:
            self._prune()
            if self.training is None:return False
            if token!=self.training['token']:raise ValueError('Training session does not match')
            self.training=None
            return True

    def clear_training(self):
        """Bridge (re)start boundary: no reservation survives."""
        with self.global_lock:self.training=None

    def training_shelf(self):
        """The shelf reserved for training (waiting or capturing), else None."""
        with self.global_lock:
            self._prune()
            return self.training['shelf'] if self.training else None

    def training_capturing(self):
        """The shelf whose training capture has started, else None."""
        with self.global_lock:
            self._prune()
            return self.training['shelf'] if self.training and self.training['ready'] else None

    def training_capture_allowed(self,token,shelf,ips):
        """None: ordinary capture, no reservation applies; False: reserved/expired; True: bound owner."""
        with self.global_lock:
            self._prune()
            if self.training is None:return False if token else None
            if not token and shelf!=self.training['shelf']:return None
            if not (token==self.training['token'] and self.training['ready'] and shelf==self.training['shelf'] and tuple(ips)==self.training['ips'] and shelf not in self.leases):return False
            self.training['expires']=min(time.monotonic()+30,self.training['deadline'])
            return True

    def cancel(self, request_id):
        with self.global_lock:
            self.waiting.pop(request_id, None)

    def owns(self, shelf, token):
        with self.global_lock:
            self._prune()
            return shelf in self.leases and self.leases[shelf][0] == token

    def renew(self, shelf, token):
        with self.global_lock:
            if not self.owns(shelf, token):
                return False
            self.leases[shelf] = (token, time.time()+120)
            return True

    def end(self, shelf, token):
        with self.global_lock:
            if self.owns(shelf, token):
                self.leases.pop(shelf, None)
                return True
            return False
