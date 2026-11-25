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

    def _prune(self):
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
